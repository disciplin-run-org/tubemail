"""Durability tests for the BridgeEngine — pin the QM #207 fix.

Regression: queue 187 dogfooded a failure mode where ~5 hours of
quartermaster-tm history vanished from `/data/tubemail/workers/
quartermaster-tm.json` despite no purge call. Code review identified
the root cause: every `record_*`/`enqueue_inbound`/`send_interrupt`
path used `if ws is None: ws = WorkerState(name=worker, registered_at=
time.time()); self._workers[worker] = ws` — auto-creating fresh state
WITHOUT consulting the on-disk file. A single in-memory eviction (or
process restart race that left disk intact but cache empty) would
overwrite the file with the empty-events state on the next event.

The fix introduces `_get_or_create_worker(name)` which prefers
`_workers[name]` → on-disk → fresh, in that order. These tests pin
each branch so the durability contract can't regress silently.
"""

from __future__ import annotations

# Standard Libraries
import asyncio
import json
import time
from pathlib import Path

# 3rd party
import pytest

# Local
from tubemail_hub.bridge.engine import BridgeEngine
from tubemail_hub.bridge.models import WorkerEvent, WorkerState


pytestmark = pytest.mark.asyncio


@pytest.fixture
def engine(tmp_path: Path) -> BridgeEngine:
    return BridgeEngine(data_dir=tmp_path)


# ── concurrent writes ──────────────────────────────────────────────────────


async def test_concurrent_outbounds_preserve_all_events(engine: BridgeEngine):
    """50 concurrent `record_outbound` calls for the same worker must
    all end up on disk. Tests that the engine's `_lock` actually
    serialises persistence — no races, no lost writes."""
    worker = "concurrent-tm"
    await engine.register_worker(worker, cwd="/tmp", forwarder_version="0.1")

    async def write(i: int):
        return await engine.record_outbound(worker, f"event-{i:03d}")

    results = await asyncio.gather(*(write(i) for i in range(50)))
    assert len(results) == 50

    # On-disk file must contain all 50 outbound events.
    disk_path = engine._worker_file(worker)
    data = json.loads(disk_path.read_text())
    contents = sorted(
        e["content"] for e in data["events"] if e["kind"] == "outbound"
    )
    assert len(contents) == 50, (
        f"expected 50 events, got {len(contents)}: "
        f"first/last = {contents[:1] + contents[-1:]}"
    )
    assert contents[0] == "event-000"
    assert contents[-1] == "event-049"


# ── disk-preserves-history when in-memory missing ────────────────────────


async def test_outbound_with_disk_state_but_missing_in_memory_preserves_events(
    engine: BridgeEngine, tmp_path: Path,
):
    """Regression for QM #187 / #207: simulate the failure mode where
    in-memory `_workers` doesn't have the worker but disk does. Before
    the fix, `record_outbound` auto-created fresh state and overwrote
    the file. After the fix, disk state is loaded first."""
    worker = "evicted-tm"
    # Seed a worker with 3 prior events, persist it, then evict from
    # the in-memory cache (simulating an eviction or a process restart
    # where _load_all hadn't finished by the time a record_* fired).
    await engine.register_worker(worker, cwd="/seed", forwarder_version="0.1")
    for i in range(3):
        await engine.record_outbound(worker, f"prior-{i}")
    #end for

    # Confirm the seed.
    disk_path = engine._worker_file(worker)
    pre = json.loads(disk_path.read_text())
    assert len([e for e in pre["events"] if e["kind"] == "outbound"]) == 3

    # Evict. This is the failure trigger — disk has data, memory doesn't.
    engine._workers.pop(worker, None)
    assert worker not in engine._workers

    # Now write a new event. Pre-fix, this would overwrite disk with
    # empty events. Post-fix, disk's 3 prior events are loaded first.
    await engine.record_outbound(worker, "post-eviction")

    post = json.loads(disk_path.read_text())
    outbounds = [e["content"] for e in post["events"] if e["kind"] == "outbound"]
    assert outbounds == ["prior-0", "prior-1", "prior-2", "post-eviction"], (
        f"prior events lost on eviction: outbounds={outbounds}"
    )


