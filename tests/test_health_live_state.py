"""Regression tests for the 2026-04-24 RCA on /health reporting stale
state as live.

See jjstack/investigate/20260424-162835-health-reports-stale-state-as-live.md
for the full analysis. The rule these tests pin:

    /health's live-state metrics (workers_online, pending_permissions,
    safe_to_restart) MUST reflect only the live subscription set —
    never the on-disk registry.

Three things we want to guarantee forever:

1. `workers_online` excludes persisted-but-offline workers.
2. `pending_permissions` in /health excludes zombies from dead sessions.
3. `safe_to_restart` is True when nothing is actually in flight, even
   if there are many stale state files on disk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tubemail_hub.bridge.engine import BridgeEngine
from tubemail_hub.bridge.models import PermissionRequestPayload


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TUBEMAIL_SECRET", "test-secret-abc123")


def _fresh_app(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Build the full app against a clean data dir. `data_dir` is the
    TUBEMAIL_DATA_DIR; workers live under `data_dir / "workers"` per
    BridgeEngine convention."""
    monkeypatch.setenv("TUBEMAIL_DATA_DIR", str(data_dir))
    from tubemail_hub.server import create_app
    return create_app()


def _plant_worker_state(data_dir: Path, name: str, pending: int = 0) -> None:
    """Write a worker state file directly to disk, simulating a stale
    registry entry left behind by a past session. No SSE subscriber is
    ever attached for this worker — it's a zombie."""
    workers_dir = data_dir / "workers"
    workers_dir.mkdir(parents=True, exist_ok=True)
    pending_list = [
        {
            "request_id": f"zombie-{i:03d}",
            "tool_name": "Bash",
            "description": "pretend i matter",
            "input_preview": "echo hi",
        }
        for i in range(pending)
    ]
    state = {
        "name": name,
        "cwd": "/tmp",
        "registered_at": 0.0,
        "last_activity": 0.0,
        "forwarder_version": "",
        "events": [],
        "pending_permissions": pending_list,
        "exited_cleanly": False,
    }
    (workers_dir / f"{name}.json").write_text(json.dumps(state))


def test_health_workers_online_excludes_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Plant 5 zombie workers on disk.
    for i in range(5):
        _plant_worker_state(tmp_path, f"zombie-{i}-tm")

    app = _fresh_app(tmp_path, monkeypatch)
    client = TestClient(app)
    body = client.get("/health").json()

    # Nothing has an SSE subscriber → workers_online is 0, workers_total is 5.
    assert body["workers_online"] == 0
    assert body["workers_total"] == 5


def test_health_pending_excludes_zombies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Plant a zombie with 4 pending permissions. This is exactly the
    # real-world case that triggered the RCA: the operator believed
    # 4 prompts were waiting on them, when in fact the Claude session
    # that generated those prompts was long gone.
    _plant_worker_state(tmp_path, "zombie-tm", pending=4)

    app = _fresh_app(tmp_path, monkeypatch)
    client = TestClient(app)
    body = client.get("/health").json()

    assert body["pending_permissions"] == 0, (
        f"zombie pending should not count in /health, got {body}"
    )


def test_safe_to_restart_true_when_only_zombies_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _plant_worker_state(tmp_path, "zombie-tm", pending=10)
    app = _fresh_app(tmp_path, monkeypatch)
    client = TestClient(app)
    body = client.get("/health").json()
    assert body["safe_to_restart"] is True, (
        f"hub should be safe to restart — the only 'pending' work is "
        f"zombie state from dead sessions, got {body}"
    )


def test_health_output_is_pretty_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Human-readable JSON (indented) so `curl /health` is useful in a
    terminal without piping through jq. Regression test for the 2026-04-24
    request."""
    app = _fresh_app(tmp_path, monkeypatch)
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert "\n" in r.text, f"expected multi-line output, got: {r.text[:80]}"
    # Specifically, 2-space indent as promised.
    assert '  "service"' in r.text or '  "status"' in r.text


async def _subscribe_for(engine: BridgeEngine, worker: str, ticks: int = 1):
    """Drive engine.subscribe() just long enough to register the worker
    as online, then let the finally block run cleanup when the iterator
    closes."""
    gen = engine.subscribe(worker)
    # First get() inside the generator blocks; kick it off.
    task = asyncio.create_task(gen.__anext__())
    # Give it a tick so it hits the await q.get().
    await asyncio.sleep(0)
    # Cancel → the finally block runs → subscriber removed → zombie
    # cleanup path fires.
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def test_pending_cleared_when_subscriber_leaves_without_reregister(
    tmp_path: Path,
):
    """When a forwarder's SSE subscription closes and nothing else
    replaces it, any pending permissions that Claude session had queued
    are dropped. They are not actionable — the session is gone."""
    engine = BridgeEngine(data_dir=tmp_path)
    await engine.register_worker("demo-tm", "/tmp")

    # Establish the subscription, let it accept one async tick, then
    # cancel so the finally-block zombie-cleanup runs.
    await _subscribe_for(engine, "demo-tm")

    # While the subscriber was up we recorded a permission request (as
    # would happen in the real forwarder flow before a crash).
    # Now that the subscriber is gone, plant one directly to simulate
    # a prompt that arrived just before the crash.
    await engine.record_permission_request(
        "demo-tm",
        PermissionRequestPayload(
            request_id="abcde",
            tool_name="Bash",
            description="",
            input_preview="",
        ),
    )
    # The worker is offline now (no subscriber). The next subscribe
    # cycle (new forwarder) would clear these; if no new forwarder
    # attaches, a subsequent subscribe-and-leave from a fresh pass
    # still clears. Verify that second subscribe-close path.
    await _subscribe_for(engine, "demo-tm")

    # Pending should be empty — the crashed-session cleanup ran.
    ws = engine.get_worker("demo-tm")
    assert ws is not None
    assert ws.pending_permissions == []
