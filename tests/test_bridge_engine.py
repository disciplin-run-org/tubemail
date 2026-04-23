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
