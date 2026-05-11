"""WebSocket endpoint for the browser↔manager pty bridge.

Path: `GET /ws/pty/{worker}?ticket=<t>`

Protocol:
- Binary frames from the server carry pty output bytes fanned from the
  manager. The browser renders them in xterm.js.
- Text frames from the server carry control events as JSON (e.g. a
  `{"kind":"closed","reason":"..."}` sentinel before closing).
- Binary frames from the client carry keystrokes to write to the pty.
- Text frames from the client carry control events as JSON:
    {"kind":"resize","cols":N,"rows":N}
    {"kind":"ping"}

Auth: ticket-exchange. The browser POSTs `/api/pty-ticket` (bearer-
authed) to get a single-use 30s token, then opens this WS with
`?ticket=<t>`. Origin header is checked against an allowlist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from .engine import BridgeEngine
from .http import _validate_worker_name
from .pty_registry import PtyBridgeRegistry
from .tickets import TicketStore

logger = logging.getLogger(__name__)


def _default_allowed_origins() -> set[str]:
    """Cross-origin allowlist. Override via TUBEMAIL_ALLOWED_ORIGINS
    (comma-separated), e.g. 'https://tubemail.example.com'.

    Same-origin WS upgrades (the typical case — the SPA served by this
    hub opens a WS back to it) are always accepted via `_is_same_origin`
    regardless of this set, so operators reaching the hub on a LAN IP,
    Tailscale hostname, or reverse-proxied domain do NOT need to add
    that address here. This list only matters for genuinely cross-origin
    callers — e.g. the vite dev server on :5173 hitting the hub on :8004.
    """
    raw = os.environ.get("TUBEMAIL_ALLOWED_ORIGINS", "").strip()
    if raw:
        return {o.strip() for o in raw.split(",") if o.strip()}
    return {
        # vite dev server proxying the hub
        "http://localhost:5173",
    }


def _is_same_origin(websocket: WebSocket, origin: str) -> bool:
    """True iff the Origin header matches the WS request's own scheme+host.

    Same-origin is the actual security property the allowlist is meant to
    enforce: only the page served by THIS hub may open a WS back to it.
    Computing self-origin from the request (Host + scheme) accepts any
    address the operator reaches the hub on — localhost, LAN IP,
    Tailscale (100.x / *.ts.net), reverse-proxied hostname — without
    having to enumerate them in TUBEMAIL_ALLOWED_ORIGINS.

    Honors X-Forwarded-Proto so a TLS-terminating proxy in front of a
    plaintext hub still matches a browser Origin of https://... — the
    common shape when serving the UI behind nginx/caddy/cloudflare.
    """
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    host = websocket.headers.get("host", "")
    if not host:
        return False
    fwd_proto = (
        websocket.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    )
    if fwd_proto in ("http", "https"):
        expected_scheme = fwd_proto
    else:
        expected_scheme = "https" if websocket.url.scheme == "wss" else "http"
    return (
        parsed.scheme.lower() == expected_scheme
        and parsed.netloc.lower() == host.lower()
    )


def _tls_required() -> bool:
    """Return True if the hub is running HTTPS, False if plain HTTP.
    Mirrors the same filesystem check the entrypoint uses."""
    from pathlib import Path
    return Path("/data/server.crt").exists() and Path("/data/server.key").exists()


def build_ws_router(
    engine: BridgeEngine,
    tickets: TicketStore,
    pty_bridges: PtyBridgeRegistry,
) -> APIRouter:
    router = APIRouter(tags=["ws"])
    allowed_origins = _default_allowed_origins()

    @router.websocket("/ws/pty/{worker}")
    async def pty_bridge(
        websocket: WebSocket,
        worker: str,
        ticket: str = Query(default=""),
    ):
        # 1. Origin allowlist (prevents a malicious page in another tab from
        # hijacking a valid ticket). Accept if (a) Origin matches the WS
        # request's own scheme+host — i.e. same-origin, the normal case
        # whether the user reached the hub on localhost, a LAN IP, or a
        # Tailscale hostname — or (b) Origin is explicitly allowlisted
        # via TUBEMAIL_ALLOWED_ORIGINS for cross-origin setups.
        origin = websocket.headers.get("origin")
        if (
            origin
            and origin not in allowed_origins
            and not _is_same_origin(websocket, origin)
        ):
            logger.warning(
                "ws_pty: rejected origin %r (host=%r)",
                origin, websocket.headers.get("host"),
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # 2. Worker name sanity — same shape as HTTP routes.
        try:
            _validate_worker_name(worker)
        except Exception:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # 3. TLS enforcement. If the hub is HTTP-only, refuse to carry
        # keystrokes over the wire.
        if _tls_required_for_pty() and not _connection_is_secure(websocket):
            logger.error("ws_pty: refused non-TLS upgrade")
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # 4. Ticket consume — single-use, worker-scoped, TTL-bounded.
        if not ticket or not await tickets.consume(ticket, worker):
            logger.warning("ws_pty: bad ticket for %s", worker)
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # Past auth. Accept the upgrade and wire the pipes.
        await websocket.accept()
        client_queue = await pty_bridges.attach(worker)

        # Tell the MANAGER that a pty client is attached. Fire on EVERY
        # attach (not just the first) so additional clients — pop-out
        # windows, second tabs — get their own size echo and TUI
        # redraw. Without this, the second client sees an empty xterm
        # because the manager's "send size + trigger redraw" path runs
        # only on the first call.
        # The manager's `_start_pty_stream` is idempotent: subsequent
        # calls re-send size + redraw without duplicating the stream
        # loop or the initial buffer dump.
        manager_name = f"{worker}-manager"
        await engine._fan_out(manager_name, {
            "event": "pty_attach",
            "data": {},
        })

        async def pump_out_to_client() -> None:
            """Forwarder pty bytes / control → browser."""
            try:
                while True:
                    item = await client_queue.get()
                    # PtyItem = (kind, data). kind in ("bytes", "text").
                    if not isinstance(item, tuple) or len(item) != 2:
                        continue
                    kind, data = item
                    if kind == "bytes":
                        if not data:
                            # soft-close sentinel from the registry
                            break
                        await websocket.send_bytes(data)
                    elif kind == "text":
                        await websocket.send_text(data)
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.warning("ws_pty: out pump error: %s", e)

        async def pump_in_from_client() -> None:
            """Browser keystrokes / control events → forwarder."""
            try:
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if "bytes" in msg and msg["bytes"] is not None:
                        # Keystrokes — relay to manager as a pty_data SSE.
                        await engine._fan_out(manager_name, {
                            "event": "pty_data",
                            "data": {"bytes_hex": msg["bytes"].hex()},
                        })
                    elif "text" in msg and msg["text"] is not None:
                        await _handle_control(engine, manager_name, msg["text"])
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.warning("ws_pty: in pump error: %s", e)

        out_task = asyncio.create_task(pump_out_to_client())
        in_task = asyncio.create_task(pump_in_from_client())
        try:
            done, pending = await asyncio.wait(
                {out_task, in_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        finally:
            remaining = await pty_bridges.detach(worker, client_queue)
            # Last browser left — tell the manager to stop streaming, but
            # only if the recorder isn't also subscribed to this worker's
            # bytes. Without this check, toggling on recording then closing
            # the last browser tab silently kills the recording stream.
            recorder = getattr(pty_bridges, "_recorder", None)
            still_recording = (
                recorder is not None and recorder.is_recording(worker)
            )
            if remaining == 0 and not still_recording:
                await engine._fan_out(manager_name, {"event": "pty_detach", "data": {}})
            try:
                await websocket.close()
            except Exception:
                pass

    return router


async def _handle_control(
    engine: BridgeEngine, worker: str, text: str
) -> None:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("ws_pty: bad control JSON: %s", text[:80])
        return
    kind = obj.get("kind")
    if kind == "resize":
        cols = int(obj.get("cols", 80))
        rows = int(obj.get("rows", 24))
        await engine._fan_out(worker, {
            "event": "pty_resize",
            "data": {"cols": cols, "rows": rows},
        })
    elif kind == "ping":
        return
    else:
        logger.debug("ws_pty: ignoring unknown control kind %r", kind)


def _tls_required_for_pty() -> bool:
    """Whether this hub requires TLS for pty traffic.

    Policy: if the operator has placed certs under /data/server.crt, we
    expect TLS. If no certs, we still allow plaintext (localhost dev).
    The operator can force TLS-only by setting TUBEMAIL_PTY_TLS_ONLY=1.
    """
    if os.environ.get("TUBEMAIL_PTY_TLS_ONLY") == "1":
        return True
    return _tls_required()


def _connection_is_secure(ws: WebSocket) -> bool:
    """Best-effort check that the inbound WS is wss://. Starlette exposes
    scheme via `url.scheme`."""
    return ws.url.scheme == "wss"


__all__ = ["build_ws_router"]
