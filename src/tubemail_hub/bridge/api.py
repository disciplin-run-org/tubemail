"""Frontend-facing HTTP and SSE endpoints.

Separate from the forwarder-facing `/tubemail/*` surface in `http.py`:
these endpoints exist for the web UI (roster, permission inbox, future
caps) and use the same bearer auth as everything else.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sse_starlette.sse import EventSourceResponse

from ..config import HubConfig, load_config, save_config
from ..recorder import RecordingManager
from .engine import BridgeEngine
from .flows import FlowStore
from .http import _validate_worker_name, auth_dep, auth_dep_with_query
from .tickets import TicketStore

logger = logging.getLogger(__name__)


def build_api_router(
    engine: BridgeEngine,
    tickets: TicketStore | None = None,
    *,
    recorder: RecordingManager | None = None,
    data_dir: Path | None = None,
) -> APIRouter:
    """JSON endpoints the web UI calls directly (roster snapshot,
    permission inbox, pty-ticket issuance). Real-time updates ride the
    SSE stream in `build_events_router`.
    """
    router = APIRouter(prefix="/api", tags=["api"])

    @router.get("/dev-bootstrap")
    async def dev_bootstrap(request: Request) -> dict[str, Any]:
        """Return the operator's bearer secret to a same-machine browser.

        Loopback-only. The browser frontend calls this once on first load
        to skip the auth gate when running on the operator's own machine
        — the common case for a local dev tool. Any non-loopback caller
        gets a 403, regardless of whether they have the secret.

        Threat model: if a process can reach this endpoint over loopback,
        it can also read the hub's environment via /proc on a Linux box,
        so this exposes nothing new. On any non-localhost deploy the
        operator pastes the bearer manually like before.

        Disabled entirely when TUBEMAIL_DISABLE_DEV_BOOTSTRAP=1, for
        operators who want to require manual paste even on localhost.
        """
        if os.environ.get("TUBEMAIL_DISABLE_DEV_BOOTSTRAP") == "1":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="dev-bootstrap disabled",
            )
        client_host = request.client.host if request.client else ""
        # IPv4 loopback, IPv6 loopback, or "localhost" hostname.
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="dev-bootstrap is loopback-only",
            )
        secret = os.environ.get("TUBEMAIL_SECRET", "")
        if not secret:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="hub has no TUBEMAIL_SECRET configured",
            )
        return {"bearer": secret}

    @router.get("/workers", dependencies=[Depends(auth_dep)])
    def list_workers() -> dict[str, Any]:
        """Roster snapshot: every known worker with its current state."""
        return {"workers": engine.list_workers()}

    @router.post(
        "/workers/{worker}/update-manager",
        dependencies=[Depends(auth_dep)],
    )
    async def update_manager(
        worker: str, force: bool = False
    ) -> dict[str, Any]:
        """Re-exec a worker's tubemail.manager process. Same safety gate
        as the `tm_update_manager` MCP tool — refuses if the worker is
        not idle unless `force=true` is passed in the query string.

        UI flow: the user clicks "Update manager" in the terminal pane's
        silent-overlay (the manager started before live-terminal support
        shipped). This endpoint dispatches the update event; the manager
        re-execs; the operator clicks Reconnect.
        """
        try:
            _validate_worker_name(worker)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid worker name",
            )
        if not force:
            ws = engine.get_worker(worker)
            state = ws.status_state() if ws is not None else "unknown"
            if state != "idle":
                return {
                    "ok": False,
                    "error": f"worker not idle (state={state}); pass force=true to override",
                    "state": state,
                }
        manager = f"{worker}-manager"
        event = await engine.enqueue_inbound(
            manager, "update_manager", {"kind": "update_manager"}
        )
        return {
            "ok": True,
            "event_id": event.event_id,
            "routed_to": manager,
        }

    @router.delete("/workers/{worker}", dependencies=[Depends(auth_dep)])
    async def purge_worker(worker: str) -> dict[str, Any]:
        """Permanently drop a worker's registry entry. Mirrors the
        `tm_purge_worker` MCP tool — the web UI uses this to clean up
        the roster from a button on a row.
        """
        try:
            _validate_worker_name(worker)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid worker name",
            )
        ok = await engine.purge_worker(worker)
        return {"ok": ok, "worker": worker}

    @router.get("/permissions", dependencies=[Depends(auth_dep)])
    def list_permissions() -> dict[str, Any]:
        """Pending permissions across online workers. Zombies from
        workers that crashed without re-registering are filtered out —
        nothing can answer those prompts anyway."""
        return {"pending": engine.list_pending_permissions(online_only=True)}

    if tickets is not None:
        @router.post("/pty-ticket", dependencies=[Depends(auth_dep)])
        async def issue_pty_ticket(body: dict[str, Any]) -> dict[str, Any]:
            """Issue a single-use 30s ticket for a pty bridge WS upgrade.

            Body: `{"worker": "<name>"}`. The bearer header stays on this
            bearer-authed POST; the returned ticket goes in the WS URL
            because browsers cannot set custom headers on
            `new WebSocket()`.
            """
            worker = str(body.get("worker", ""))
            try:
                _validate_worker_name(worker)
            except Exception:
                return {"ok": False, "error": "invalid worker name"}
            token = await tickets.issue(worker)
            return {"ok": True, "ticket": token, "ttl_s": 30, "worker": worker}

    @router.post("/permissions/resolve", dependencies=[Depends(auth_dep)])
    async def resolve_permission(body: dict[str, Any]) -> dict[str, Any]:
        """Allow or deny a pending permission by (worker, request_id)."""
        worker = str(body.get("worker", ""))
        request_id = str(body.get("request_id", ""))
        behavior = str(body.get("behavior", ""))
        if behavior not in ("allow", "deny"):
            return {"ok": False, "error": "behavior must be 'allow' or 'deny'"}
        ok = await engine.resolve_permission(worker, request_id, behavior)
        return {"ok": ok, "request_id": request_id}

    # ── settings + recording ────────────────────────────────────────────
    # Only mounted when the hub was wired with config persistence + a
    # recorder. Tests that build a bare router skip this surface.
    if data_dir is not None:
        @router.get("/settings", dependencies=[Depends(auth_dep)])
        def get_settings() -> dict[str, Any]:
            """Return the current hub settings (recording defaults, etc.)."""
            cfg = load_config(data_dir)
            return cfg.model_dump()

        @router.put("/settings", dependencies=[Depends(auth_dep)])
        def put_settings(body: dict[str, Any]) -> dict[str, Any]:
            """Update hub settings. Pass any subset of the keys returned
            by GET /api/settings; unspecified keys keep their current value.
            """
            current = load_config(data_dir).model_dump()
            current.update({k: v for k, v in body.items() if v is not None})
            try:
                cfg = HubConfig.model_validate(current)
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"invalid settings: {e}",
                )
            save_config(data_dir, cfg)
            # Apply runtime knobs to the live recorder + engine.
            engine.set_recording_default(cfg.recording_enabled_by_default)
            if recorder is not None:
                recorder.update_limits(
                    max_bytes_per_file=cfg.recording_max_bytes_per_file,
                    keep_files=cfg.recording_keep_files,
                )
            return cfg.model_dump()

    if recorder is not None:
        @router.get(
            "/workers/{worker}/recording", dependencies=[Depends(auth_dep)]
        )
        def get_recording_status(worker: str) -> dict[str, Any]:
            try:
                _validate_worker_name(worker)
            except Exception:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid worker name",
                )
            return recorder.status(worker)

        @router.post(
            "/workers/{worker}/recording", dependencies=[Depends(auth_dep)]
        )
        async def toggle_recording(
            worker: str, body: dict[str, Any]
        ) -> dict[str, Any]:
            """Body: `{"enabled": bool}`. Persists the per-worker flag and
            starts/stops the recorder."""
            try:
                _validate_worker_name(worker)
            except Exception:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid worker name",
                )
            enabled = bool(body.get("enabled", False))
            ok = await engine.set_recording_enabled(worker, enabled)
            if not ok:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"unknown worker: {worker}",
                )
            return recorder.status(worker)

        @router.get(
            "/workers/{worker}/recording/frames",
            dependencies=[Depends(auth_dep)],
        )
        def read_recording_frames(
            worker: str,
            since: str | None = None,
            until: str | None = None,
            grep: str | None = None,
            limit: int = 200,
        ) -> dict[str, Any]:
            try:
                _validate_worker_name(worker)
            except Exception:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid worker name",
                )
            limit = max(1, min(2000, int(limit)))
            frames = recorder.read_frames(
                worker, since=since, until=until, grep=grep, limit=limit + 1,
            )
            truncated = len(frames) > limit
            if truncated:
                frames = frames[:limit]
            return {"worker": worker, "frames": frames, "truncated": truncated}

    return router


def build_events_router(engine: BridgeEngine) -> APIRouter:
    """Hub-wide SSE for the web UI. One stream, all workers, all events.

    Every `_fan_out` and `_fan_out_global_only` call routes here with the
    worker name injected so the frontend can dispatch without per-worker
    subscriptions.
    """
    router = APIRouter(prefix="/api/events", tags=["events"])

    @router.get("/stream", dependencies=[Depends(auth_dep_with_query)])
    async def stream(request: Request):
        async def event_source():
            try:
                async for msg in engine.subscribe_all():
                    if await request.is_disconnected():
                        break
                    yield {
                        "event": msg.get("event", "message"),
                        "data": json.dumps({
                            "worker": msg.get("worker", ""),
                            **msg.get("data", {}),
                        }),
                    }
            except asyncio.CancelledError:
                logger.debug("global SSE cancelled")
                raise

        return EventSourceResponse(event_source())

    return router


def build_flows_router(store: FlowStore, engine: BridgeEngine) -> APIRouter:
    """REST surface for the Flow Shell — same data as the MCP tools, in
    the shape the web UI wants.
    """
    router = APIRouter(prefix="/api/flows", tags=["flows"])

    @router.get("", dependencies=[Depends(auth_dep)])
    async def list_flows() -> dict[str, Any]:
        flows = await store.list_all()
        return {"flows": [f.model_dump() for f in flows]}

    @router.post("", dependencies=[Depends(auth_dep)])
    async def save_flow(body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name", ""))
        content = str(body.get("body", ""))
        default_worker = body.get("default_worker")
        meta = body.get("meta") or {}
        if not name or not content:
            return {"ok": False, "error": "name and body required"}
        try:
            flow = await store.save(name, content, default_worker=default_worker, meta=meta)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "flow": flow.model_dump()}

    @router.delete("/{name}", dependencies=[Depends(auth_dep)])
    async def delete_flow(name: str) -> dict[str, Any]:
        ok = await store.delete(name)
        return {"ok": ok, "name": name}

    @router.post("/{name}/run", dependencies=[Depends(auth_dep)])
    async def run_flow(name: str, body: dict[str, Any]) -> dict[str, Any]:
        flow = await store.get(name)
        if flow is None:
            return {"ok": False, "error": f"flow not found: {name!r}"}
        target = body.get("worker") or flow.default_worker
        if not target:
            return {"ok": False, "error": "no target worker"}
        run_meta = {**flow.meta, **(body.get("meta") or {})}
        run_meta.setdefault("flow_name", name)
        event = await engine.enqueue_inbound(target, flow.body, run_meta)
        run_id = await store.start_run(name, target, event.event_id)
        return {
            "ok": True,
            "run_id": run_id,
            "first_event_id": event.event_id,
            "worker": target,
        }

    @router.get("/runs/{run_id}", dependencies=[Depends(auth_dep)])
    async def get_run(run_id: str) -> dict[str, Any]:
        log = await store.get_run(run_id)
        if log is None:
            return {"ok": False, "error": f"run not found: {run_id!r}"}
        return {"ok": True, "run": log.model_dump()}

    return router


__all__ = ["build_api_router", "build_events_router", "build_flows_router"]
