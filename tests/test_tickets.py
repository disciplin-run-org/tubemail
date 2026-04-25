"""Tests for TicketStore and PtyBridgeRegistry."""

from __future__ import annotations

import asyncio

import pytest

from tubemail_hub.bridge.pty_registry import PtyBridgeRegistry
from tubemail_hub.bridge.tickets import TicketStore


@pytest.fixture
def store() -> TicketStore:
    return TicketStore(ttl_s=0.5)


async def test_issue_and_consume(store: TicketStore):
    token = await store.issue("demo-tm")
    assert isinstance(token, str) and len(token) >= 32
    assert await store.consume(token, "demo-tm") is True
    # Single-use: second consume fails.
    assert await store.consume(token, "demo-tm") is False


async def test_cross_worker_rejection(store: TicketStore):
    token = await store.issue("worker-a")
    # Same token, different worker — must be rejected.
    assert await store.consume(token, "worker-b") is False
    # Consumed (popped) even on rejection? Yes — look at impl: pops then
    # checks worker. Post-pop, token is gone.
    assert await store.consume(token, "worker-a") is False


async def test_concurrent_double_consume(store: TicketStore):
    """Exactly one concurrent consume of the same token wins."""
    token = await store.issue("demo-tm")
    results = await asyncio.gather(
        store.consume(token, "demo-tm"),
        store.consume(token, "demo-tm"),
        store.consume(token, "demo-tm"),
    )
    assert results.count(True) == 1
    assert results.count(False) == 2


async def test_expiry(store: TicketStore):
    token = await store.issue("demo-tm")
    await asyncio.sleep(0.6)
    assert await store.consume(token, "demo-tm") is False


async def test_sweep_removes_expired(store: TicketStore):
    # Issue three, wait past TTL, one sweep should clear them all.
    for _ in range(3):
        await store.issue("demo-tm")
    assert store.size() == 3
    await asyncio.sleep(0.6)
    n = await store.sweep()
    assert n == 3
    assert store.size() == 0


async def test_pty_registry_attach_detach():
    reg = PtyBridgeRegistry()
    assert reg.attached_count("w") == 0
    assert reg.has_any_client("w") is False
    q1 = await reg.attach("w")
    q2 = await reg.attach("w")
    assert reg.attached_count("w") == 2
    assert reg.has_any_client("w") is True
    remaining = await reg.detach("w", q1)
    assert remaining == 1
    remaining = await reg.detach("w", q2)
    assert remaining == 0
    assert reg.has_any_client("w") is False


async def test_pty_registry_fan_out_bytes():
    reg = PtyBridgeRegistry()
    q1 = await reg.attach("w")
    q2 = await reg.attach("w")
    await reg.push_output("w", b"hello")
    # Items are tagged tuples now: ("bytes", payload) for binary frames,
    # ("text", payload) for control JSON.
    assert q1.get_nowait() == ("bytes", b"hello")
    assert q2.get_nowait() == ("bytes", b"hello")


async def test_pty_registry_fan_out_control():
    reg = PtyBridgeRegistry()
    q = await reg.attach("w")
    await reg.push_control("w", {"kind": "size", "cols": 80, "rows": 24})
    kind, payload = q.get_nowait()
    assert kind == "text"
    import json
    assert json.loads(payload) == {"kind": "size", "cols": 80, "rows": 24}


async def test_pty_registry_control_with_no_clients_is_noop():
    reg = PtyBridgeRegistry()
    # No clients attached — push_control just no-ops, doesn't raise.
    await reg.push_control("nobody", {"kind": "size", "cols": 80, "rows": 24})


async def test_pty_registry_no_clients_is_noop():
    reg = PtyBridgeRegistry()
    # Should not raise even with no attached clients.
    await reg.push_output("nobody-home", b"data")
