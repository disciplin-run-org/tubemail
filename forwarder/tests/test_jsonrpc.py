"""Tests for the JSON-RPC stdio layer."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tubemail.jsonrpc import JsonRpcError, JsonRpcStdio


class _FakeWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        pass

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.buf.decode().splitlines() if line.strip()]


def _make_reader(lines: list[dict[str, Any]]) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data((json.dumps(line) + "\n").encode())
    reader.feed_eof()
    return reader


async def test_send_notification_writes_line():
    writer = _FakeWriter()
    rpc = JsonRpcStdio(reader=asyncio.StreamReader(), writer=writer)
    await rpc.send_notification("foo/bar", {"a": 1})
    lines = writer.lines()
    assert len(lines) == 1
    assert lines[0] == {"jsonrpc": "2.0", "method": "foo/bar", "params": {"a": 1}}


async def test_send_response_success():
    writer = _FakeWriter()
    rpc = JsonRpcStdio(reader=asyncio.StreamReader(), writer=writer)
    await rpc.send_response(42, result={"ok": True})
    assert writer.lines()[0] == {"jsonrpc": "2.0", "id": 42, "result": {"ok": True}}


async def test_send_response_error():
    writer = _FakeWriter()
    rpc = JsonRpcStdio(reader=asyncio.StreamReader(), writer=writer)
    await rpc.send_response(7, error=JsonRpcError(-32601, "missing", {"extra": "info"}))
    msg = writer.lines()[0]
    assert msg["error"]["code"] == -32601
    assert msg["error"]["message"] == "missing"
    assert msg["error"]["data"] == {"extra": "info"}


async def test_dispatch_calls_request_handler():
    reader = _make_reader([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"x": 1}},
    ])
    writer = _FakeWriter()
    rpc = JsonRpcStdio(reader=reader, writer=writer)

    async def init(params):
        return {"capabilities": {}, "seen": params}

    rpc.on_request("initialize", init)
    await rpc.run()
    msg = writer.lines()[0]
    assert msg["id"] == 1
    assert msg["result"]["seen"] == {"x": 1}


async def test_dispatch_calls_notification_handler():
    received: list[Any] = []
    reader = _make_reader([
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"id": 3}},
    ])
    writer = _FakeWriter()
    rpc = JsonRpcStdio(reader=reader, writer=writer)

    async def handler(params):
        received.append(params)

    rpc.on_notification("notifications/cancelled", handler)
    await rpc.run()
    assert received == [{"id": 3}]
    # No response written for notifications
    assert writer.lines() == []


async def test_unknown_method_returns_error():
    reader = _make_reader([
        {"jsonrpc": "2.0", "id": 1, "method": "nonexistent"},
    ])
    writer = _FakeWriter()
    rpc = JsonRpcStdio(reader=reader, writer=writer)
    await rpc.run()
    msg = writer.lines()[0]
    assert msg["error"]["code"] == -32601


async def test_unknown_notification_with_fallback():
    captured: list[tuple[str, Any]] = []
    reader = _make_reader([
        {
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel/permission_request",
            "params": {"request_id": "abcde", "tool_name": "Bash"},
        },
    ])
    writer = _FakeWriter()
    rpc = JsonRpcStdio(reader=reader, writer=writer)

    async def fallback(method, params):
        captured.append((method, params))

    rpc.on_unknown_notification(fallback)
    await rpc.run()
    assert len(captured) == 1
    assert captured[0][0] == "notifications/claude/channel/permission_request"
    assert captured[0][1]["request_id"] == "abcde"


async def test_handler_exception_returns_internal_error():
    reader = _make_reader([
        {"jsonrpc": "2.0", "id": 1, "method": "crash"},
    ])
    writer = _FakeWriter()
    rpc = JsonRpcStdio(reader=reader, writer=writer)

    async def boom(params):
        raise ValueError("kaboom")

    rpc.on_request("crash", boom)
    await rpc.run()
    msg = writer.lines()[0]
    assert msg["error"]["code"] == -32603
    assert "kaboom" in msg["error"]["message"]


async def test_empty_lines_skipped():
    reader = asyncio.StreamReader()
    reader.feed_data(b"\n\n")
    reader.feed_data(
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    )
    reader.feed_eof()
    writer = _FakeWriter()
    rpc = JsonRpcStdio(reader=reader, writer=writer)

    async def ping(params):
        return {"pong": True}

    rpc.on_request("ping", ping)
    await rpc.run()
    assert writer.lines()[0]["result"]["pong"] is True
