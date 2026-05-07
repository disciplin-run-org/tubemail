"""Tests for the channel-health surface added in QM #206.

Covers four failure modes the channel plugin used to swallow:

1. **Init-time register fails.** The channel must surface a
   `notifications/claude/channel` event with `meta.kind=channel_health`
   AND `channel_health()` must return `registered: false`. (Today's bug:
   the LLM had no signal.)
2. **SSE re-register fails N≥5x.** Counter increments on every failure;
   the threshold notification fires exactly once per outage, not on
   every attempt.
3. **`ack` failure → JsonRpcError.** Used to be "ignored"; now the LLM
   learns its ack didn't land.
4. **`reply` success updates `last_outbound_success_at`.** So `health()`
   reflects the most recent successful outbound — useful for the LLM
   to confirm before emitting a critical qm-report.
"""

from __future__ import annotations

# Standard Libraries
import asyncio
import json
from typing import Any, AsyncIterator

# 3rd party
import pytest

# 1st party
from tubemail.channel import Channel
from tubemail.hub_client import HubClient, UNHEALTHY_NOTIFY_THRESHOLD
from tubemail.jsonrpc import JsonRpcStdio


# ── shared scaffolding (mirrors test_channel.py) ────────────────────────────


class FakeWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        pass

    def lines(self) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in self.buf.decode().splitlines()
            if line.strip()
        ]


class HealthFakeHub:
    """Hub stand-in that exposes the same surface HubClient provides for
    the Channel-side health wiring. We need:
      - register: configurable to raise, increments counter.
      - post_outbound: configurable to raise, stamps last_outbound on success.
      - health(): returns dict.
      - stream(): async iterator (empty queue by default).
      - aclose, unregister.
      - _health_notifier attribute (Channel sets this in __init__).
    """

    def __init__(self, *, register_raises: int = 0):
        # `register_raises` = how many register() calls should raise before
        # one succeeds. -1 = always raise.
        self._register_raises_remaining = register_raises
        self._register_always_raises = register_raises < 0
        self.registered = False
        self.connected = False
        self.register_failures_since_boot = 0
        self.consecutive_register_failures = 0
        self.last_outbound_success_at: float | None = None
        self.outbound: list[dict[str, Any]] = []
        self.permission_requests: list[dict[str, Any]] = []
        self.unregistered = False
        self.closed = False
        self.post_outbound_raises = False
        self._health_notifier = None
        self._unhealthy_notified = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def register(self, cwd: str, pid: int | None = None):
        should_raise = self._register_always_raises or (
            self._register_raises_remaining > 0
        )
        if should_raise:
            if self._register_raises_remaining > 0:
                self._register_raises_remaining -= 1
            self.register_failures_since_boot += 1
            self.consecutive_register_failures += 1
            self.registered = False
            raise RuntimeError("simulated register failure")
        #end if
        self.registered = True
        self.consecutive_register_failures = 0
        self._unhealthy_notified = False
        return {"worker": "test", "cursor": ""}

    async def unregister(self) -> None:
        self.unregistered = True

    async def post_outbound(self, text: str, meta: dict[str, Any] | None = None):
        if self.post_outbound_raises:
            raise RuntimeError("simulated outbound failure")
        self.outbound.append({"text": text, "meta": meta or {}})
        # Stamp a deterministic non-None value so tests can detect the
        # change without depending on wall-clock time.
        self.last_outbound_success_at = 12345.0
        return {"event_id": "fake", "ts": 0}

    async def post_permission_request(self, **_kwargs):
        return {"event_id": "fake", "ts": 0}

    async def post_permission_response(self, **_kwargs):
        return {"event_id": "fake", "ts": 0}

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            evt = await self._queue.get()
            if evt is None:
                return
            yield evt

    async def aclose(self) -> None:
        self.closed = True

    def health(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "registered": self.registered,
            "register_failures_since_boot": self.register_failures_since_boot,
            "last_outbound_success_at": self.last_outbound_success_at,
            "hub_url": "http://test",
        }


def _make_channel(reader, writer, hub) -> Channel:
    rpc = JsonRpcStdio(reader=reader, writer=writer)
    return Channel(hub=hub, worker_name="test", cwd="/tmp/test", rpc=rpc)


# ── deliverable 1: init-register failure surfaces to the LLM ────────────────


