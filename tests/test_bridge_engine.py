"""Unit tests for the BridgeEngine."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tubemail_hub.bridge.engine import BridgeEngine, _new_event_id
from tubemail_hub.bridge.models import PermissionRequestPayload, WorkerEvent


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


async def test_wait_for_matching_event_skips_unrelated(engine: BridgeEngine):
    """Regression test: an unrelated event must not satisfy a request that's
    waiting for a specific outbound reply.

    Historical bug: tm_reconnect_mcp called wait_for_activity(since=None)
    which returned existing events instantly; if a parallel orchestrator
    fired tm_screenshot during the reconnect, the screenshot's events
    would wake the wait, the tool would scan and find no matching
    reconnect_mcp_result, and return a false 30s timeout — even though
    the manager later posted a successful result.
    """
    await engine.register_worker("w", "/")

    # Pre-existing stale events (e.g. an old health_response from
    # yesterday). With the old wait_for_activity(since=None) these
    # would short-circuit the wait.
    await engine.record_outbound("w", "stale", {"kind": "health_response"})
    inbound = await engine.enqueue_inbound("w", "do thing", {"kind": "do_thing"})

    async def post_intervening_then_real_reply() -> None:
        # Unrelated noise that wakes wait_for_activity but doesn't match.
        await asyncio.sleep(0.05)
        await engine.record_outbound("w", "screenshot data", {"kind": "screenshot"})
        # Then the actual reply we're waiting for.
        await asyncio.sleep(0.05)
        await engine.record_outbound(
            "w", '{"ok": true}', {"kind": "reconnect_mcp_result", "ok": True},
        )

    asyncio.create_task(post_intervening_then_real_reply())

    result = await engine.wait_for_matching_event(
        "w",
        since=inbound.event_id,
        match=lambda e: (
            e.kind == "outbound" and e.meta.get("kind") == "reconnect_mcp_result"
        ),
        timeout_s=2.0,
    )
    assert result is not None, "must have found the reconnect_mcp_result"
    assert result.meta.get("kind") == "reconnect_mcp_result"


async def test_wait_for_matching_event_returns_none_on_timeout(
    engine: BridgeEngine,
):
    """If the matching event never arrives, return None — even if other
    activity (intervening events) keeps the worker busy."""
    await engine.register_worker("w", "/")
    inbound = await engine.enqueue_inbound("w", "do thing", {"kind": "do_thing"})

    async def keep_posting_unrelated() -> None:
        for _ in range(10):
            await asyncio.sleep(0.05)
            await engine.record_outbound("w", "noise", {"kind": "screenshot"})

    asyncio.create_task(keep_posting_unrelated())

    result = await engine.wait_for_matching_event(
        "w",
        since=inbound.event_id,
        match=lambda e: e.meta.get("kind") == "reconnect_mcp_result",
        timeout_s=0.6,
    )
    assert result is None


async def test_wait_for_matching_event_does_not_return_pre_since_match(
    engine: BridgeEngine,
):
    """Regression: a matching event from BEFORE the `since` cursor must
    not be returned. This is what made tm_health return stale data."""
    await engine.register_worker("w", "/")
    # Old reply already in the timeline (the "stale data" trap).
    await engine.record_outbound(
        "w", '{"old": true}', {"kind": "health_response"},
    )
    inbound = await engine.enqueue_inbound("w", "health_check", {"kind": "health_check"})

    result = await engine.wait_for_matching_event(
        "w",
        since=inbound.event_id,
        match=lambda e: (
            e.kind == "outbound" and e.meta.get("kind") == "health_response"
        ),
        timeout_s=0.3,
    )
    # The stale outbound predates `since` — must not be returned.
    assert result is None


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


async def test_status_state_decays_quiet_worker_with_post_inbound_activity(
    engine: BridgeEngine,
):
    """When a worker showed post-inbound activity (e.g. context-pct
    heartbeats while Claude generated tokens) but those heartbeats then
    went quiet for longer than BUSY_QUIET_S, treat as idle even though
    the inbound itself is still inside the 10-minute hard cap.

    Caught 2026-04-26: quartermaster-tm sat at `busy` for the full 10
    minutes after each silently-completed work order even though
    context-pct stopped firing within seconds of the actual completion.
    """
    import time as _time
    from tubemail_hub.bridge.models import BUSY_QUIET_S
    await engine.register_worker("w", "/")
    event = await engine.enqueue_inbound("w", "do the thing")
    ws = engine.get_worker("w")
    assert ws is not None

    # Worker pushed a context-pct heartbeat 5 s after the inbound (still
    # actively generating tokens at that point).
    ws.last_activity = event.ts + 5.0

    # No heartbeats since. Pretend `now` is BUSY_QUIET_S + 10s past the
    # last heartbeat (still well under BUSY_DECAY_S since the inbound).
    # Drive the clock by shifting both the event and last_activity into
    # the past instead of patching time.time().
    quiet_for = BUSY_QUIET_S + 10.0
    event.ts = _time.time() - (5.0 + quiet_for)
    ws.last_activity = _time.time() - quiet_for

    assert ws.status_state() == "idle", (
        "post-inbound heartbeats stopped >BUSY_QUIET_S ago — must decay early"
    )


async def test_status_state_busy_while_post_inbound_activity_is_fresh(
    engine: BridgeEngine,
):
    """Inverse of the quiet-decay test: while heartbeats are still fresh
    (within BUSY_QUIET_S), keep reporting busy. Otherwise an actively-
    generating worker would flap to idle every time the algorithm runs."""
    import time as _time
    await engine.register_worker("w", "/")
    event = await engine.enqueue_inbound("w", "do the thing")
    ws = engine.get_worker("w")
    assert ws is not None

    # Inbound 30 s ago, heartbeat 5 s ago — well under BUSY_QUIET_S.
    event.ts = _time.time() - 30.0
    ws.last_activity = _time.time() - 5.0
    assert ws.status_state() == "busy"


async def test_status_state_observed_active_overrides_timeline_decay(
    engine: BridgeEngine,
):
    """When the manager has pushed a fresh active-state observation,
    `status_state` honors it regardless of what the event timeline
    says. This is the manager-as-source-of-truth path added 2026-04-26.
    """
    await engine.register_worker("w", "/")
    # Trailing inbound from 5 min ago — timeline alone would say busy.
    event = await engine.enqueue_inbound("w", "do the thing")
    import time as _time
    event.ts = _time.time() - 300.0

    # But the manager has just pushed `is_active=False` — claude is at
    # the prompt. That observation must win.
    changed = await engine.update_active_state("w", False)
    assert changed is True
    ws = engine.get_worker("w")
    assert ws is not None
    assert ws.status_state() == "idle"

    # And the inverse: stale inbound (>10 min) but manager says active.
    # Observation still wins — claude is generating tokens.
    event.ts = _time.time() - 1200.0
    await engine.update_active_state("w", True)
    assert ws.status_state() == "busy"


async def test_status_state_observed_active_decays_when_stale(
    engine: BridgeEngine,
):
    """If the manager hasn't pushed in OBSERVED_ACTIVE_FRESHNESS_S, the
    observation goes stale and `status_state` falls back to event-
    timeline decay. Catches the manager-disconnect / dead-manager case."""
    import time as _time
    from tubemail_hub.bridge.models import OBSERVED_ACTIVE_FRESHNESS_S
    await engine.register_worker("w", "/")
    event = await engine.enqueue_inbound("w", "do the thing")
    ws = engine.get_worker("w")
    assert ws is not None

    # Manager once said "busy" but hasn't reported in a long time.
    await engine.update_active_state("w", True)
    ws.observed_active_at = _time.time() - (OBSERVED_ACTIVE_FRESHNESS_S + 30.0)

    # Trailing inbound is fresh, so the timeline-fallback says busy.
    event.ts = _time.time() - 10.0
    ws.last_activity = event.ts
    assert ws.status_state() == "busy"

    # And the timeline-fallback's stale-inbound branch still works.
    event.ts = _time.time() - 1200.0
    ws.last_activity = event.ts
    assert ws.status_state() == "idle"


async def test_status_state_legacy_manager_no_post_inbound_signal(
    engine: BridgeEngine,
):
    """Workers running the legacy (pre-context-pct) manager produce no
    post-inbound activity that advances last_activity. They must keep
    the BUSY_DECAY_S hard cap as their only decay mechanism — the
    BUSY_QUIET_S branch must NOT fire when last_activity == inbound.ts.
    """
    import time as _time
    from tubemail_hub.bridge.models import BUSY_QUIET_S
    await engine.register_worker("w", "/")
    event = await engine.enqueue_inbound("w", "do the thing")
    ws = engine.get_worker("w")
    assert ws is not None

    # Drive both into the past by BUSY_QUIET_S + buffer, but keep them
    # equal — the legacy-manager case where the inbound itself was the
    # last thing that touched last_activity.
    elapsed = BUSY_QUIET_S + 30.0
    event.ts = _time.time() - elapsed
    ws.last_activity = event.ts  # no post-inbound advance

    # Still inside BUSY_DECAY_S (10 min) and NO post-inbound activity →
    # must remain busy. The QUIET_S branch is gated on
    # `last_activity > inbound.ts` and must skip this case.
    assert ws.status_state() == "busy"


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


# ── sweep_stale_permissions ─────────────────────────────────────────────


async def test_sweep_drops_proven_resolved_pending(engine: BridgeEngine):
    """A pending permission whose request_event is followed by a worker
    outbound event must be dropped — the LLM cannot have produced output
    while still blocked on a permission gate, so the resolution clearly
    happened locally and just failed to reach the hub."""
    await engine.register_worker("w", "/")
    payload = PermissionRequestPayload(request_id="stuck1", tool_name="Bash")
    await engine.record_permission_request("w", payload)
    # Backdate the request so the grace window doesn't protect it.
    ws = engine._workers["w"]
    for ev in ws.events:
        if ev.kind == "permission_request":
            ev.ts -= 3600  # 1h ago
    # Worker keeps replying — append the outbound DIRECTLY to bypass the
    # auto-sweep wired into record_outbound; this test pins the explicit
    # sweep admin path. The auto-sweep is covered separately by
    # test_outbound_auto_sweeps_proven_resolved_pending below.
    import time as _t
    ws.events.append(WorkerEvent(
        event_id=_new_event_id(),
        ts=_t.time(),
        kind="outbound",
        content="i continued past the gate",
        meta={},
    ))
    ws.last_activity = ws.events[-1].ts
    assert len(ws.pending_permissions) == 1

    dropped = await engine.sweep_stale_permissions("w")
    assert dropped == 1
    assert ws.pending_permissions == []


async def test_sweep_keeps_genuinely_pending(engine: BridgeEngine):
    """If no worker outbound event exists after the request, the pending
    entry is still real — the worker IS blocked. Don't drop it."""
    await engine.register_worker("w", "/")
    payload = PermissionRequestPayload(request_id="real", tool_name="Bash")
    await engine.record_permission_request("w", payload)
    ws = engine._workers["w"]
    for ev in ws.events:
        if ev.kind == "permission_request":
            ev.ts -= 3600
    # No outbound after the request — worker is still waiting.
    dropped = await engine.sweep_stale_permissions("w")
    assert dropped == 0
    assert len(ws.pending_permissions) == 1


