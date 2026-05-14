#!/usr/bin/env python3
"""Claude Code Stop hook: relay the assistant's last message to tubemail.

Runs when Claude Code fires a `Stop` event (assistant turn finished).
Reads the hook payload from stdin, extracts the most recent assistant
message text, and POSTs it to the tubemail hub's
`/{worker}/outbound` endpoint so it lands as a `kind=outbound` event
on the worker's timeline.

Why this exists:
    The tubemail-channel plugin exposes a `reply` MCP tool that the
    LLM can call to relay its response. The LLM doesn't always call
    it (it's optional, not auto-fired). When the LLM emits prose +
    a qm-report fence as a normal chat reply, no `reply` invocation
    happens and the response never reaches tubemail's bridge.
    Quartermaster's auto-extract reads from that bridge, so the
    fence becomes invisible despite being syntactically perfect.

    This hook closes that gap: every Stop event POSTs the assistant's
    last message text, regardless of whether the LLM remembered to
    call `reply`.

Durability (added 2026-05-07 after queue 187 dogfooded the failure
mode where assistant replies vanished on a transient hub blip):
    - **Retry with backoff.** Up to TUBEMAIL_STOP_HOOK_RETRIES attempts
      (default 3) with 0.5s, 2s, 5s backoff. Single transient blips
      (hub restarting, brief network cut) no longer drop the message.
    - **Local spool.** When all retries fail, the unposted body is
      written to ~/.claude/tubemail-spool/<worker>/<ts>-<sha>.json
      with mode 0600. The spool is drained oldest-first on the next
      Stop event, before the new event is posted. Capped at 100
      entries; oldest is evicted with a stderr WARNING when full.
    - **Exit 2 on total failure.** When BOTH the POST retry chain AND
      the spool write fail (e.g. disk full + hub down), the hook
      exits 2 so Claude Code surfaces the failure in the session UI
      rather than hiding the loss. This violates the original
      best-effort contract intentionally — silent loss is worse than
      a noisy hook failure.
    - **Optional verify-readback.** When TUBEMAIL_STOP_HOOK_VERIFY=1,
      the hook re-fetches the just-posted event from the hub to
      confirm it actually persisted. Doubles request count, so
      default-off; the durability test runs with it on.

Skip-on-double-fire:
    If the LLM DID call `reply` during this turn, both paths fire
    and we'd double-record. The hook tags its event with
    `meta.kind="stop_relay"` so consumers can dedup if needed.

Activation:
    Only runs when `TM_WORKER_NAME` is set (i.e. inside a tubemail-
    channel session). On non-tubemail sessions the hook self-skips
    and exits 0.

Settings.json registration:
    {"hooks": {"Stop": [{"hooks": [{"type": "command",
        "command": "python3 /path/to/post_stop_relay.py"}]}]}}
"""

from __future__ import annotations

# Standard Libraries
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


HUB_URL_DEFAULT = "http://localhost:8001"
TIMEOUT_S = 5.0
DEFAULT_RETRIES = 3
# Backoff schedule between attempts. Index N is the wait BEFORE attempt N+1.
# So with 3 attempts: try, sleep BACKOFF_S[0], try, sleep BACKOFF_S[1], try.
# Length matches DEFAULT_RETRIES-1 plus a tail entry that's used if a caller
# overrides retries to a larger number.
BACKOFF_S = (0.5, 2.0, 5.0)
SPOOL_CAP = 100


def _stderr(msg: str) -> None:
    """Log to stderr. Claude Code captures stderr for hook diagnostics
    (visible in session UI when the hook also exits non-zero). stdout is
    reserved for hook control JSON which we don't emit."""
    sys.stderr.write(f"[tubemail-stop-relay] {msg}\n")


def _retries_from_env() -> int:
    raw = os.environ.get("TUBEMAIL_STOP_HOOK_RETRIES", "").strip()
    if not raw:
        return DEFAULT_RETRIES
    try:
        n = int(raw)
    except ValueError:
        _stderr(f"invalid TUBEMAIL_STOP_HOOK_RETRIES={raw!r}; using {DEFAULT_RETRIES}")
        return DEFAULT_RETRIES
    return max(1, n)


