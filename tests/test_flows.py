"""Tests for the Flow Shell — FlowStore + MCP tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from tubemail_hub.bridge.engine import BridgeEngine
from tubemail_hub.bridge.flows import FlowStore, RunLogEntry


@pytest.fixture
def store(tmp_path: Path) -> FlowStore:
    return FlowStore(data_dir=tmp_path)


@pytest.fixture
def engine(tmp_path: Path) -> BridgeEngine:
    return BridgeEngine(data_dir=tmp_path / "engine")


async def test_save_and_get(store: FlowStore):
    flow = await store.save("qa-loop", "Run the QA loop", default_worker="iris-qa-tm")
    assert flow.name == "qa-loop"
    assert flow.default_worker == "iris-qa-tm"
    got = await store.get("qa-loop")
    assert got is not None
    assert got.body == "Run the QA loop"


async def test_save_updates_existing(store: FlowStore):
    f1 = await store.save("x", "first")
    f2 = await store.save("x", "second")
    assert f2.body == "second"
    assert f2.created_at == f1.created_at  # preserved
    assert f2.updated_at >= f1.updated_at


async def test_save_rejects_invalid_names(store: FlowStore):
    for bad in ["..", "../evil", "a/b", "", "a" * 100]:
        with pytest.raises(ValueError):
            await store.save(bad, "body")


async def test_list_all_alphabetical_and_skips_bad_files(store: FlowStore, tmp_path: Path):
    await store.save("b-flow", "b")
    await store.save("a-flow", "a")
    # Drop a crafted file with an invalid stem — must be ignored.
    (tmp_path / "flows" / "..evil.json").write_text('{"name": "evil", "body": "x"}')
    flows = await store.list_all()
    names = [f.name for f in flows]
    assert names == ["a-flow", "b-flow"]
    assert "evil" not in names


async def test_delete(store: FlowStore):
    await store.save("gone", "body")
    assert await store.delete("gone") is True
    assert await store.get("gone") is None
    # Deleting non-existent is a no-op False, not an error.
    assert await store.delete("gone") is False


async def test_run_lifecycle(store: FlowStore, engine: BridgeEngine):
    """Full save → enqueue → start_run → append → finish round-trip."""
    await store.save("hello", "say hi", default_worker="demo-tm")
    await engine.register_worker("demo-tm", "/")
    event = await engine.enqueue_inbound("demo-tm", "say hi")
    run_id = await store.start_run("hello", "demo-tm", event.event_id)
    assert len(run_id) >= 16

    log = await store.get_run(run_id)
    assert log is not None
    assert log.flow_name == "hello"
    assert log.worker == "demo-tm"
    assert log.first_event_id == event.event_id
    assert log.finished_at is None

    # Append a reply event.
    await store.append_run_event(run_id, RunLogEntry(
        event_id="reply-1", ts=event.ts + 1, kind="outbound", content="hi back",
    ))
    log = await store.get_run(run_id)
    assert log is not None
    assert len(log.events) == 1
    assert log.events[0].kind == "outbound"

    # Finish the run.
    await store.finish_run(run_id)
    log = await store.get_run(run_id)
    assert log is not None
    assert log.finished_at is not None


async def test_last_run_at_updates_on_start_run(store: FlowStore):
    await store.save("flow-a", "body")
    f1 = await store.get("flow-a")
    assert f1 is not None
    assert f1.last_run_at is None

    await store.start_run("flow-a", "worker", "evt-1")
    f2 = await store.get("flow-a")
    assert f2 is not None
    assert f2.last_run_at is not None


async def test_get_run_ignores_invalid_run_ids(store: FlowStore):
    assert await store.get_run("../evil") is None
    assert await store.get_run("") is None


async def test_append_run_event_on_missing_run_returns_false(store: FlowStore):
    ok = await store.append_run_event(
        "aabbccddeeffgghh",  # valid shape, but no such run on disk
        RunLogEntry(event_id="x", ts=0, kind="outbound", content=""),
    )
    assert ok is False