async def test_sweep_respects_grace_window(engine: BridgeEngine):
    """A request from less than _SWEEP_GRACE_S ago must be kept even
    when an outbound has landed since — the timing is too close to call
    safely (could be a race between a request append and a stop_relay).
    """
    await engine.register_worker("w", "/")
    payload = PermissionRequestPayload(request_id="fresh", tool_name="Bash")
    await engine.record_permission_request("w", payload)
    await engine.record_outbound("w", "racing reply")
    # No backdating — request ts is current, well within grace window.
    dropped = await engine.sweep_stale_permissions("w")
    assert dropped == 0
    assert len(engine._workers["w"].pending_permissions) == 1


async def test_sweep_drops_orphan_after_grace(engine: BridgeEngine):
    """A pending entry whose permission_request event is missing from
    the timeline (state corruption, or an old entry whose event has
    aged out) gets dropped once the worker has been quiet for the
    grace window. Otherwise the entry is unanswerable forever."""
    await engine.register_worker("w", "/")
    ws = engine._workers["w"]
    # Inject a pending entry directly with no matching event in `events`.
    ws.pending_permissions.append(
        PermissionRequestPayload(request_id="orphan", tool_name="Bash"),
    )
    # Backdate last_activity past the grace window.
    ws.last_activity = ws.last_activity - 3600
    dropped = await engine.sweep_stale_permissions("w")
    assert dropped == 1
    assert ws.pending_permissions == []


