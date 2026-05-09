"""Tests for the Channel plugin logic with a mocked hub."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from tubemail.channel import Channel
from tubemail.jsonrpc import JsonRpcStdio


class FakeHub:
    """In-memory hub stand-in for tests."""

    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []
        self.outbound: list[dict[str, Any]] = []
        self.permission_requests: list[dict[str, Any]] = []
        self.unregistered = False
        self.closed = False
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def register(self, cwd: str, pid: int | None = None) -> dict[str, Any]:
        self.registered.append({"cwd": cwd, "pid": pid})
        return {"worker": "test", "cursor": ""}

    async def unregister(self) -> None:
        self.unregistered = True

    async def post_outbound(
        self, text: str, meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.outbound.append({"text": text, "meta": meta or {}})
        return {"event_id": "fake", "ts": 0}

    async def post_permission_request(
        self,
        request_id: str,
        tool_name: str,
        description: str = "",
        input_preview: str = "",
    ) -> dict[str, Any]:
        self.permission_requests.append(
            {
                "request_id": request_id,
                "tool_name": tool_name,
                "description": description,
                "input_preview": input_preview,
            }
        )
        return {"event_id": "fake", "ts": 0}

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            evt = await self._queue.get()
            if evt is None:
                return
            yield evt

    def push_event(self, event: str, data: dict[str, Any]) -> None:
        self._queue.put_nowait({"event": event, "data": data})

    async def aclose(self) -> None:
        self.closed = True


class FakeWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        pass

    def lines(self) -> list[dict[str, Any]]:
        return [
            json.loads(line) for line in self.buf.decode().splitlines() if line.strip()
        ]


def make_reader(messages: list[dict[str, Any]]) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    for m in messages:
        r.feed_data((json.dumps(m) + "\n").encode())
    r.feed_eof()
    return r


def make_channel(reader, writer, hub=None) -> tuple[Channel, FakeHub]:
    rpc = JsonRpcStdio(reader=reader, writer=writer)
    hub = hub or FakeHub()
    ch = Channel(hub=hub, worker_name="test", cwd="/tmp/test", rpc=rpc)
    return ch, hub


async def test_initialize_returns_experimental_capability():
    reader = make_reader(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        ]
    )
    writer = FakeWriter()
    ch, _ = make_channel(reader, writer)
    await ch.run()
    msg = writer.lines()[0]
    assert msg["id"] == 1
    caps = msg["result"]["capabilities"]
    assert "claude/channel" in caps["experimental"]
    assert "claude/channel/permission" in caps["experimental"]
    assert caps["tools"]["listChanged"] is False
    assert msg["result"]["serverInfo"]["name"] == "tubemail"


async def test_tools_list_has_reply_and_ack():
    reader = make_reader(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ]
    )
    writer = FakeWriter()
    ch, _ = make_channel(reader, writer)
    await ch.run()
    tools = writer.lines()[0]["result"]["tools"]
    names = [t["name"] for t in tools]
    assert "reply" in names
    assert "ack" in names


async def test_reply_tool_posts_outbound_to_hub():
    reader = make_reader(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "reply",
                    "arguments": {
                        "text": "hello orchestrator",
                        "meta": {"progress": 50},
                    },
                },
            },
        ]
    )
    writer = FakeWriter()
    ch, hub = make_channel(reader, writer)
    await ch.run()
    assert len(hub.outbound) == 1
    assert hub.outbound[0]["text"] == "hello orchestrator"
    assert hub.outbound[0]["meta"] == {"progress": 50}
    resp = writer.lines()[0]
    assert resp["id"] == 1
    assert "error" not in resp


async def test_ack_tool_posts_empty_outbound():
    reader = make_reader(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ack", "arguments": {}},
            },
        ]
    )
    writer = FakeWriter()
    ch, hub = make_channel(reader, writer)
    await ch.run()
    assert len(hub.outbound) == 1
    assert hub.outbound[0]["meta"]["kind"] == "ack"


async def test_permission_request_notification_forwarded_to_hub():
    reader = make_reader(
        [
            {
                "jsonrpc": "2.0",
                "method": "notifications/claude/channel/permission_request",
                "params": {
                    "request_id": "abcde",
                    "tool_name": "Bash",
                    "description": "run pytest",
                    "input_preview": "pytest tests/",
                },
            },
        ]
    )
    writer = FakeWriter()
    ch, hub = make_channel(reader, writer)
    await ch.run()
    assert len(hub.permission_requests) == 1
    assert hub.permission_requests[0]["request_id"] == "abcde"
    assert hub.permission_requests[0]["tool_name"] == "Bash"


async def test_unknown_tool_returns_error():
    reader = make_reader(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "nonexistent", "arguments": {}},
            },
        ]
    )
    writer = FakeWriter()
    ch, _ = make_channel(reader, writer)
    await ch.run()
    msg = writer.lines()[0]
    assert msg["error"]["code"] == -32601


async def test_initialized_triggers_register_and_stream():
    """After `notifications/initialized`, the channel should register with the
    hub and start pumping SSE events into Claude Code as channel
    notifications."""
    hub = FakeHub()
    hub.push_event(
        "channel_event",
        {"content": "hello from orchestrator", "meta": {"from": "orch"}},
    )

    # Feed initialize + initialized then EOF after a delay so the SSE pump has
    # time to flush one event before the RPC loop exits.
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
    reader.feed_data(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')

    writer = FakeWriter()
    ch, _ = make_channel(reader, writer, hub=hub)

    async def close_after_delay():
        await asyncio.sleep(0.2)
        reader.feed_eof()

    close_task = asyncio.create_task(close_after_delay())
    await ch.run()
    await close_task

    assert len(hub.registered) == 1

    # writer lines should contain initialize response + a channel notification
    lines = writer.lines()
    methods = [m.get("method") for m in lines if "method" in m]
    assert "notifications/claude/channel" in methods

    channel_notif = next(
        m for m in lines if m.get("method") == "notifications/claude/channel"
    )
    assert channel_notif["params"]["content"] == "hello from orchestrator"
    assert channel_notif["params"]["meta"]["source"] == "tubemail"


async def test_permission_response_event_becomes_notification():
    hub = FakeHub()
    hub.push_event("permission_response", {"request_id": "abcde", "behavior": "allow"})

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    reader.feed_data(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')

    writer = FakeWriter()
    ch, _ = make_channel(reader, writer, hub=hub)

    async def close_after_delay():
        await asyncio.sleep(0.2)
        reader.feed_eof()

    close_task = asyncio.create_task(close_after_delay())
    await ch.run()
    await close_task

    lines = writer.lines()
    perm_notif = next(
        (
            m
            for m in lines
            if m.get("method") == "notifications/claude/channel/permission"
        ),
        None,
    )
    assert perm_notif is not None
    assert perm_notif["params"]["request_id"] == "abcde"
    assert perm_notif["params"]["behavior"] == "allow"


async def test_unregister_on_shutdown():
    reader = make_reader(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ]
    )
    writer = FakeWriter()
    ch, hub = make_channel(reader, writer)
    await ch.run()
    assert hub.unregistered is True
    assert hub.closed is True


# ── reconnect_mcp tool ──────────────────────────────────────────────────


async def test_tools_list_advertises_reconnect_mcp():
    """The new tool must show up in tools/list with the right schema so Claude
    Code surfaces it to the LLM."""
    reader = make_reader(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ]
    )
    writer = FakeWriter()
    ch, _ = make_channel(reader, writer)
    await ch.run()
    tools = writer.lines()[0]["result"]["tools"]
    by_name = {t["name"]: t for t in tools}
    assert "reconnect_mcp" in by_name
    schema = by_name["reconnect_mcp"]["inputSchema"]
    assert schema["properties"]["server"]["type"] == "string"
    assert schema["required"] == ["server"]


async def test_reconnect_mcp_returns_helpful_error_when_env_unset(monkeypatch):
    """Without TUBEMAIL_LOCAL_SOCK the channel cannot reach the manager locally
    — it must respond with a structured error and point the LLM at the hub-
    routed tm_self_reconnect_mcp instead.

    The call MUST NOT raise — the LLM can only react to a tool result,
    not an exception.
    """
    monkeypatch.delenv("TUBEMAIL_LOCAL_SOCK", raising=False)
    reader = make_reader(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "reconnect_mcp",
                    "arguments": {"server": "tubemail"},
                },
            },
        ]
    )
    writer = FakeWriter()
    ch, _ = make_channel(reader, writer)
    await ch.run()
    resp = writer.lines()[0]
    assert "error" not in resp
    body = resp["result"]["structuredContent"]
    assert body["ok"] is False
    assert body["server"] == "tubemail"
    assert "TUBEMAIL_LOCAL_SOCK" in body["detail"]


async def test_reconnect_mcp_falls_back_to_hub_when_socket_unreachable(
    monkeypatch,
    tmp_path,
):
    """If TUBEMAIL_LOCAL_SOCK points at a missing socket file, the call returns
    ok=false with a 'local IPC failed' detail, NOT an exception.

    The orchestrator can then pick a different recovery strategy.
    """
    missing = tmp_path / "missing.sock"
    monkeypatch.setenv("TUBEMAIL_LOCAL_SOCK", str(missing))
    reader = make_reader(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "reconnect_mcp",
                    "arguments": {"server": "tubemail"},
                },
            },
        ]
    )
    writer = FakeWriter()
    ch, _ = make_channel(reader, writer)
    await ch.run()
    body = writer.lines()[0]["result"]["structuredContent"]
    assert body["ok"] is False
    assert "local IPC failed" in body["detail"]


async def test_reconnect_mcp_routes_through_local_socket(monkeypatch, tmp_path):
    """End-to-end: a real LocalIPCServer responds, the channel reads the
    response, drops the request_id echo, and returns the structured body
    to Claude Code."""
    from tubemail.local_ipc import LocalIPCServer

    sock_path = str(tmp_path / "ipc.sock")
    seen: list[dict] = []

    def handler(req: dict) -> dict:
        seen.append(req)
        return {
            "request_id": req.get("request_id", ""),
            "ok": True,
            "server": req["server"],
            "detail": "reconnected",
        }

    server = LocalIPCServer(sock_path, handler)
    server.start()
    monkeypatch.setenv("TUBEMAIL_LOCAL_SOCK", sock_path)

    reader = make_reader(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "reconnect_mcp",
                    "arguments": {"server": "tubemail"},
                },
            },
        ]
    )
    writer = FakeWriter()
    try:
        ch, _ = make_channel(reader, writer)
        await ch.run()
    finally:
        server.stop()

    body = writer.lines()[0]["result"]["structuredContent"]
    assert body == {"ok": True, "server": "tubemail", "detail": "reconnected"}
    assert seen and seen[0]["action"] == "reconnect_mcp"
    assert seen[0]["server"] == "tubemail"
    # The channel generates a uuid4 request_id; the test only cares it's
    # non-empty and gets echoed for correlation.
    assert seen[0]["request_id"]


async def test_reconnect_mcp_rejects_missing_server_arg():
    reader = make_reader(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "reconnect_mcp",
                    "arguments": {},
                },
            },
        ]
    )
    writer = FakeWriter()
    ch, _ = make_channel(reader, writer)
    await ch.run()
    resp = writer.lines()[0]
    assert resp["error"]["code"] == -32602
    assert "server" in resp["error"]["message"]