def _backoff_s(attempt: int) -> float:
    """Return the wait BEFORE the next attempt (0-indexed)."""
    if attempt < 0:
        return 0.0
    if attempt < len(BACKOFF_S):
        return BACKOFF_S[attempt]
    # If retries > len(BACKOFF_S), keep using the last backoff value.
    return BACKOFF_S[-1]


def _extract_last_assistant_text(payload: dict) -> str | None:
    """Pull the most recent assistant message text out of the Stop event.

    Claude Code's Stop hook payload includes a `transcript_path` pointing at
    a JSONL file with the conversation, OR an inline `messages` array, OR
    both. We try messages first, then transcript path. Schema isn't fully
    locked across Claude Code versions, so we're tolerant.
    """
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            text = _content_to_text(content)
            if text:
                return text
            #end if
        #end for
    #end if

    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path:
        try:
            with open(transcript_path, encoding="utf-8") as f:
                lines = [line for line in f if line.strip()]
        except OSError as err:
            _stderr(f"transcript read failed: {err}")
            return None
        #end try
        for line in reversed(lines):
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            #end try
            msg = evt.get("message") if isinstance(evt, dict) else None
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                text = _content_to_text(msg.get("content"))
                if text:
                    return text
                #end if
            #end if
            if isinstance(evt, dict) and evt.get("role") == "assistant":
                text = _content_to_text(evt.get("content"))
                if text:
                    return text
                #end if
            #end if
        #end for
    #end if

    return None


def _content_to_text(content) -> str | None:
    """Claude Code messages can have content as a string or a list of
    blocks (`{"type": "text", "text": "..."}`). Concatenate text blocks
    in order; ignore tool-use / tool-result blocks (those are structured
    events, not the assistant's prose)."""
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("text", "thinking"):
                # 'thinking' blocks are extended-thinking output; include
                # them so the relay captures the full reasoning text. The
                # LLM's qm-report fence usually lives in the 'text' block
                # but tools/skills sometimes emit fences from thinking
                # context — better to over-include than miss a fence.
                t = block.get("text") or block.get("thinking")
                if isinstance(t, str) and t:
                    parts.append(t)
                #end if
            #end if
        #end for
        joined = "\n".join(parts).strip()
        return joined or None
    #end if
    return None


def _build_body(text: str) -> bytes:
    return json.dumps({"text": text, "meta": {"kind": "stop_relay"}}).encode("utf-8")


def _verify_event_persisted(
    hub_url: str, worker: str, secret: str, event_id: str
) -> bool:
    """Re-fetch the worker's recent events and confirm `event_id` is present.
    Used only when TUBEMAIL_STOP_HOOK_VERIFY=1."""
    url = f"{hub_url.rstrip('/')}/tubemail/{worker}/events?limit=10"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {secret}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read()
    except urllib.error.HTTPError as err:
        _stderr(f"verify GET {url} HTTP {err.code}: {err.reason}")
        return False
    except (urllib.error.URLError, OSError) as err:
        _stderr(f"verify GET {url} failed: {err}")
        return False
    #end try
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError) as err:
        _stderr(f"verify GET {url} returned non-JSON: {err}")
        return False
    #end try
    events = parsed.get("events") if isinstance(parsed, dict) else None
    if not isinstance(events, list):
        _stderr(f"verify GET {url}: missing 'events' array")
        return False
    #end if
    for e in events:
        if isinstance(e, dict) and e.get("event_id") == event_id:
            return True
        #end if
    #end for
    _stderr(f"verify: event_id={event_id} not found in last {len(events)} events")
    return False


def _post_outbound(
    hub_url: str,
    worker: str,
    secret: str,
    text: str,
    *,
    retries: int | None = None,
    verify: bool | None = None,
    sleep_fn=time.sleep,
) -> bool:
    """POST the relay event with retry+backoff. Returns True on 2xx, False if
    every attempt fails. Never raises — silent-failure was the whole problem
    we're solving, but the hook's main() needs to differentiate success/failure
    to drive the spool.

    `retries` defaults to TUBEMAIL_STOP_HOOK_RETRIES env (or DEFAULT_RETRIES).
    `verify` defaults to TUBEMAIL_STOP_HOOK_VERIFY env. `sleep_fn` is injectable
    so tests don't actually wait through the backoff.
    """
    if retries is None:
        retries = _retries_from_env()
    #end if
    if verify is None:
        verify = os.environ.get("TUBEMAIL_STOP_HOOK_VERIFY", "").strip() == "1"
    #end if
    body = _build_body(text)
    return _post_body_with_retry(
        hub_url, worker, secret, body, retries=retries,
        verify=verify, sleep_fn=sleep_fn,
    )


