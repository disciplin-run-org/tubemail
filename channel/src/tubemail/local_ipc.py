"""Local Unix-domain socket bridge between the channel plugin and its manager.

The channel plugin lives inside the Claude Code worker process; the manager
is the parent pty wrapper. Until now the only path between them was the
TubeMail hub: channel→hub HTTP→manager SSE. That works for everything
*except* reconnecting the tubemail MCP itself — when the hub is down, the
worker has no way to ask its own manager to drive `/mcp` and reconnect.

This module gives them a private side-channel that does not depend on the
hub. The manager opens a Unix-domain socket on startup at the path in
``TUBEMAIL_LOCAL_SOCK`` (default ``/tmp/tubemail-<session>.sock``) with
file mode ``0600`` so only the same user can connect. The channel reads
the same env var on init and uses it for the new ``reconnect_mcp`` tool.
If the env var is unset (older manager, or a manager that failed to bind
the socket), the channel falls back to hub-routed reconnect.

Wire protocol — one request, one response, then close:

    >>> request:  {"action": "reconnect_mcp", "server": "tubemail",
    ...            "request_id": "<uuid>"}
    >>> response: {"request_id": "<uuid>", "ok": true,
    ...            "server": "tubemail", "detail": "reconnected"}

Both sides use newline-terminated UTF-8 JSON. Requests are capped at 64 KB
so a malformed peer cannot exhaust memory by streaming an unterminated
line.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum bytes we will read for a single request line. Protects against a
# malformed peer that opens the socket and streams forever without a newline.
_MAX_REQUEST_BYTES = 64 * 1024

# How long the channel client waits for a manager reply before giving up.
# Manager-side `_PtyChild.reconnect_mcp` budgets ~25s of dialog driving;
# the extra 5s is for I/O scheduling jitter.
_CLIENT_TIMEOUT_S = 30.0

# Env var the manager exports and the channel reads.
SOCK_ENV = "TUBEMAIL_LOCAL_SOCK"


def default_sock_path(session_name: str) -> str:
    """Default socket path when ``TUBEMAIL_LOCAL_SOCK`` is unset.

    Picked to keep the path short (Linux limits sun_path to 108 bytes)
    and inside a per-user-writable location. The socket file itself is
    chmod 0600 immediately after bind in :meth:`LocalIPCServer.start`,
    so a hostile user on the same host cannot connect — the
    predictable ``/tmp`` path matches the convention already used by
    ``_pidfile_path`` and ``_logfile_path`` in ``manager.py``.
    """
    return f"/tmp/tubemail-{session_name}.sock"  # nosec B108


# ── Manager-side server ──────────────────────────────────────────────────


class LocalIPCServer:
    """Blocking, thread-based Unix socket server for the manager process.

    The manager already runs a mix of threads (SSE listener, pty pump,
    context-pct watcher), so this server uses the same model rather than
    introducing an asyncio loop. Each accepted connection is dispatched
    to a short-lived worker thread so the accept loop stays responsive
    while a long reconnect drives the pty.
    """

    def __init__(
        self,
        sock_path: str,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self._sock_path = sock_path
        self._handler = handler
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Bind the socket and start accepting connections in a daemon thread.

        Idempotent at startup: if a stale socket file exists from a previous
        manager that crashed before cleanup, it is unlinked and recreated.
        Raises only if the bind itself fails — callers can decide whether
        to disable the local-IPC path (the channel will fall back to the
        hub) or abort manager startup.
        """
        path = Path(self._sock_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(self._sock_path)
        os.chmod(self._sock_path, 0o600)
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = sock

        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            daemon=True,
            name="local-ipc-accept",
        )
        self._accept_thread.start()
        logger.info("local-ipc: listening on %s", self._sock_path)

    def stop(self) -> None:
        """Stop the accept loop and unlink the socket file.

        Best-effort; called on manager shutdown. Does not join handler
        threads because they're daemon threads driving the pty and the
        manager is exiting.
        """
        self._stop.set()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        with contextlib.suppress(FileNotFoundError):
            Path(self._sock_path).unlink()

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                # Socket was closed by stop(); loop will exit on next check.
                return
            threading.Thread(
                target=self._serve_connection,
                args=(conn,),
                daemon=True,
                name="local-ipc-serve",
            ).start()

    def _serve_connection(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(_CLIENT_TIMEOUT_S)
                line = self._read_line(conn)
                if line is None:
                    return
                request = json.loads(line)
                response = self._handler(request)
            except json.JSONDecodeError as e:
                response = {"ok": False, "error": f"invalid json: {e}"}
            except Exception as e:
                logger.exception("local-ipc: handler raised")
                response = {"ok": False, "error": f"handler exception: {e}"}
            try:
                payload = (json.dumps(response) + "\n").encode("utf-8")
                conn.sendall(payload)
            except OSError:
                logger.debug("local-ipc: peer closed before response")

    def _read_line(self, conn: socket.socket) -> str | None:
        buf = bytearray()
        while True:
            try:
                chunk = conn.recv(4096)
            except TimeoutError:
                logger.debug("local-ipc: read timeout")
                return None
            if not chunk:
                return None
            buf.extend(chunk)
            if b"\n" in chunk:
                line, _, _rest = buf.partition(b"\n")
                return line.decode("utf-8")
            if len(buf) > _MAX_REQUEST_BYTES:
                logger.warning(
                    "local-ipc: request exceeded %d bytes — dropping",
                    _MAX_REQUEST_BYTES,
                )
                return None


# ── Channel-side client ──────────────────────────────────────────────────


class LocalIPCError(Exception):
    """Raised when the channel cannot complete a local-IPC round trip.

    The channel catches this and falls back to the hub-routed reconnect.
    """


async def request(
    sock_path: str, payload: dict[str, Any], *, timeout_s: float = _CLIENT_TIMEOUT_S
) -> dict[str, Any]:
    """Send one request to the local IPC socket and return the response.

    Async on purpose — the channel plugin is asyncio-native, and a blocking
    ``socket.recv`` would freeze the stdio JSON-RPC pump for the duration
    of a 25-second reconnect.

    Raises :class:`LocalIPCError` if the socket file is missing, the
    connect fails, the manager closes without replying, or the response
    is not valid JSON. The caller is expected to log and try the hub
    fallback.
    """
    if not Path(sock_path).exists():
        raise LocalIPCError(f"socket file missing: {sock_path}")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(sock_path),
            timeout=2.0,
        )
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        raise LocalIPCError(f"connect failed: {e}") from e
    except asyncio.TimeoutError as e:
        raise LocalIPCError("connect timed out") from e

    try:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
        except asyncio.TimeoutError as e:
            raise LocalIPCError(f"manager did not reply within {timeout_s:.0f}s") from e
        if not line:
            raise LocalIPCError("manager closed connection without reply")
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise LocalIPCError(f"invalid json reply: {e}") from e
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
