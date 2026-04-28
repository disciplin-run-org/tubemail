"""Tubemail HTTP router.

Forwarder-facing endpoints under /tubemail/<worker>/. Bearer-auth via a
shared secret env var (TUBEMAIL_SECRET). SSE stream for server-pushed
events (channel notifications, permission responses, interrupts).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from sse_starlette.sse import EventSourceResponse

if TYPE_CHECKING:
    from .pty_registry import PtyBridgeRegistry  # noqa: F401

from .engine import BridgeEngine
from .models import (
    InboundEvent,
    OutboundEvent,
    PermissionRequestPayload,
    RegisterRequest,
    RegisterResponse,
)

logger = logging.getLogger(__name__)

# Worker names become filesystem paths via BridgeEngine._worker_file. Reject
# anything that could escape the workers directory or produce weird filenames.
# Keep the character set wide enough to match ecosystem conventions like
# `leanspecs-spec-tm`, `jesper-resume-and-linkedin-tm`.
_WORKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")


def _validate_worker_name(name: str) -> str:
    if not _WORKER_NAME_RE.fullmatch(name) or ".." in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid worker name",
        )
    return name


def _check_auth(authorization: str | None) -> None:
    secret = os.environ.get("TUBEMAIL_SECRET")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="tubemail secret not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    provided = authorization.removeprefix("Bearer ").strip()
    # Constant-time compare: prevents timing attacks that recover the secret
    # one byte at a time by measuring 401 response latency.
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
        )


async def auth_dep(authorization: str | None = Header(default=None)) -> None:
    _check_auth(authorization)


async def auth_dep_with_query(
    authorization: str | None = Header(default=None),
    token: str | None = None,
) -> None:
    """Accept the bearer either as an `Authorization: Bearer <t>` header
    OR as a `?token=<t>` query-string parameter. The query-string path
    exists only because browsers cannot set custom headers on
    `new EventSource()`. Use this ONLY on endpoints the browser reaches
    directly via EventSource (the hub-wide SSE) — never on endpoints
    the forwarder or MCP clients hit.

    The query-string token leaks into server logs, proxy logs, and
    browser history. v2 replaces this with the ticket-exchange pattern
    specified in jjstack/ceo-plans/jesper-main-eng-20260423-222000.md.
    """
    if authorization:
        _check_auth(authorization)
        return
    if token:
        _check_auth(f"Bearer {token}")
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing bearer token",
    )


def build_tubemail_router(
    engine: BridgeEngine,
    pty_bridges: "PtyBridgeRegistry | None" = None,
) -> APIRouter:
    router = APIRouter(prefix="/tubemail", tags=["tubemail"])

    @router.post("/{worker}/register", dependencies=[Depends(auth_dep)])
    async def register(worker: str, body: RegisterRequest) -> RegisterResponse:
        worker = _validate_worker_name(worker)
        cursor = await engine.register_worker(
            worker, body.cwd, forwarder_version=body.forwarder_version
        )
        return RegisterResponse(worker=worker, cursor=cursor)

    @router.post("/{worker}/unregister", dependencies=[Depends(auth_dep)])
    async def unregister(worker: str) -> dict[str, Any]:
        worker = _validate_worker_name(worker)
        await engine.unregister_worker(worker)
        return {"ok": True}

    @router.post("/{worker}/goodbye", dependencies=[Depends(auth_dep)])
    async def goodbye(worker: str) -> dict[str, Any]:
        """Called by the forwarder immediately before a clean shutdown
        (user typed /exit). Marks the worker as exited_cleanly so
        list_workers can distinguish clean exits from crashes/hangs."""
        worker = _validate_worker_name(worker)
        await engine.goodbye_worker(worker)
        return {"ok": True}

    @router.post("/{worker}/outbound", dependencies=[Depends(auth_dep)])
    async def outbound(worker: str, body: OutboundEvent) -> dict[str, Any]:
        worker = _validate_worker_name(worker)
        event = await engine.record_outbound(worker, body.text, body.meta)
        return {"event_id": event.event_id, "ts": event.ts}

    @router.post("/{worker}/permission-request", dependencies=[Depends(auth_dep)])
    async def permission_request(
        worker: str, body: PermissionRequestPayload
    ) -> dict[str, Any]:
        worker = _validate_worker_name(worker)
        event = await engine.record_permission_request(worker, body)
        return {"event_id": event.event_id, "ts": event.ts}

    @router.post("/{worker}/permission-response", dependencies=[Depends(auth_dep)])
    async def permission_response(
        worker: str, body: PermissionResponsePayload
    ) -> dict[str, Any]:
        """Record a locally-resolved permission (user or hook approved/denied at the terminal)."""
        worker = _validate_worker_name(worker)
        ok = await engine.resolve_permission(worker, body.request_id, body.behavior)
        return {"ok": ok, "request_id": body.request_id}

    @router.post("/{worker}/context-pct", dependencies=[Depends(auth_dep)])
    async def context_pct(worker: str, body: dict[str, Any]) -> dict[str, Any]:
        """Manager pushes the latest context-window % parsed from its
        screen buffer. Body: `{"pct": int | null}`. Hub records on the
        worker state and fans a `context_pct` event for any global
        SSE subscribers."""
        worker = _validate_worker_name(worker)
        raw = body.get("pct")
        pct: int | None
        if raw is None:
            pct = None
        else:
            try:
                pct = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="pct must be an integer or null",
                )
            if not (0 <= pct <= 100):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="pct must be between 0 and 100",
                )
        changed = await engine.update_context_pct(worker, pct)
        return {"ok": True, "changed": changed}

    @router.post("/{worker}/active", dependencies=[Depends(auth_dep)])
    async def worker_active(worker: str, body: dict[str, Any]) -> dict[str, Any]:
        """Manager pushes the authoritative "is claude actively
        processing" signal it derives from the pty screen. Body:
        `{"is_active": bool}`. Hub stores on the worker state; the
        roster and `tm_status` use it as the primary busy/idle source,
        falling back to event-timeline decay only when the observation
        is stale or absent."""
        worker = _validate_worker_name(worker)
        raw = body.get("is_active")
        if not isinstance(raw, bool):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="is_active must be a boolean",
            )
        changed = await engine.update_active_state(worker, raw)
        return {"ok": True, "changed": changed}

    if pty_bridges is not None:
        @router.post("/{worker}/pty-out", dependencies=[Depends(auth_dep)])
        async def pty_out(worker: str, request: Request) -> dict[str, Any]:
            """Manager POSTs batched pty output bytes for this worker.

            Body is raw bytes (application/octet-stream). The hub fans
            them to every attached browser WS for this worker.
            """
            worker = _validate_worker_name(worker)
            if not pty_bridges.has_any_client(worker):
                # No browser attached — don't buffer; tell the manager so
                # it can stop streaming. Not a silent drop: explicit 404.
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="no pty clients attached",
                )
            data = await request.body()
            await pty_bridges.push_output(worker, data)
            return {"ok": True, "bytes": len(data)}

        @router.post("/{worker}/pty-control", dependencies=[Depends(auth_dep)])
        async def pty_control(
            worker: str, body: dict[str, Any]
        ) -> dict[str, Any]:
            """Manager POSTs a control message (size update, etc.).
            The hub forwards as a TEXT WS frame carrying the JSON body
            to every attached browser. Browsers parse and act.
            """
            worker = _validate_worker_name(worker)
            if not pty_bridges.has_any_client(worker):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="no pty clients attached",
                )
            await pty_bridges.push_control(worker, body)
            return {"ok": True}

    @router.get("/{worker}/stream", dependencies=[Depends(auth_dep)])
    async def stream(worker: str, request: Request):
        """SSE stream: outbound events pushed to the forwarder.

        Each event is serialized as {event: "...", data: "..."}. sse-starlette
        handles the wire format.
        """
        worker = _validate_worker_name(worker)

        async def event_source():
            # Heartbeat task to detect client disconnect
            try:
                async for msg in engine.subscribe(worker):
                    if await request.is_disconnected():
                        break
                    yield {
                        "event": msg.get("event", "message"),
                        "data": json.dumps(msg.get("data", {})),
                    }
            except asyncio.CancelledError:
                logger.debug("SSE stream cancelled for %s", worker)
                raise

        return EventSourceResponse(event_source())

    return router


__all__ = ["build_tubemail_router", "auth_dep"]