def _post_body_with_retry(
    hub_url: str,
    worker: str,
    secret: str,
    body: bytes,
    *,
    retries: int,
    verify: bool,
    sleep_fn=time.sleep,
) -> bool:
    """POST a pre-built body. Splitting this out from `_post_outbound` lets
    spool-drain re-use it without re-building from text."""
    url = f"{hub_url.rstrip('/')}/tubemail/{worker}/outbound"
    last_event_id: str | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {secret}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                # urlopen raises HTTPError on 4xx/5xx by default, so reaching
                # here means 2xx (1xx/3xx are auto-followed). The dead
                # `resp.status >= 400` check from the previous version is
                # gone — there's nothing to check.
                if verify:
                    # Only read the response body when we need it; reading is
                    # cheap but keeps tests with minimal FakeResp stubs working.
                    try:
                        raw = resp.read()
                        parsed = json.loads(raw)
                        last_event_id = parsed.get("event_id") if isinstance(parsed, dict) else None
                    except (json.JSONDecodeError, ValueError, AttributeError, OSError):
                        last_event_id = None
                    #end try
                #end if
            #end with
        except urllib.error.HTTPError as err:
            _stderr(
                f"WARNING: POST {url} attempt {attempt + 1}/{retries} "
                f"HTTP {err.code}: {err.reason}"
            )
            if attempt < retries - 1:
                sleep_fn(_backoff_s(attempt))
            #end if
            continue
        except (urllib.error.URLError, OSError) as err:
            _stderr(
                f"WARNING: POST {url} attempt {attempt + 1}/{retries} "
                f"failed: {err}"
            )
            if attempt < retries - 1:
                sleep_fn(_backoff_s(attempt))
            #end if
            continue
        #end try

        # POST succeeded. If verify is on, confirm the event landed.
        if verify and last_event_id:
            if not _verify_event_persisted(hub_url, worker, secret, last_event_id):
                _stderr(
                    f"WARNING: verify failed for event_id={last_event_id} on "
                    f"attempt {attempt + 1}/{retries} — retrying"
                )
                if attempt < retries - 1:
                    sleep_fn(_backoff_s(attempt))
                #end if
                continue
            #end if
        #end if
        return True
    #end for
    return False


