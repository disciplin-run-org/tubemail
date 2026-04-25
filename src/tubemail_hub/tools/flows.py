"""MCP tools for the Flow Shell. Quartermaster calls these to save and
execute named work-order templates against workers.

Endpoint shape (as documented in the engineering review, Prereq D):
    tm_save_flow(name, body, default_worker=None, meta=None)
        -> {name, saved_at}
    tm_list_flows() -> [{name, body, default_worker, created_at, last_run_at}]
    tm_run_flow(name, worker=None, meta=None)
        -> {run_id, queued_at, first_event_id}
    tm_get_run_log(run_id) -> {...}
    tm_delete_flow(name) -> {ok}
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastmcp import FastMCP

from tubemail_hub.bridge.engine import BridgeEngine
from tubemail_hub.bridge.flows import FlowStore, RunLogEntry

logger = logging.getLogger(__name__)


def register_flow_tools(mcp: FastMCP, engine: BridgeEngine, store: FlowStore) -> None:
    """Attach flow MCP tools to the given FastMCP instance."""

    @mcp.tool
    async def tm_save_flow(
        name: str,
        body: str,
        default_worker: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save a named work-order template.

        `body` is the message that will be delivered as an inbound event
        when the flow is run. `default_worker` lets you pin a flow to one
        worker (overridable at run time). `meta` is passed through to the
        event's meta field.
        """
        try:
            flow = await store.save(name, body, default_worker=default_worker, meta=meta)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "name": flow.name,
            "saved_at": flow.updated_at,
        }

    @mcp.tool
    async def tm_list_flows() -> list[dict[str, Any]]:
        """List every saved flow with a one-line summary of each."""
        flows = await store.list_all()
        return [
            {
                "name": f.name,
                "body_preview": f.body[:80] + ("…" if len(f.body) > 80 else ""),
                "default_worker": f.default_worker,
                "created_at": f.created_at,
                "updated_at": f.updated_at,
                "last_run_at": f.last_run_at,
            }
            for f in flows
        ]

    @mcp.tool
    async def tm_run_flow(
        name: str,
        worker: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a saved flow against a worker.

        Returns a `run_id` that can be passed to `tm_get_run_log` to read
        the event timeline that resulted from this invocation, and a
        `first_event_id` for use as a `tm_wait_for_activity` cursor.

        `worker` overrides the flow's `default_worker` if provided; one
        of them must be set.
        """
        flow = await store.get(name)
        if flow is None:
            return {"ok": False, "error": f"flow not found: {name!r}"}
        target = worker or flow.default_worker
        if not target:
            return {
                "ok": False,
                "error": "no target worker: pass `worker=` or set a `default_worker` on the flow",
            }
        # Merge meta: flow-level first, per-run overrides on top.
        run_meta = {**flow.meta, **(meta or {})}
        run_meta.setdefault("flow_name", name)
        event = await engine.enqueue_inbound(target, flow.body, run_meta)
        run_id = await store.start_run(name, target, event.event_id)
        return {
            "ok": True,
            "run_id": run_id,
            "first_event_id": event.event_id,
            "queued_at": event.ts,
            "worker": target,
        }

    @mcp.tool
    async def tm_get_run_log(run_id: str) -> dict[str, Any]:
        """Read a flow run's event log.

        Events are captured into the log as they arrive (outbound replies,
        permission prompts/responses) until the run is marked finished.
        For v1 a run "finishes" when the caller explicitly calls this
        tool after the worker has replied — lazy finalize, good enough.
        """
        log = await store.get_run(run_id)
        if log is None:
            return {"ok": False, "error": f"run not found: {run_id!r}"}

        # Lazy-populate events from the worker's event timeline: any
        # event on `log.worker` at ts >= log.started_at (and before
        # log.finished_at if set) gets mirrored into the run log.
        ws = engine.get_worker(log.worker)
        if ws is not None:
            start = log.started_at
            end = log.finished_at or time.time()
            for e in ws.events:
                if e.ts < start or e.ts > end:
                    continue
                if any(x.event_id == e.event_id for x in log.events):
                    continue
                await store.append_run_event(
                    run_id,
                    RunLogEntry(
                        event_id=e.event_id, ts=e.ts, kind=e.kind,
                        content=e.content, meta=e.meta,
                    ),
                )
            log = await store.get_run(run_id) or log

        return {
            "ok": True,
            "run_id": log.run_id,
            "flow_name": log.flow_name,
            "worker": log.worker,
            "started_at": log.started_at,
            "finished_at": log.finished_at,
            "events": [e.model_dump() for e in log.events],
        }

    @mcp.tool
    async def tm_finish_run(run_id: str) -> dict[str, Any]:
        """Mark a flow run as finished. Stops the run log from growing
        on subsequent `tm_get_run_log` calls.
        """
        log = await store.get_run(run_id)
        if log is None:
            return {"ok": False, "error": f"run not found: {run_id!r}"}
        await store.finish_run(run_id)
        return {"ok": True, "run_id": run_id, "finished_at": time.time()}

    @mcp.tool
    async def tm_delete_flow(name: str) -> dict[str, Any]:
        """Delete a saved flow. Past run logs are preserved."""
        ok = await store.delete(name)
        return {"ok": ok, "name": name}
