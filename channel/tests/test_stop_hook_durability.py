"""Durability tests for the Stop hook relay.

These tests cover the failure modes that motivated the 2026-05-07 rewrite
(queue 187 dogfooding): a single transient hub blip used to drop the
assistant reply forever because the hook caught HTTPError + URLError +
OSError and returned, with no retry, no spool, and no surface to the user.

Three scenarios:

1. **Transient blip recovers.** Spawn a fake hub that responds 503 on
   the first 2 attempts then 200. The hook must persist through the
   retry loop and the event must land. With TUBEMAIL_STOP_HOOK_VERIFY=1
   the hook also re-fetches the event to confirm it persisted.

2. **Hub fully down → spool + exit 2 only when spool also fails.** When
   the hub is unreachable, the hook spools the event to disk and exits 0
   (durable; will drain on next Stop). When the spool dir is unwritable
   AND the hub is down, exit 2 fires.

3. **Spool drains on next invocation.** Pre-populate the spool dir with a
   stale event, bring the hub up, run the hook with a NEW reply. Both
   the stale spooled event AND the new event must end up at the hub,
   and the spool dir must be empty afterwards.
"""

from __future__ import annotations

# Standard Libraries
import http.server
import json
import socket
import socketserver
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 3rd party
import pytest


HOOK_SCRIPT = Path(__file__).resolve().parents[1] / "hooks" / "post_stop_relay.py"


# ── fake hub ────────────────────────────────────────────────────────────────


def _free_port() -> int:
    """Bind to port 0 to let the kernel pick a free port, then release it.
    There's a TOCTOU race between the close and the test server bind, but
    the window is small enough in practice and we have no in-test
    alternative without taking a hard dependency on aiohttp."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeHubHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler whose behavior is driven by the parent server's state.

    Two surfaces:
      POST /tubemail/<worker>/outbound — records the body in `outbound_events`
        on the parent server and returns either 503 (configurable failure
        count) or 200 with `{event_id, ts}`.
      GET  /tubemail/<worker>/events — returns the recorded events so the
        verify-readback path can confirm persistence.
    """

    # Quiet down the default stderr access log (noisy in pytest output).
    def log_message(self, format, *args):
        return

    def do_POST(self):
        srv: "_FakeHub" = self.server  # type: ignore[assignment]
        srv.post_attempts += 1

        if srv.fail_remaining > 0:
            srv.fail_remaining -= 1
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail": "service unavailable"}')
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        event_id = f"evt-{srv.post_attempts:04d}"
        ts = time.time()
        srv.outbound_events.append({
            "event_id": event_id,
            "ts": ts,
            "kind": "outbound",
            "content": parsed.get("text", ""),
            "meta": parsed.get("meta", {}),
        })

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"event_id": event_id, "ts": ts}).encode())

    def do_GET(self):
        srv: "_FakeHub" = self.server  # type: ignore[assignment]
        srv.get_attempts += 1
        # Don't bother parsing query string; verify path uses the unbounded list.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps({"events": srv.outbound_events}).encode()
        )


class _FakeHub(socketserver.TCPServer):
    """ThreadingTCPServer would also work, but pytest runs serially per
    test and a single-threaded server makes attempt counting deterministic.

    We do NOT inherit ThreadingTCPServer because urllib makes one connection
    per attempt, sequentially — a single handler thread is enough.
    """

    allow_reuse_address = True

    def __init__(self, port: int, *, fail_count: int = 0):
        super().__init__(("127.0.0.1", port), _FakeHubHandler)
        self.fail_remaining = fail_count
        self.post_attempts = 0
        self.get_attempts = 0
        self.outbound_events: list[dict] = []


