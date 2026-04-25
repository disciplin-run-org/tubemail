"""Unit tests for the BridgeEngine."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tubemail_hub.bridge.engine import BridgeEngine
from tubemail_hub.bridge.models import PermissionRequestPayload


@pytest.fixture
def engine(tmp_path: Path) -> BridgeEngine:
    return BridgeEngine(data_dir=tmp_path)


async def test_register_and_list(engine: BridgeEngine):
    cursor = await engine.register_worker("leanspecs", "/src/leanspecs")
    assert cursor == ""
    workers = engine.list_workers()
    assert len(workers) == 1
    assert workers[0]["name"] == "leanspecs"
    assert workers[0]["state"] == "idle"
    assert workers[0]["pending_count"] == 0


async def test_inbound_creates_event(engine: BridgeEngine):
    await engine.register_worker("test", "/tmp/test")
    event = await engine.enqueue_inbound("test", "hello", {"from": "orchestrator"})
    assert event.kind == "inbound"
    assert event.content == "hello"
    assert event.meta == {"from": "orchestrator"}
    events = engine.events_since("test")
    assert len(events) == 1
    assert events[0].event_id == event.event_id


async def test_events_since_cursor(engine: BridgeEngine):
    await engine.register_worker("w", "/")
    e1 = await engine.enqueue_inbound("w", "one")
    e2 = await engine.enqueue_inbound("w", "two")
    e3 = await engine.enqueue_inbound("w", "three")
    after_e1 = engine.events_since("w", since=e1.event_id)
    assert [e.event_id for e in after_e1] == [e2.event_id, e3.event_id]


async def test_outbound_recorded(engine: BridgeEngine):
    await engine.register_worker("w", "/")
    event = await engine.record_outbound("w", "progress report")
    assert event.kind == "outbound"
    assert event.content == "progress report"


async def test_permission_request_and_resolve(engine: BridgeEngine):
    await engine.register_worker("w", "/")
    payload = PermissionRequestPayload(
        request_id="abcde",
        tool_name="Bash",
        description="run tests",
        input_preview="pytest",
    )
    await engine.record_permission_request("w", payload)
    pending = engine.list_pending_permissions()
    assert len(pending) == 1
    assert pending[0]["request_id"] == "abcde"
    ws = engine.get_worker("w")
    assert ws is not None
    assert ws.status_state() == "waiting_permission"

    ok = await engine.resolve_permission("w", "abcde", "allow")
    assert ok is True
    assert engine.list_pending_permissions() == []
    assert engine.get_worker("w").status_state() == "idle"


async def test_resolve_unknown_permission_returns_false(engine: BridgeEngine):
    await engine.register_worker("w", "/")
    ok = await engine.resolve_permission("w", "zzzzz", "allow")
    assert ok is False


async def test_register_worker_rejects_path_traversal(engine: BridgeEngine):
    """Worker names that could escape the workers directory must be refused.

    Regression test for the path-traversal finding in the 2026-04-24 security
    review: `_worker_file(name)` composed `workers_dir / f"{name}.json"`
    without validating `name`. A name like `../../tmp/pwned` would have
    written outside the data directory.
    """
    for bad in ["../evil", "..", "../../tmp/x", "a/b", "a\x00b", "", " leading-space", "a" * 100]:
        with pytest.raises(ValueError):
            await engine.register_worker(bad, "/")


async def test_purge_worker_drops_state_and_file(engine: BridgeEngine, tmp_path: Path):
    await engine.register_worker("victim-tm", "/")
    assert engine.get_worker("victim-tm") is not None
    state_file = tmp_path / "workers" / "victim-tm.json"
    assert state_file.exists()

    ok = await engine.purge_worker("victim-tm")
    assert ok is True
    assert engine.get_worker("victim-tm") is None
    assert not state_file.exists(), "state file should be removed"

    # Idempotent: second purge of the same name returns False, no error.
    assert await engine.purge_worker("victim-tm") is False


async def test_purge_worker_handles_orphan_disk_state(engine: BridgeEngine, tmp_path: Path):
    """A state file on disk with no in-memory entry is still purgeable."""
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir(exist_ok=True)
    (workers_dir / "orphan-tm.json").write_text(
        '{"name":"orphan-tm","cwd":"/","registered_at":0,"last_activity":0,'
        '"forwarder_version":"","events":[],"pending_permissions":[],'
        '"exited_cleanly":false}'
    )
    assert engine.get_worker("orphan-tm") is None  # not loaded yet
    ok = await engine.purge_worker("orphan-tm")
    assert ok is True
    assert not (workers_dir / "orphan-tm.json").exists()


async def test_purge_worker_rejects_invalid_name(engine: BridgeEngine):
    """Path-traversal name from the security review is still refused —
    purge_worker must not become an arbitrary-delete primitive."""
    assert await engine.purge_worker("../etc/passwd") is False
    assert await engine.purge_worker("..") is False


async def test_purge_stale_workers_only_drops_old_offline_no_pending(
    engine: BridgeEngine,
):
    import time

    # Three workers, all offline (no SSE subscriber). Backdate two.
    await engine.register_worker("ancient-tm", "/")
    await engine.register_worker("recent-tm", "/")
    await engine.register_worker("ancient-with-pending-tm", "/")

    now = time.time()
    engine.get_worker("ancient-tm").last_activity = now - 100_000
    engine.get_worker("recent-tm").last_activity = now - 100  # young
    ws = engine.get_worker("ancient-with-pending-tm")
    ws.last_activity = now - 100_000
    ws.pending_permissions.append(
        PermissionRequestPayload(
            request_id="hold", tool_name="Bash", description="", input_preview=""
        )
    )

    purged = await engine.purge_stale_workers(max_age_s=86400, now=now)
    # Ancient + offline + no pending → purged.
    assert purged == ["ancient-tm"]
    assert engine.get_worker("ancient-tm") is None
    # Young → kept.
    assert engine.get_worker("recent-tm") is not None
    # Has pending → kept regardless of age (load-bearing state).
    assert engine.get_worker("ancient-with-pending-tm") is not None


async def test_load_all_skips_invalid_name_files(tmp_path: Path):
    """Files whose stem is not a valid worker name must not be trusted.

    Even if an attacker plants a crafted state file under the workers
    directory, it must be skipped (not loaded into in-memory state).
    """
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir()
    # Plant a malicious-looking state file
    (workers_dir / "..evil.json").write_text('{"name": "evil", "cwd": "/"}')
    (workers_dir / "legit.json").write_text(
        '{"name": "legit", "cwd": "/", "registered_at": 0, "last_activity": 0, '
        '"forwarder_version": "", "events": [], "pending_permissions": [], '
        '"exited_cleanly": false}'
    )
    e = BridgeEngine(data_dir=tmp_path)
    # Evil file is skipped; legit file is loaded.
    assert e.get_worker("evil") is None
    assert e.get_worker("legit") is not None


async def test_status_state_busy_after_inbound(engine: BridgeEngine):
    """Unanswered inbound → busy. Keeps orchestrators from giving up too soon."""
    await engine.register_worker("w", "/")
    assert engine.get_worker("w").status_state() == "idle"
    await engine.enqueue_inbound("w", "long-running work order")
    assert engine.get_worker("w").status_state() == "busy"


async def test_status_state_idle_after_reply(engine: BridgeEngine):
    """Inbound followed by outbound reply → idle again."""
    await engine.register_worker("w", "/")
    await engine.enqueue_inbound("w", "work")
    await engine.record_outbound("w", "done")
    assert engine.get_worker("w").status_state() == "idle"


async def test_status_state_pending_permission_beats_busy(engine: BridgeEngine):
    """waiting_permission outranks busy even if the trailing event is inbound."""
    await engine.register_worker("w", "/")
    await engine.enqueue_inbound("w", "work")
    await engine.record_permission_request(
        "w",
        PermissionRequestPayload(
            request_id="p1", tool_name="Bash", description="", input_preview=""
        ),
    )
    assert engine.get_worker("w").status_state() == "waiting_permission"


async def test_persistence_across_reloads(tmp_path: Path):
    e1 = BridgeEngine(data_dir=tmp_path)
    await e1.register_worker("persisted", "/src/persisted")
    await e1.enqueue_inbound("persisted", "hello")

    e2 = BridgeEngine(data_dir=tmp_path)
    workers = e2.list_workers()
    assert len(workers) == 1
    assert workers[0]["name"] == "persisted"
    events = e2.events_since("persisted")
    assert len(events) == 1
    assert events[0].content == "hello"


async def test_subscribe_receives_inbound(engine: BridgeEngine):
    await engine.register_worker("w", "/")

    received: list[dict] = []

    async def reader():
        async for msg in engine.subscribe("w"):
            received.append(msg)
            if len(received) >= 2:
                break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.05)  # let subscriber register
    await engine.enqueue_inbound("w", "one")
    await engine.enqueue_inbound("w", "two")
    await asyncio.wait_for(task, timeout=2.0)

    assert len(received) == 2
    assert received[0]["event"] == "channel_event"
    assert received[0]["data"]["content"] == "one"
    assert received[1]["data"]["content"] == "two"


async def test_subscribe_receives_permission_response(engine: BridgeEngine):
    await engine.register_worker("w", "/")
    payload = PermissionRequestPayload(request_id="abcde", tool_name="Bash")
    await engine.record_permission_request("w", payload)

    received: list[dict] = []

    async def reader():
        async for msg in engine.subscribe("w"):
            received.append(msg)
            break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.05)
    await engine.resolve_permission("w", "abcde", "allow")
    await asyncio.wait_for(task, timeout=2.0)

    assert received[0]["event"] == "permission_response"
    assert received[0]["data"]["request_id"] == "abcde"
    assert received[0]["data"]["behavior"] == "allow"


async def test_interrupt_event_and_fan_out(engine: BridgeEngine):
    await engine.register_worker("w", "/")
    received: list[dict] = []

    async def reader():
        async for msg in engine.subscribe("w"):
            received.append(msg)
            break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.05)
    await engine.send_interrupt("w")
    await asyncio.wait_for(task, timeout=2.0)

    assert received[0]["event"] == "interrupt"


async def test_wait_for_activity_returns_existing(engine: BridgeEngine):
    await engine.register_worker("w", "/")
    e1 = await engine.enqueue_inbound("w", "first")
    new = await engine.wait_for_activity("w", since=None, timeout_s=1.0)
    assert len(new) == 1
    assert new[0].event_id == e1.event_id


async def test_wait_for_activity_blocks_until_event(engine: BridgeEngine):
    await engine.register_worker("w", "/")
    e1 = await engine.enqueue_inbound("w", "first")

    async def delayed_post():
        await asyncio.sleep(0.1)
        await engine.enqueue_inbound("w", "second")

    asyncio.create_task(delayed_post())
    new = await engine.wait_for_activity("w", since=e1.event_id, timeout_s=2.0)
    assert len(new) == 1
    assert new[0].content == "second"


async def test_wait_for_activity_timeout(engine: BridgeEngine):
    await engine.register_worker("w", "/")
    e1 = await engine.enqueue_inbound("w", "first")
    new = await engine.wait_for_activity("w", since=e1.event_id, timeout_s=0.3)
    assert new == []


async def test_goodbye_marks_worker_exited_cleanly(engine: BridgeEngine):
    await engine.register_worker("w", "/")
    # Fresh worker: not exited yet.
    assert engine.list_workers()[0]["exited_cleanly"] is False
    await engine.goodbye_worker("w")
    entry = engine.list_workers()[0]
    assert entry["exited_cleanly"] is True
    assert entry["online"] is False


async def test_unregister_does_not_mark_exited_cleanly(engine: BridgeEngine):
    """Unregister (the crash/lose-connection path) must NOT claim clean exit."""
    await engine.register_worker("w", "/")
    await engine.unregister_worker("w")
    entry = engine.list_workers()[0]
    assert entry["exited_cleanly"] is False
    assert entry["online"] is False


async def test_re_register_resets_exited_cleanly(engine: BridgeEngine):
    """Starting a new session clears any prior clean-exit flag."""
    await engine.register_worker("w", "/")
    await engine.goodbye_worker("w")
    assert engine.list_workers()[0]["exited_cleanly"] is True
    # New session begins.
    await engine.register_worker("w", "/")
    assert engine.list_workers()[0]["exited_cleanly"] is False


async def test_goodbye_on_unknown_worker_is_noop(engine: BridgeEngine):
    # No exception; just quietly does nothing.
    await engine.goodbye_worker("never-existed")
    assert engine.list_workers() == []


# ── Recording flag wiring ────────────────────────────────────────────────


async def test_recording_default_off_for_new_workers(engine: BridgeEngine):
    """The default engine has recording_default=False, so new workers
    register with recording_enabled=False."""
    await engine.register_worker("w", "/")
    assert engine.list_workers()[0]["recording_enabled"] is False


async def test_recording_default_on_starts_recorder(tmp_path: Path):
    """When recording_default=True, registering a new worker starts the
    recorder for it automatically."""
    from tubemail_hub.recorder import RecordingManager
    rec = RecordingManager(tmp_path / "rec")
    eng = BridgeEngine(
        data_dir=tmp_path / "engine", recorder=rec, recording_default=True,
    )
    await eng.register_worker("w", "/")
    assert rec.is_recording("w") is True
    assert eng.list_workers()[0]["recording_enabled"] is True


async def test_set_recording_enabled_toggles_recorder(tmp_path: Path):
    from tubemail_hub.recorder import RecordingManager
    rec = RecordingManager(tmp_path / "rec")
    eng = BridgeEngine(data_dir=tmp_path / "engine", recorder=rec)
    await eng.register_worker("w", "/")
    assert rec.is_recording("w") is False

    ok = await eng.set_recording_enabled("w", True)
    assert ok is True
    assert rec.is_recording("w") is True

    ok = await eng.set_recording_enabled("w", False)
    assert ok is True
    assert rec.is_recording("w") is False


async def test_set_recording_enabled_unknown_worker_returns_false(tmp_path: Path):
    from tubemail_hub.recorder import RecordingManager
    rec = RecordingManager(tmp_path / "rec")
    eng = BridgeEngine(data_dir=tmp_path / "engine", recorder=rec)
    ok = await eng.set_recording_enabled("ghost", True)
    assert ok is False
    assert rec.is_recording("ghost") is False


async def test_recording_flag_persists_across_engine_reload(tmp_path: Path):
    """Toggling recording on, then reloading the engine, preserves the
    per-worker flag."""
    from tubemail_hub.recorder import RecordingManager
    data_dir = tmp_path / "engine"
    rec1 = RecordingManager(tmp_path / "rec")
    eng1 = BridgeEngine(data_dir=data_dir, recorder=rec1)
    await eng1.register_worker("w", "/")
    await eng1.set_recording_enabled("w", True)

    # Simulate restart.
    rec2 = RecordingManager(tmp_path / "rec")
    eng2 = BridgeEngine(data_dir=data_dir, recorder=rec2)
    workers = eng2.list_workers()
    assert workers[0]["recording_enabled"] is True
    # Recorder picks up the worker on reload so files start fresh.
    assert rec2.is_recording("w") is True


async def test_status_state_decays_old_inbound_to_idle(engine: BridgeEngine):
    """Trailing inbound older than BUSY_DECAY_S decays to idle. Without this,
    a worker that handles a work order silently (no channel reply) stays
    "busy" forever — observed on jjstack-tm in the 2026-04-25 investigation.
    """
    import time as _time
    from tubemail_hub.bridge.models import BUSY_DECAY_S
    await engine.register_worker("w", "/")
    event = await engine.enqueue_inbound("w", "do the thing")
    ws = engine.get_worker("w")
    assert ws is not None
    # Recent inbound: still busy.
    assert ws.status_state() == "busy"
    # Pretend it happened way before the decay threshold.
    event.ts = _time.time() - (BUSY_DECAY_S + 60)
    assert ws.status_state() == "idle"


async def test_re_register_keeps_same_recording_session(tmp_path: Path):
    """Re-register does NOT rotate the recording. Forwarder reconnect storms
    after a hub restart would otherwise produce a flood of empty .cast files
    without any benefit — and the manager's claude child is the same process,
    so the asciinema timestamps remain meaningful."""
    from tubemail_hub.recorder import RecordingManager
    rec = RecordingManager(tmp_path / "rec")
    eng = BridgeEngine(data_dir=tmp_path / "engine", recorder=rec)
    await eng.register_worker("w", "/")
    await eng.set_recording_enabled("w", True)
    rec.write("w", b"first session\n")
    files_before = rec.list_files("w")
    assert len(files_before) == 1

    # Re-register simulates a forwarder reconnect — same file should remain.
    await eng.register_worker("w", "/")
    rec.write("w", b"second session\n")
    files_after = rec.list_files("w")
    assert len(files_after) == 1