async def test_sweep_keeps_orphan_within_grace(engine: BridgeEngine):
    """A brand-new orphan (request_event hasn't been appended yet, or
    last_activity is fresh) is kept — could be a race, not a stuck
    entry. Dropping it would race with legitimate registrations."""
    await engine.register_worker("w", "/")
    ws = engine._workers["w"]
    ws.pending_permissions.append(
        PermissionRequestPayload(request_id="new", tool_name="Bash"),
    )
    # last_activity is `now` from register_worker — well within grace.
    dropped = await engine.sweep_stale_permissions("w")
    assert dropped == 0
    assert len(ws.pending_permissions) == 1


async def test_sweep_drops_pending_after_heartbeat_silence(engine: BridgeEngine):
    """Incident QM #416 (2026-05-18, architrix-tm): a worker fires a Bash
    permission prompt and its Claude session goes stale before producing
    any further outbound. The forwarder subscription stays alive (so the
    connection-drop sweep can't fire), the request has no later outbound
    (so the event-timeline sweep can't fire), and the prompt sits in
    pending_permissions forever — visible as `waiting_permission` in the
    web UI even though the worker is idle.

    The heartbeat-threshold signal must drop the entry once the worker
    has been silent for `_SWEEP_HEARTBEAT_S`. last_activity gets stuck
    on the request itself (no later events), so `now - last_activity`
    is effectively the request's age.
    """
    await engine.register_worker("w", "/")
    payload = PermissionRequestPayload(request_id="zombie", tool_name="Bash")
    await engine.record_permission_request("w", payload)
    ws = engine._workers["w"]
    # Push request_event AND last_activity past the heartbeat threshold.
    # No outbound after — the event-timeline path can't drop this.
    old_ts = ws.last_activity - (engine._SWEEP_HEARTBEAT_S + 60.0)
    for ev in ws.events:
        if ev.kind == "permission_request":
            ev.ts = old_ts
    ws.last_activity = old_ts
    assert len(ws.pending_permissions) == 1

    dropped = await engine.sweep_stale_permissions("w")
    assert dropped == 1
    assert ws.pending_permissions == []