@contextmanager
def fake_hub(port: int, *, fail_count: int = 0) -> Iterator[_FakeHub]:
    server = _FakeHub(port, fail_count=fail_count)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _run_hook(
    stdin: str,
    *,
    worker: str,
    secret: str,
    hub_port: int | None,
    spool_dir: Path,
    retries: int = 3,
    verify: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook script as a subprocess (mirrors how Claude Code
    fires hooks). hub_port=None forces an unreachable URL (hub-down case).
    """
    hub_url = (
        f"http://127.0.0.1:{hub_port}" if hub_port is not None
        # Use a port that's almost certainly closed — the hook will get
        # connection-refused on every attempt.
        else "http://127.0.0.1:1"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "TM_WORKER_NAME": worker,
        "TUBEMAIL_SECRET": secret,
        "TUBEMAIL_HUB_URL": hub_url,
        "TUBEMAIL_STOP_HOOK_RETRIES": str(retries),
        "TUBEMAIL_STOP_HOOK_SPOOL_DIR": str(spool_dir),
        "HOME": str(spool_dir.parent),
    }
    if verify:
        env["TUBEMAIL_STOP_HOOK_VERIFY"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _stop_payload(text: str) -> str:
    return json.dumps({"messages": [{"role": "assistant", "content": text}]})


# ── case 1: transient blip recovers ─────────────────────────────────────────


def test_retry_recovers_after_transient_503s(tmp_path: Path):
    """Hub returns 503 twice, then 200. Hook must retry and the event must
    land on the third attempt. With verify on, the hook also confirms the
    event is readable via the events GET endpoint.
    """
    port = _free_port()
    spool = tmp_path / "spool"
    with fake_hub(port, fail_count=2) as hub:
        proc = _run_hook(
            _stop_payload("retry test reply"),
            worker="test-tm",
            secret="abc123",
            hub_port=port,
            spool_dir=spool,
            retries=3,
            verify=True,
        )

    # Hook must succeed (exit 0) after the 3rd attempt lands.
    assert proc.returncode == 0, (
        f"hook exit={proc.returncode}\nstderr={proc.stderr}"
    )
    # Three attempts — two 503s + one 200.
    assert hub.post_attempts == 3, (
        f"expected 3 POST attempts, saw {hub.post_attempts}"
    )
    # Event landed.
    assert len(hub.outbound_events) == 1
    assert hub.outbound_events[0]["content"] == "retry test reply"
    assert hub.outbound_events[0]["meta"] == {"kind": "stop_relay"}
    # Verify path fired at least one GET.
    assert hub.get_attempts >= 1
    # Stderr surfaces retry warnings so a developer tailing logs sees the
    # pattern when something is flaky.
    assert "WARNING" in proc.stderr
    # Spool stays empty — the new event made it on the retry.
    assert not list(spool.glob("**/*.json"))


# ── case 2: hub down → spool, then exit 2 if spool also fails ──────────────


def test_hub_down_spools_and_exits_zero(tmp_path: Path):
    """No hub at all. Hook must NOT raise, must exit 0 (durable: spooled),
    and must write the event to the per-worker spool dir."""
    spool = tmp_path / "spool"
    proc = _run_hook(
        _stop_payload("hub-down reply"),
        worker="downhub-tm",
        secret="abc123",
        hub_port=None,  # unreachable
        spool_dir=spool,
        retries=2,
    )

    assert proc.returncode == 0, (
        f"hook exit={proc.returncode}\nstderr={proc.stderr}"
    )
    spooled = list((spool / "downhub-tm").glob("*.json"))
    assert len(spooled) == 1, f"expected 1 spool entry, got {spooled}"
    body = json.loads(spooled[0].read_bytes())
    assert body["text"] == "hub-down reply"
    assert body["meta"] == {"kind": "stop_relay"}
    # Mode must be 0600 (defense in depth — a leaked stop_relay body could
    # contain auth tokens, secrets, paths, etc.).
    mode = spooled[0].stat().st_mode & 0o777
    assert mode == 0o600, f"spool entry mode={oct(mode)} (want 0o600)"


def test_hub_down_AND_spool_unwritable_exits_two(tmp_path: Path):
    """When BOTH the POST chain AND the spool write fail, exit 2 — Claude
    Code surfaces non-zero hook exits in the session UI per
    ~/.claude/CLAUDE.md, so the user sees the loss instead of the silent
    black hole.
    """
    # Make the spool root a regular file so mkdir() fails.
    spool_root = tmp_path / "spool-blocker"
    spool_root.write_text("not a directory")

    proc = _run_hook(
        _stop_payload("doomed reply"),
        worker="doomed-tm",
        secret="abc123",
        hub_port=None,
        spool_dir=spool_root,  # mkdir on this path will fail (it's a file)
        retries=1,
    )

    assert proc.returncode == 2, (
        f"hook exit={proc.returncode} (want 2)\nstderr={proc.stderr}"
    )
    assert "LOST" in proc.stderr or "spool write failed" in proc.stderr


# ── case 3: drain on next invocation ───────────────────────────────────────


def test_spool_drains_on_next_invocation(tmp_path: Path):
    """Pre-populate the spool, bring the hub up, run the hook with a new
    reply. Both the stale spooled event AND the new event must reach the
    hub, ordered: stale first, then new.
    """
    port = _free_port()
    spool = tmp_path / "spool"
    worker_spool = spool / "drain-tm"
    worker_spool.mkdir(parents=True)
    # Two stale spooled entries to verify oldest-first ordering.
    stale_a = worker_spool / "1700000000.000000-aaaaaaaaaaaa.json"
    stale_a.write_bytes(
        json.dumps({"text": "stale-A", "meta": {"kind": "stop_relay"}}).encode()
    )
    stale_b = worker_spool / "1700000001.000000-bbbbbbbbbbbb.json"
    stale_b.write_bytes(
        json.dumps({"text": "stale-B", "meta": {"kind": "stop_relay"}}).encode()
    )

    with fake_hub(port) as hub:
        proc = _run_hook(
            _stop_payload("fresh reply"),
            worker="drain-tm",
            secret="abc123",
            hub_port=port,
            spool_dir=spool,
            retries=2,
        )

    assert proc.returncode == 0, (
        f"hook exit={proc.returncode}\nstderr={proc.stderr}"
    )
    # All three events landed: two drained + one fresh.
    contents = [e["content"] for e in hub.outbound_events]
    assert contents == ["stale-A", "stale-B", "fresh reply"], contents
    # Spool dir is empty after the drain.
    assert not list(worker_spool.glob("*.json")), (
        "spool not drained after hub recovery"
    )


def test_spool_partial_drain_when_hub_flaps_mid_drain(tmp_path: Path):
    """Spool has 3 entries, hub returns 503 forever. Hook drains nothing,
    leaves all 3 entries in place (so they retry on the next Stop), and
    spools the new reply too. With retries=1 the hook makes ONE POST per
    spool entry up to the first failure — and bails immediately on the
    first failure to avoid burning the retry budget on every entry.
    """
    port = _free_port()
    spool = tmp_path / "spool"
    worker_spool = spool / "flapper-tm"
    worker_spool.mkdir(parents=True)
    for i, label in enumerate(["A", "B", "C"]):
        p = worker_spool / f"170000000{i}.000000-{label * 12}.json"
        p.write_bytes(
            json.dumps({"text": f"stale-{label}", "meta": {"kind": "stop_relay"}}).encode()
        )

    with fake_hub(port, fail_count=10**6) as hub:
        proc = _run_hook(
            _stop_payload("fresh-reply-during-flap"),
            worker="flapper-tm",
            secret="abc123",
            hub_port=port,
            spool_dir=spool,
            retries=1,
        )

    # Hook still exits 0 — fresh reply ended up spooled, no events were lost.
    assert proc.returncode == 0, (
        f"hook exit={proc.returncode}\nstderr={proc.stderr}"
    )
    # Hub saw at least one drain attempt + one new POST attempt; both 503'd.
    assert hub.post_attempts >= 2
    assert len(hub.outbound_events) == 0
    # All 3 stale entries are still spooled, plus the new one.
    remaining = sorted(p.name for p in worker_spool.glob("*.json"))
    assert len(remaining) == 4, (
        f"expected 4 spool entries (3 stale + 1 fresh), got {remaining}"
    )


# ── unit tests for the spool primitives ────────────────────────────────────


@pytest.fixture
def hook_module(tmp_path, monkeypatch):
    """Import the hook script as a module so we can call internals directly.
    Sandboxes the spool dir to tmp_path so unit tests don't touch the
    developer's real ~/.claude/."""
    monkeypatch.setenv("TUBEMAIL_STOP_HOOK_SPOOL_DIR", str(tmp_path / "spool"))
    import importlib.util

    spec = importlib.util.spec_from_file_location("post_stop_relay", HOOK_SCRIPT)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_spool_caps_at_limit(hook_module, tmp_path, monkeypatch):
    """Once the spool reaches SPOOL_CAP, the oldest entry is evicted with
    a stderr WARNING. Pin the cap small so we don't have to write 100
    files in the test."""
    monkeypatch.setattr(hook_module, "SPOOL_CAP", 3)
    worker = "cap-tm"

    for i in range(3):
        ok = hook_module._spool_event(worker, f"body-{i}".encode())
        assert ok
        # Nudge mtime forward so sort order is stable across the loop.
        time.sleep(0.01)
    #end for

    d = hook_module._spool_dir(worker)
    assert len(list(d.glob("*.json"))) == 3

    # 4th write evicts the oldest.
    ok = hook_module._spool_event(worker, b"body-3")
    assert ok
    entries = sorted(d.glob("*.json"))
    assert len(entries) == 3
    contents = [e.read_bytes() for e in entries]
    assert b"body-0" not in contents
    assert b"body-3" in contents


def test_backoff_schedule(hook_module):
    """Sanity-check the backoff helper used between retry attempts."""
    assert hook_module._backoff_s(0) == 0.5
    assert hook_module._backoff_s(1) == 2.0
    assert hook_module._backoff_s(2) == 5.0
    # Out-of-range attempts clamp to the tail value (no IndexError).
    assert hook_module._backoff_s(99) == 5.0
    # Negative is treated as 0 wait (defensive).
    assert hook_module._backoff_s(-1) == 0.0


def test_retries_from_env_invalid_falls_back_to_default(hook_module, monkeypatch):
    monkeypatch.setenv("TUBEMAIL_STOP_HOOK_RETRIES", "not-a-number")
    assert hook_module._retries_from_env() == hook_module.DEFAULT_RETRIES


def test_retries_from_env_zero_clamped_to_one(hook_module, monkeypatch):
    """0 retries means "don't even try once" which is silently broken;
    clamp to 1 so the hook always makes at least one attempt."""
    monkeypatch.setenv("TUBEMAIL_STOP_HOOK_RETRIES", "0")
    assert hook_module._retries_from_env() == 1
