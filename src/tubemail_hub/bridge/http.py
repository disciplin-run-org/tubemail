"""Tubemail HTTP router.

Forwarder-facing endpoints under /tubemail/<worker>/. Bearer-auth via a
shared secret env var (TUBEMAIL_SECRET). SSE stream for server-pushed
events (channel notifications, permission responses, interrupts).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sse_starlette.sse import EventSourceResponse

from .engine import BridgeEngine
from .models import (
    InboundEvent,
    OutboundEvent,
    PermissionRequestPayload,
    RegisterRequest,
    RegisterResponse,
)

logger = logging.getLogger(__name__)


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
    if authorization.removeprefix("Bearer ").strip() != secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
        )


async def auth_dep(authorization: str | None = Header(default=None)) -> None:
    _check_auth(authorization)


def build_tubemail_router(engine: BridgeEngine) -> APIRouter:
    router = APIRouter(prefix="/tubemail", tags=["tubemail"])

    @router.post("/{worker}/register", dependencies=[Depends(auth_dep)])
    async def register(worker: str, body: RegisterRequest) -> RegisterResponse:
        cursor = await engine.register_worker(
            worker, body.cwd, forwarder_version=body.forwarder_version
        )
        return RegisterResponse(worker=worker, cursor=cursor)

    @router.post("/{worker}/unregister", dependencies=[Depends(auth_dep)])
    async def unregister(worker: str) -> dict[str, Any]:
        await engine.unregister_worker(worker)
        return {"ok": True}

    @router.post("/{worker}/goodbye", dependencies=[Depends(auth_dep)])
    async def goodbye(worker: str) -> dict[str, Any]:
        """Called by the forwarder immediately before a clean shutdown
        (user typed /exit). Marks the worker as exited_cleanly so
        list_workers can distinguish clean exits from crashes/hangs."""
        await engine.goodbye_worker(worker)
        return {"ok": True}

    @router.post("/{worker}/outbound", dependencies=[Depends(auth_dep)])
    async def outbound(worker: str, body: OutboundEvent) -> dict[str, Any]:
        event = await engine.record_outbound(worker, body.text, body.meta)
        return {"event_id": event.event_id, "ts": event.ts}

    @router.post("/{worker}/permission-request", dependencies=[Depends(auth_dep)])
    async def permission_request(
        worker: str, body: PermissionRequestPayload
    ) -> dict[str, Any]:
        event = await engine.record_permission_request(worker, body)
        return {"event_id": event.event_id, "ts": event.ts}

    @router.post("/{worker}/permission-response", dependencies=[Depends(auth_dep)])
    async def permission_response(
        worker: str, body: PermissionResponsePayload
    ) -> dict[str, Any]:
        """Record a locally-resolved permission (user or hook approved/denied at the terminal)."""
        ok = await engine.resolve_permission(worker, body.request_id, body.behavior)
        return {"ok": ok, "request_id": body.request_id}

    @router.get("/{worker}/stream", dependencies=[Depends(auth_dep)])
    async def stream(worker: str, request: Request):
        """SSE stream: outbound events pushed to the forwarder.

        Each event is serialized as {event: "...", data: "..."}. sse-starlette
        handles the wire format.
        """
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