async def test_sweep_keeps_pending_within_heartbeat(engine: BridgeEngine):
    """A pending entry from inside the heartbeat window stays — the
    worker may legitimately be blocked on a slow human-approval prompt.
    Only entries past the threshold get the new sweep treatment."""
    await engine.register_worker("w", "/")
    payload = PermissionRequestPayload(request_id="fresh", tool_name="Bash")
    await engine.record_permission_request("w", payload)
    # No backdating — request and last_activity are both `now`, well
    # within the heartbeat threshold.
    dropped = await engine.sweep_stale_permissions("w")
    assert dropped == 0
    assert len(engine._workers["w"].pending_permissions) == 1


async def test_sweep_heartbeat_constant_is_documented_minutes(engine: BridgeEngine):
    """The default heartbeat threshold per QM #419's proposal is 15
    minutes. Pin the constant so a future tweak doesn't silently
    shorten the window for human-attended prompts (which the WO
    explicitly carved out — "permission prompts that genuinely need an
    answer sit pending for many minutes")."""
    assert engine._SWEEP_HEARTBEAT_S == 900.0


async def test_sweep_all_only_reports_workers_that_changed(engine: BridgeEngine):
    """The aggregated sweeper returns only workers where it actually
    dropped something — clean workers are omitted to keep the response
    readable when scanning a large fleet."""
    await engine.register_worker("clean", "/")
    await engine.register_worker("stuck", "/")
    payload = PermissionRequestPayload(request_id="x", tool_name="Bash")
    await engine.record_permission_request("stuck", payload)
    for ev in engine._workers["stuck"].events:
        if ev.kind == "permission_request":
            ev.ts -= 3600
    # Append outbound directly so record_outbound's auto-sweep doesn't
    # claim the drop before the admin call gets to count it. The
    # auto-sweep behavior is pinned by its own dedicated test.
    import time as _t
    ws_stuck = engine._workers["stuck"]
    ws_stuck.events.append(WorkerEvent(
        event_id=_new_event_id(),
        ts=_t.time(),
        kind="outbound",
        content="moved on",
        meta={},
    ))
    ws_stuck.last_activity = ws_stuck.events[-1].ts

    results = await engine.sweep_stale_permissions_all()
    assert results == {"stuck": 1}
    # Verify clean worker was actually scanned, just not reported.
    assert engine._workers["clean"].pending_permissions == []