def _spool_root() -> Path:
    # TUBEMAIL_STOP_HOOK_SPOOL_DIR overrides the default. Tests rely on this
    # to sandbox the per-worker spool to a tmp dir; production never sets it.
    override = os.environ.get("TUBEMAIL_STOP_HOOK_SPOOL_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude" / "tubemail-spool"


def _spool_dir(worker: str) -> Path:
    # Worker names are validated upstream by the channel registration path;
    # still, normalize defensively to prevent any path-traversal weirdness.
    safe = worker.replace("/", "_").replace("..", "_")
    return _spool_root() / safe


def _spool_event(worker: str, body: bytes) -> bool:
    """Persist `body` to the per-worker spool directory. Returns True on
    success. Caps the spool at SPOOL_CAP entries — drops oldest with a
    stderr WARNING when full."""
    try:
        d = _spool_dir(worker)
        d.mkdir(parents=True, exist_ok=True)
        # Tighten directory perms (mkdir doesn't honor mode if the parent
        # already existed at a wider mode). Best-effort.
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        #end try

        existing = sorted(d.glob("*.json"))
        while len(existing) >= SPOOL_CAP:
            victim = existing.pop(0)
            _stderr(
                f"WARNING: spool full at {SPOOL_CAP}; dropping oldest "
                f"{victim.name}"
            )
            try:
                victim.unlink()
            except OSError as err:
                _stderr(f"failed to drop spool victim {victim}: {err}")
            #end try
        #end while

        ts = f"{time.time():.6f}"
        sha = hashlib.sha256(body).hexdigest()[:12]
        path = d / f"{ts}-{sha}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(body)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        #end try
        os.replace(tmp, path)
        return True
    except OSError as err:
        _stderr(f"spool write failed: {err}")
        return False


def _drain_spool(
    hub_url: str,
    worker: str,
    secret: str,
    *,
    retries: int,
    verify: bool,
    sleep_fn=time.sleep,
) -> int:
    """Drain spooled events oldest-first. Stop on the first failure so we
    don't burn the full retry chain on every entry when the hub is still
    down. Returns count of successfully posted entries."""
    d = _spool_dir(worker)
    if not d.is_dir():
        return 0
    drained = 0
    for path in sorted(d.glob("*.json")):
        try:
            body = path.read_bytes()
        except OSError as err:
            _stderr(f"spool read failed for {path.name}: {err}")
            continue
        #end try
        ok = _post_body_with_retry(
            hub_url, worker, secret, body,
            retries=retries, verify=verify, sleep_fn=sleep_fn,
        )
        if not ok:
            # Hub is still unreachable; preserve remaining spool for the next
            # Stop. Don't keep retrying through the rest of the directory.
            break
        #end if
        try:
            path.unlink()
        except OSError as err:
            _stderr(f"spool unlink failed for {path.name}: {err}")
        #end try
        drained += 1
    #end for
    if drained:
        _stderr(f"drained {drained} spooled event(s) for {worker}")
    #end if
    return drained


def main() -> int:
    worker = os.environ.get("TM_WORKER_NAME", "").strip()
    if not worker:
        # Not a tubemail-channel session — silently skip.
        return 0
    #end if

    secret = os.environ.get("TUBEMAIL_SECRET", "").strip()
    if not secret:
        _stderr("TUBEMAIL_SECRET not set; cannot POST relay event")
        return 0
    #end if

    # Use `or HUB_URL_DEFAULT` (not get(name, default)) so an explicitly-set
    # EMPTY env var falls back to the default. Some tubemail launches export
    # TUBEMAIL_HUB_URL='' instead of leaving it unset — without this guard,
    # the script builds a relative URL like '/tubemail/{worker}/outbound'
    # and urllib raises 'unknown url type', exit 1, and Claude Code surfaces
    # the failure on stop.
    hub_url = (os.environ.get("TUBEMAIL_HUB_URL") or HUB_URL_DEFAULT).strip()
    if not hub_url:
        hub_url = HUB_URL_DEFAULT
    #end if

    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    #end if

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as err:
        _stderr(f"stdin not JSON: {err}")
        return 0
    #end try

    text = _extract_last_assistant_text(payload)
    if not text:
        _stderr("no assistant text found in Stop event")
        return 0
    #end if

    retries = _retries_from_env()
    verify = os.environ.get("TUBEMAIL_STOP_HOOK_VERIFY", "").strip() == "1"

    # Drain any prior unposted events first. If the hub is still down we'll
    # bail out of the drain on the first failure and try the new event below
    # (which will also fail and end up spooled). Drain-before-post preserves
    # event ordering when the hub returns.
    _drain_spool(
        hub_url, worker, secret,
        retries=retries, verify=verify,
    )

    body = _build_body(text)
    posted = _post_body_with_retry(
        hub_url, worker, secret, body,
        retries=retries, verify=verify,
    )
    if posted:
        return 0
    #end if

    # All retries failed. Spool to disk so the next Stop event can drain it.
    spooled = _spool_event(worker, body)
    if spooled:
        _stderr(
            "POST failed after retries; event spooled — will drain on next Stop"
        )
        # Spool succeeded means the event is durable. Exit 0; we have nothing
        # surfaced to the user-facing UI yet but the loss is averted.
        return 0
    #end if

    # POST failed AND spool failed. This is the worst case — exit 2 so
    # Claude Code surfaces the failure rather than swallowing the loss.
    _stderr("POST failed AND spool write failed — assistant reply LOST")
    return 2


if __name__ == "__main__":
    sys.exit(main())