async def test_register_with_disk_state_preserves_events(
    engine: BridgeEngine,
):
    """Same regression but for `register_worker`: a worker re-registering
    after an in-memory eviction must keep its history. Before the fix,
    re-register with `_workers[name]` empty produced fresh state."""
    worker = "rereg-tm"
    await engine.register_worker(worker, cwd="/seed", forwarder_version="0.1")
    for i in range(5):
        await engine.record_outbound(worker, f"prior-{i}")
    #end for

    engine._workers.pop(worker, None)

    cursor = await engine.register_worker(
        worker, cwd="/post", forwarder_version="0.2"
    )

    state = engine._workers[worker]
    assert len(state.events) == 5, (
        f"re-register lost events; got {len(state.events)}"
    )
    assert state.cwd == "/post"
    assert state.forwarder_version == "0.2"
    # The cursor returned should be the latest event's id, not empty.
    assert cursor == state.events[-1].event_id


# ── inbound + permissions paths also load from disk ───────────────────────


async def test_enqueue_inbound_with_evicted_state_preserves_events(
    engine: BridgeEngine,
):
    worker = "inbound-evict-tm"
    await engine.register_worker(worker, cwd="/seed", forwarder_version="0.1")
    await engine.record_outbound(worker, "first")
    engine._workers.pop(worker, None)

    await engine.enqueue_inbound(worker, "from-orchestrator")

    state = engine._workers[worker]
    contents = [e.content for e in state.events]
    assert "first" in contents and "from-orchestrator" in contents


# ── fresh worker still works ──────────────────────────────────────────────


async def test_outbound_for_unknown_worker_creates_fresh_state(
    engine: BridgeEngine, tmp_path: Path,
):
    """A worker name that's not in memory AND not on disk must still
    work — the auto-create path is legitimate when there's no prior
    state to preserve. We just want it to consult disk first."""
    worker = "brand-new-tm"
    # No prior register, no prior file.
    assert not engine._worker_file(worker).exists()

    await engine.record_outbound(worker, "hello")

    state = engine._workers[worker]
    assert len(state.events) == 1
    assert state.events[0].content == "hello"
    assert engine._worker_file(worker).exists()


# ── disk corruption falls back to fresh ───────────────────────────────────


async def test_corrupt_disk_state_falls_back_to_fresh(
    engine: BridgeEngine, tmp_path: Path,
):
    """When the on-disk file exists but is malformed, we want a fresh
    state rather than a crash. Caller-side logging handles surface."""
    worker = "corrupt-tm"
    path = engine._worker_file(worker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json")

    # Should not raise. Fresh state created.
    await engine.record_outbound(worker, "after-corruption")

    state = engine._workers[worker]
    assert len(state.events) == 1
    assert state.events[0].content == "after-corruption"


# ── audit tool ────────────────────────────────────────────────────────────


async def test_audit_tool_flags_suspiciously_thin_event_log(
    tmp_path: Path,
):
    """The audit tool should report a worker active for 5h with only 2
    events as suspicious — the exact shape of the QM #187 smoking gun."""
    from tubemail_hub.tools.audit_workers import audit_workers

    workers_dir = tmp_path / "workers"
    workers_dir.mkdir()

    now = time.time()
    # Worker A: registered 5h ago, last activity 10s ago, only 2 events.
    # That's a single event every ~2.5 hours — way below the threshold.
    suspect = WorkerState(
        name="suspect-tm",
        registered_at=now - 5 * 3600,
        last_activity=now - 10,
        events=[
            WorkerEvent(event_id="aa", ts=now - 5*3600, kind="inbound", content="x"),
            WorkerEvent(event_id="bb", ts=now - 10, kind="outbound", content="y"),
        ],
    )
    (workers_dir / "suspect-tm.json").write_text(suspect.model_dump_json())

    # Worker B: registered 5h ago, 100 events, healthy.
    healthy_events = [
        WorkerEvent(
            event_id=f"e{i:03d}", ts=now - 5*3600 + i*180,
            kind="outbound", content=f"e{i}",
        )
        for i in range(100)
    ]
    healthy = WorkerState(
        name="healthy-tm",
        registered_at=now - 5 * 3600,
        last_activity=now - 10,
        events=healthy_events,
    )
    (workers_dir / "healthy-tm.json").write_text(healthy.model_dump_json())

    # Worker C: registered 30s ago, 1 event, healthy (just started).
    fresh = WorkerState(
        name="fresh-tm",
        registered_at=now - 30,
        last_activity=now - 5,
        events=[
            WorkerEvent(event_id="cc", ts=now - 5, kind="outbound", content="z"),
        ],
    )
    (workers_dir / "fresh-tm.json").write_text(fresh.model_dump_json())

    anomalies = audit_workers(workers_dir)

    # suspect-tm flagged. healthy-tm and fresh-tm not flagged.
    flagged_names = {a["name"] for a in anomalies}
    assert "suspect-tm" in flagged_names
    assert "healthy-tm" not in flagged_names
    assert "fresh-tm" not in flagged_names