async def test_sweeper_runs_at_engine_construction(tmp_path: Path):
    """When the hub starts, _load_all reads workers from disk; the
    sweeper must immediately drop proven-stuck entries so the very first
    tm_status / tm_pending_permissions response is already clean. Without
    this, the hub's startup window leaks the same stale state until
    something else triggers cleanup."""
    import json
    import time as _time
    # Hand-craft a worker file with a stuck pending permission and a
    # subsequent outbound — the exact shape of the leanspecs-code-tm bug.
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir()
    request_ts = _time.time() - 3600
    outbound_ts = request_ts + 60
    state = {
        "name": "stuck-worker",
        "registered_at": request_ts - 100,
        "last_activity": outbound_ts,
        "pending_permissions": [
            {"request_id": "ghost", "tool_name": "Bash"},
        ],
        "events": [
            {
                "event_id": "ev1", "ts": request_ts, "kind": "permission_request",
                "content": "Bash", "meta": {"request_id": "ghost"},
            },
            {
                "event_id": "ev2", "ts": outbound_ts, "kind": "outbound",
                "content": "moved on", "meta": {},
            },
        ],
    }
    (workers_dir / "stuck-worker.json").write_text(json.dumps(state))

    fresh_engine = BridgeEngine(data_dir=tmp_path)
    assert fresh_engine._workers["stuck-worker"].pending_permissions == []
    # And the cleanup persisted to disk, not just to memory.
    on_disk = json.loads((workers_dir / "stuck-worker.json").read_text())
    assert on_disk["pending_permissions"] == []


# ── Auto-sweep on outbound (class-boundary regression for QM #243-followup) ──
#
# Class of failure: a permission gets resolved locally (auto-approve hook,
# user prompt, etc.) without the resolution reaching the hub — typically
# because the local hook short-circuits Claude Code's permission flow and
# no `permission` notification is ever emitted to the channel. The hub's
# pending_permissions entry then sits forever until someone restarts the
# hub or calls tm_sweep_stale_permissions.
#
# The fix wires the existing sweeper into `record_outbound`: any outbound
# event is proof the LLM passed the prior permission gate, so any pending
# entry that pre-dates the outbound by more than the grace window MUST be
# dropped automatically. These tests pin that contract regardless of HOW
# the resolution failed to round-trip.


async def test_outbound_auto_sweeps_proven_resolved_pending(
    engine: BridgeEngine,
):
    """Record an outbound that postdates a stuck request by more than
    the grace window. Pending must clear with no admin call."""
    await engine.register_worker("w", "/")
    payload = PermissionRequestPayload(request_id="stuck", tool_name="Bash")
    await engine.record_permission_request("w", payload)
    ws = engine._workers["w"]
    for ev in ws.events:
        if ev.kind == "permission_request":
            ev.ts -= 3600
    assert len(ws.pending_permissions) == 1

    await engine.record_outbound("w", "i moved on")

    assert ws.pending_permissions == []


async def test_outbound_auto_sweep_respects_grace_window(
    engine: BridgeEngine,
):
    """An outbound emitted within the grace window of a fresh request
    must NOT trigger eviction — could be a parallel reply racing a real
    pending prompt. The grace window inside _sweep_stale_for_worker is
    the safety net against false positives."""
    await engine.register_worker("w", "/")
    payload = PermissionRequestPayload(request_id="real", tool_name="Bash")
    await engine.record_permission_request("w", payload)
    # No backdating — request is current. record_outbound's auto-sweep
    # should see the request inside the grace window and keep it.
    await engine.record_outbound("w", "parallel reply")
    assert len(engine._workers["w"].pending_permissions) == 1


async def test_outbound_auto_sweep_no_op_when_no_pending(
    engine: BridgeEngine,
):
    """When pending_permissions is empty, record_outbound must not pay
    the cost of scanning the event timeline. Verified indirectly: the
    method runs without error on a worker with no pending entries, and
    leaves the new event in place."""
    await engine.register_worker("w", "/")
    await engine.record_outbound("w", "first reply")
    ws = engine._workers["w"]
    assert ws.pending_permissions == []
    assert any(e.kind == "outbound" and e.content == "first reply" for e in ws.events)


async def test_outbound_auto_sweep_persists_to_disk(
    tmp_path: Path,
):
    """The auto-sweep must persist its cleanup to the worker JSON file —
    otherwise a hub restart could re-load the stuck entry from disk and
    the symptom recurs. Verified by re-loading a fresh engine and
    checking the in-memory state."""
    import time as _time
    eng = BridgeEngine(data_dir=tmp_path)
    await eng.register_worker("w", "/")
    payload = PermissionRequestPayload(request_id="x", tool_name="Bash")
    await eng.record_permission_request("w", payload)
    for ev in eng._workers["w"].events:
        if ev.kind == "permission_request":
            ev.ts -= 3600
    eng._persist("w")
    # Wedge the in-memory state too so we know the disk reflects the
    # backdated request before the outbound fires.
    await eng.record_outbound("w", "moved on")
    assert eng._workers["w"].pending_permissions == []

    fresh = BridgeEngine(data_dir=tmp_path)
    assert fresh._workers["w"].pending_permissions == []