async def test_init_register_failure_emits_channel_health_notification():
    """When the initial `register()` raises, the channel must NOT just
    log+continue. It must push a `notifications/claude/channel` event
    tagged with `meta.kind=channel_health`."""
    hub = HealthFakeHub(register_raises=-1)  # always raise

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    reader.feed_data(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')

    writer = FakeWriter()
    ch = _make_channel(reader, writer, hub)

    async def close_after():
        await asyncio.sleep(0.1)
        reader.feed_eof()

    close_task = asyncio.create_task(close_after())
    await ch.run()
    await close_task

    lines = writer.lines()
    health_notif = next(
        (
            m for m in lines
            if m.get("method") == "notifications/claude/channel"
            and (m.get("params") or {}).get("meta", {}).get("kind") == "channel_health"
        ),
        None,
    )
    assert health_notif is not None, (
        f"expected channel_health notification, saw methods="
        f"{[m.get('method') for m in lines]}"
    )
    params = health_notif["params"]
    assert params["meta"]["level"] == "error"
    assert params["meta"]["phase"] == "init"
    assert params["meta"]["source"] == "tubemail-channel"
    assert "registration failed" in params["content"].lower()
    assert "channel_health" in params["content"]


async def test_channel_health_tool_reports_registered_false_after_init_failure():
    """With init register having failed, the `channel_health` tool must
    return `registered: false`. The LLM uses this to decide whether to
    trust a reply."""
    hub = HealthFakeHub(register_raises=-1)

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    reader.feed_data(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    reader.feed_data(
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        b'"params":{"name":"channel_health","arguments":{}}}\n'
    )

    writer = FakeWriter()
    ch = _make_channel(reader, writer, hub)

    async def close_after():
        await asyncio.sleep(0.1)
        reader.feed_eof()

    close_task = asyncio.create_task(close_after())
    await ch.run()
    await close_task

    health_resp = next(
        (m for m in writer.lines() if m.get("id") == 2),
        None,
    )
    assert health_resp is not None
    assert "error" not in health_resp
    structured = health_resp["result"].get("structuredContent")
    assert structured is not None, "channel_health must return structuredContent"
    assert structured["registered"] is False
    assert structured["register_failures_since_boot"] >= 1


# ── deliverable 2: SSE re-register threshold notification ────────────────────


async def test_hub_client_register_counter_increments_on_each_failure():
    """The `register_failures_since_boot` counter is monotonic — every
    failure bumps it. The channel_health tool surfaces this so a flapping
    link is observable."""
    import httpx

    # Build a HubClient against an unreachable URL so register raises.
    client = httpx.AsyncClient(timeout=0.1)
    hub = HubClient(
        base_url="http://127.0.0.1:1",  # connection-refused
        worker="test",
        secret="x",
        client=client,
    )
    try:
        for _ in range(3):
            with pytest.raises(Exception):
                await hub.register(cwd="/tmp")
            #end with
        #end for
        h = hub.health()
        assert h["register_failures_since_boot"] == 3
        assert h["registered"] is False
    finally:
        await client.aclose()
    #end try


async def test_hub_client_threshold_fires_health_notifier_once(monkeypatch):
    """When SSE-loop register failures hit UNHEALTHY_NOTIFY_THRESHOLD,
    the health_notifier fires ONCE — not on every subsequent failure. A
    successful register in between resets the latch.

    The SSE-disconnect backoff schedule (0.5/1/2/4/8…s) would make this
    test sleep ~7.5s to reach 5 attempts on the production timing. Patch
    `asyncio.sleep` inside the hub_client module to a fast stub so we
    drive the loop deterministically without burning real wall-clock.
    """
    import httpx
    from tubemail import hub_client as hub_client_mod

    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        # Yield once to keep cooperative scheduling working but skip the
        # backoff. Real `asyncio.sleep(0)` is the canonical "yield and
        # come back" idiom.
        await real_sleep(0)

    monkeypatch.setattr(hub_client_mod.asyncio, "sleep", fast_sleep)

    notifications: list[dict[str, Any]] = []

    async def notifier(payload: dict[str, Any]) -> None:
        notifications.append(payload)

    client = httpx.AsyncClient(timeout=0.1)
    hub = HubClient(
        base_url="http://127.0.0.1:1",
        worker="test",
        secret="x",
        client=client,
        health_notifier=notifier,
    )
    try:
        # Drive the stream() generator forward enough times to trip the
        # threshold. We can't actually reach SSE (no hub), so we just
        # poll the generator and tear it down once we see the notification.
        agen = hub.stream()

        async def pump():
            try:
                async for _evt in agen:
                    pass  # never reaches — no SSE
                #end for
            except Exception:
                pass
            #end try

        task = asyncio.create_task(pump())
        # With sleeps no-op'd this should fire within a fraction of a
        # second. 5 seconds is generous headroom for the httpx connect
        # timeout (0.1s × 5 attempts = ~0.5s wall-clock).
        for _ in range(50):
            if notifications:
                break
            await real_sleep(0.1)
        #end for
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        #end try
        try:
            await agen.aclose()
        except Exception:
            pass
        #end try

        assert len(notifications) == 1, (
            f"expected exactly 1 notification, got {len(notifications)}"
        )
        payload = notifications[0]
        assert payload["meta"]["kind"] == "channel_health"
        assert payload["meta"]["source"] == "tubemail-channel"
        assert payload["meta"]["consecutive_failures"] >= UNHEALTHY_NOTIFY_THRESHOLD
        assert "channel_health" in payload["content"]
        # Counter is monotonic and at least equals the threshold.
        assert hub._register_failures_since_boot >= UNHEALTHY_NOTIFY_THRESHOLD
    finally:
        await client.aclose()
    #end try


# ── deliverable 3: ack failure raises JsonRpcError ──────────────────────────


async def test_ack_failure_raises_jsonrpc_error():
    """ack used to swallow with `logger.exception("...ignored")`. Now
    the LLM gets a JsonRpcError -32603 so it can retry or warn."""
    hub = HealthFakeHub()
    hub.post_outbound_raises = True

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    reader.feed_data(
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        b'"params":{"name":"ack","arguments":{}}}\n'
    )

    writer = FakeWriter()
    ch = _make_channel(reader, writer, hub)

    async def close_after():
        await asyncio.sleep(0.1)
        reader.feed_eof()

    close_task = asyncio.create_task(close_after())
    await ch.run()
    await close_task

    ack_resp = next((m for m in writer.lines() if m.get("id") == 2), None)
    assert ack_resp is not None
    assert "error" in ack_resp, f"expected error response, got {ack_resp}"
    assert ack_resp["error"]["code"] == -32603
    assert "post_outbound failed" in ack_resp["error"]["message"]


# ── deliverable 4: reply success updates last_outbound_success_at ───────────


async def test_reply_success_updates_last_outbound_timestamp():
    """A successful `reply` must set `last_outbound_success_at` so the
    LLM can confirm via channel_health that recent outbound traffic has
    actually been landing."""
    hub = HealthFakeHub()
    assert hub.last_outbound_success_at is None

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    reader.feed_data(
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call",'
        b'"params":{"name":"reply","arguments":{"text":"ok"}}}\n'
    )
    reader.feed_data(
        b'{"jsonrpc":"2.0","id":3,"method":"tools/call",'
        b'"params":{"name":"channel_health","arguments":{}}}\n'
    )

    writer = FakeWriter()
    ch = _make_channel(reader, writer, hub)

    async def close_after():
        await asyncio.sleep(0.1)
        reader.feed_eof()

    close_task = asyncio.create_task(close_after())
    await ch.run()
    await close_task

    # Reply landed.
    assert len(hub.outbound) == 1
    assert hub.outbound[0]["text"] == "ok"
    assert hub.last_outbound_success_at == 12345.0

    # channel_health response reflects the timestamp.
    health_resp = next(
        (m for m in writer.lines() if m.get("id") == 3), None
    )
    assert health_resp is not None
    structured = health_resp["result"]["structuredContent"]
    assert structured["last_outbound_success_at"] == 12345.0


async def test_post_outbound_does_not_stamp_on_failure():
    """If the hub returns 5xx (or the network fails), `last_outbound_success_at`
    must NOT advance. A lying timestamp is worse than a stale one — the
    LLM uses this field to gate critical qm-reports."""
    import httpx

    # Real HubClient against unreachable URL — every post raises.
    client = httpx.AsyncClient(timeout=0.1)
    hub = HubClient(
        base_url="http://127.0.0.1:1",
        worker="test",
        secret="x",
        client=client,
    )
    try:
        with pytest.raises(Exception):
            await hub.post_outbound("hi")
        #end with
        assert hub.health()["last_outbound_success_at"] is None
    finally:
        await client.aclose()
    #end try


# ── tools/list now exposes channel_health ───────────────────────────────────


async def test_tools_list_includes_channel_health():
    hub = HealthFakeHub()
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
    reader.feed_eof()

    writer = FakeWriter()
    ch = _make_channel(reader, writer, hub)
    await ch.run()

    tools = writer.lines()[0]["result"]["tools"]
    names = [t["name"] for t in tools]
    assert "channel_health" in names
    assert "reply" in names
    assert "ack" in names
