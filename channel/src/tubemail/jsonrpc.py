"""Minimal JSON-RPC 2.0 stdio reader/writer.

Claude Code MCP stdio transport is line-delimited JSON — one JSON-RPC
message per line on stdin / stdout. This module reads from an async stream
and writes to an async stream, with a serialized write queue so multiple
concurrent emit() callers don't interleave bytes.

Deliberately does NOT depend on the `mcp` Python SDK — the SDK's closed
notification-type union rejects `notifications/claude/channel/permission_request`.
Hand-rolling keeps us in full control of the wire format.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class JsonRpcStdio:
    """Line-delimited JSON-RPC 2.0 over stdin/stdout.

    Reads are blocking `readline()` calls dispatched to a thread executor
    (robust across all fd types: pipes, files, character devices, sockets).
    Writes go directly to `sys.stdout.buffer` under an async lock — fast
    enough not to meaningfully block the event loop, and it sidesteps
    asyncio.connect_write_pipe's fd-type restriction.

    Tests inject an asyncio.StreamReader for `reader` and a FakeWriter for
    `writer` (any object with `write(bytes)` and awaitable `drain()`). In
    production both are None and real stdin/stdout are used.

    Usage:
        io = JsonRpcStdio()
        io.on_request("initialize", handle_init)
        io.on_notification("notifications/cancelled", handle_cancel)
        await io.run()
    """

    def __init__(
        self,
        reader: asyncio.StreamReader | None = None,
        writer: Any | None = None,
    ):
        self._reader = reader
        self._writer = writer
        self._use_stdio = reader is None and writer is None
        self._write_lock = asyncio.Lock()
        self._request_handlers: dict[str, Callable[[Any], Awaitable[Any]]] = {}
        self._notification_handlers: dict[str, Callable[[Any], Awaitable[None]]] = {}
        self._fallback_request: Callable[[str, Any], Awaitable[Any]] | None = None
        self._fallback_notification: Callable[[str, Any], Awaitable[None]] | None = None
        self._stopped = asyncio.Event()

    # ── handler registration ─────────────────────────────────────────────────

    def on_request(self, method: str, handler: Callable[[Any], Awaitable[Any]]) -> None:
        self._request_handlers[method] = handler

    def on_notification(
        self, method: str, handler: Callable[[Any], Awaitable[None]]
    ) -> None:
        self._notification_handlers[method] = handler

    def on_unknown_request(
        self, handler: Callable[[str, Any], Awaitable[Any]]
    ) -> None:
        self._fallback_request = handler

    def on_unknown_notification(
        self, handler: Callable[[str, Any], Awaitable[None]]
    ) -> None:
        self._fallback_notification = handler

    # ── wire I/O ─────────────────────────────────────────────────────────────

    async def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        async with self._write_lock:
            if self._writer is not None:
                # Injected test writer: duck-typed write(bytes) + awaitable drain()
                self._writer.write(encoded)
                drain = getattr(self._writer, "drain", None)
                if drain is not None:
                    result = drain()
                    if asyncio.iscoroutine(result):
                        await result
            else:
                # Production: write straight to stdout.buffer
                sys.stdout.buffer.write(encoded)
                sys.stdout.buffer.flush()

    async def send_response(
        self, request_id: Any, result: Any | None = None, error: JsonRpcError | None = None
    ) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            err_obj: dict[str, Any] = {"code": error.code, "message": error.message}
            if error.data is not None:
                err_obj["data"] = error.data
            msg["error"] = err_obj
        else:
            msg["result"] = result if result is not None else {}
        await self.send(msg)

    async def send_notification(self, method: str, params: Any | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self.send(msg)

    # ── dispatch loop ────────────────────────────────────────────────────────

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" in message:
            # Request
            method = message["method"]
            params = message.get("params", {})
            req_id = message["id"]
            try:
                handler = self._request_handlers.get(method)
                if handler is not None:
                    result = await handler(params)
                elif self._fallback_request is not None:
                    result = await self._fallback_request(method, params)
                else:
                    raise JsonRpcError(-32601, f"method not found: {method}")
                await self.send_response(req_id, result=result)
            except JsonRpcError as e:
                await self.send_response(req_id, error=e)
            except Exception as e:
                logger.exception("request handler crashed: %s", method)
                await self.send_response(
                    req_id,
                    error=JsonRpcError(-32603, f"internal error: {e}"),
                )
        elif "method" in message:
            # Notification
            method = message["method"]
            params = message.get("params", {})
            handler = self._notification_handlers.get(method)
            try:
                if handler is not None:
                    await handler(params)
                elif self._fallback_notification is not None:
                    await self._fallback_notification(method, params)
                # Unknown notification without fallback: silently drop (spec-compliant)
            except Exception:
                logger.exception("notification handler crashed: %s", method)
        elif "id" in message and ("result" in message or "error" in message):
            # Incoming response to a request we sent — we don't send client requests
            # in this process, so log and ignore.
            logger.debug("unexpected response message: %s", message)
        else:
            logger.warning("malformed JSON-RPC message: %s", message)

    async def _read_line(self) -> bytes:
        if self._reader is not None:
            return await self._reader.readline()
        # Production path: blocking readline in a thread
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, sys.stdin.buffer.readline)

    async def run(self) -> None:
        """Read lines from stdin, parse, dispatch. Returns when stdin closes."""
        while not self._stopped.is_set():
            line = await self._read_line()
            if not line:
                # EOF — Claude Code closed stdin, we should exit.
                break
            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue
            try:
                message = json.loads(line_str)
            except json.JSONDecodeError as e:
                logger.error("parse error: %s | line=%s", e, line_str)
                continue
            await self._dispatch(message)

    def stop(self) -> None:
        self._stopped.set()
