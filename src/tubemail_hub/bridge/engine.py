"""BridgeEngine: per-worker state and event fan-out for tubemail.

Single in-memory authority for everything the tubemail HTTP routes and the
orchestrator MCP tools need to read or write. JSON persistence under
/data/tubemail/workers/<name>.json with atomic writes.

Event fan-out uses asyncio.Queue per subscriber so multiple SSE streams
(e.g. reconnecting forwarders, a future web UI) can receive the same
outgoing event without racing. Subscribers get any event *after* their
subscription time — historical replay is via the stored event list.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, AsyncIterator

from .models import (
    PermissionRequestPayload,
    PermissionResponsePayload,
    WorkerEvent,
    WorkerState,
)

_EVENT_ID_ALPHABET = "0123456789abcdefghijkmnpqrstuvwxyz"  # no 'l' or 'o' for clarity


def _new_event_id() -> str:
    return "".join(secrets.choice(_EVENT_ID_ALPHABET) for _ in range(10))


def _new_permission_request_id() -> str:
    """Permission relay spec: five lowercase letters, alphabet minus 'l'."""
    alphabet = "abcdefghijkmnopqrstuvwxyz"
    return "".join(secrets.choice(alphabet) for _ in range(5))


class BridgeEngine:
    """Holds all tubemail state in memory, persists to JSON on disk."""

    def __init__(self, data_dir: Path | str = "/data/tubemail"):
        self._data_dir = Path(data_dir)
        self._workers_dir = self._data_dir / "workers"
        self._workers_dir.mkdir(parents=True, exist_ok=True)
        self._workers: dict[str, WorkerState] = {}
        # One queue per active SSE subscription. worker -> list of queues.
        self._outbound_subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()
        self._load_all()

    # ── persistence ──────────────────────────────────────────────────────────

    def _worker_file(self, name: str) -> Path:
        return self._workers_dir / f"{name}.json"

    def _load_all(self) -> None:
        for path in self._workers_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                ws = WorkerState.model_validate(data)
                self._workers[ws.name] = ws
            except Exception:
                continue

    def _persist(self, name: str) -> None:
        ws = self._workers.get(name)
        if ws is None:
            return
        path = self._worker_file(name)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(ws.model_dump_json(indent=2))
        os.replace(tmp, path)

    # ── worker lifecycle ─────────────────────────────────────────────────────

    async def register_worker(
        self, name: str, cwd: str, forwarder_version: str | None = None
    ) -> str:
        """Register or refresh a worker. Returns the cursor to resume from.

        Clears any stale pending permissions — if the forwarder is re-registering,
        the old Claude session that generated those prompts is gone.
        """
        async with self._lock:
            ws = self._workers.get(name)
            now = time.time()
            if ws is None:
                ws = WorkerState(
                    name=name, cwd=cwd, registered_at=now, last_activity=now,
                    forwarder_version=forwarder_version or "",
                )
                self._workers[name] = ws
            else:
                ws.cwd = cwd or ws.cwd
                ws.last_activity = now
                if forwarder_version:
                    ws.forwarder_version = forwarder_version
                if ws.pending_permissions:
                    ws.pending_permissions.clear()
                # Re-registering = new session starting; any previous clean
                # exit flag no longer reflects current state.
                ws.exited_cleanly = False
            self._persist(name)
            # Cursor is the most recent event id, or empty string
            return ws.events[-1].event_id if ws.events else ""

    async def unregister_worker(self, name: str) -> None:
        async with self._lock:
            # Keep persistent state; just close subscribers
            for q in self._outbound_subscribers.pop(name, []):
                await q.put({"event": "closed", "data": {}})

    async def goodbye_worker(self, name: str) -> None:
        """Mark the worker as having exited cleanly (user typed /exit)
        and close its subscribers. Differs from unregister_worker in that
        list_workers will render the worker as "exited cleanly" instead
        of the ambiguous "offline"."""
        async with self._lock:
            ws = self._workers.get(name)
            if ws is not None:
                ws.exited_cleanly = True
                ws.last_activity = time.time()
                self._persist(name)
            for q in self._outbound_subscribers.pop(name, []):
                await q.put({"event": "closed", "data": {}})

    def is_online(self, name: str) -> bool:
        """True if the worker has at least one active SSE subscriber (forwarder connected)."""
        subs = self._outbound_subscribers.get(name, [])
        return len(subs) > 0

    def list_workers(self) -> list[dict[str, Any]]:
        return [
            {
                "name": ws.name,
                "cwd": ws.cwd,
                "online": self.is_online(ws.name),
                "state": ws.status_state(),
                "last_activity": ws.last_activity,
                "pending_count": len(ws.pending_permissions),
                "event_count": len(ws.events),
                "forwarder_version": ws.forwarder_version,
                "exited_cleanly": ws.exited_cleanly,
            }
            for ws in self._workers.values()
        ]

    def get_worker(self, name: str) -> WorkerState | None:
        return self._workers.get(name)

    # ── event recording (from orchestrator or worker) ────────────────────────

    async def enqueue_inbound(
        self, worker: str, content: str, meta: dict[str, Any] | None = None
    ) -> WorkerEvent:
        """Orchestrator sends a message → worker inbox. Fans out to SSE subs."""
        async with self._lock:
            ws = self._workers.get(worker)
            if ws is None:
                ws = WorkerState(name=worker, registered_at=time.time())
                self._workers[worker] = ws
            event = WorkerEvent(
                event_id=_new_event_id(),
                ts=time.time(),
                kind="inbound",
                content=content,
                meta=meta or {},
            )
            ws.events.append(event)
            ws.last_activity = event.ts
            self._persist(worker)
        await self._fan_out(worker, {"event": "channel_event", "data": {
            "content": content,
            "meta": meta or {},
        }})
        return event

    async def record_outbound(
        self, worker: str, text: str, meta: dict[str, Any] | None = None
    ) -> WorkerEvent:
        """Worker replied via its reply tool → store for orchestrator to poll."""
        async with self._lock:
            ws = self._workers.get(worker)
            if ws is None:
                ws = WorkerState(name=worker, registered_at=time.time())
                self._workers[worker] = ws
            event = WorkerEvent(
                event_id=_new_event_id(),
                ts=time.time(),
                kind="outbound",
                content=text,
                meta=meta or {},
            )
            ws.events.append(event)
            ws.last_activity = event.ts
            self._persist(worker)
            return event

    async def record_permission_request(
        self, worker: str, payload: PermissionRequestPayload
    ) -> WorkerEvent:
        """Worker's Claude Code is waiting for tool approval."""
        async with self._lock:
            ws = self._workers.get(worker)
            if ws is None:
                ws = WorkerState(name=worker, registered_at=time.time())
                self._workers[worker] = ws
            # Dedupe by request_id
            if not any(p.request_id == payload.request_id for p in ws.pending_permissions):
                ws.pending_permissions.append(payload)
            event = WorkerEvent(
                event_id=_new_event_id(),
                ts=time.time(),
                kind="permission_request",
                content=payload.tool_name,
                meta={
                    "request_id": payload.request_id,
                    "description": payload.description,
                    "input_preview": payload.input_preview,
                },
            )
            ws.events.append(event)
            ws.last_activity = event.ts
            self._persist(worker)
            return event

    async def resolve_permission(
        self, worker: str, request_id: str, behavior: str
    ) -> bool:
        """Orchestrator decides on a pending permission. Fans out to SSE."""
        async with self._lock:
            ws = self._workers.get(worker)
            if ws is None:
                return False
            found = None
            remaining = []
            for p in ws.pending_permissions:
                if p.request_id == request_id and found is None:
                    found = p
                else:
                    remaining.append(p)
            if found is None:
                return False
            ws.pending_permissions = remaining
            event = WorkerEvent(
                event_id=_new_event_id(),
                ts=time.time(),
                kind="permission_response",
                content=behavior,
                meta={"request_id": request_id},
            )
            ws.events.append(event)
            ws.last_activity = event.ts
            self._persist(worker)
        await self._fan_out(worker, {"event": "permission_response", "data": {
            "request_id": request_id,
            "behavior": behavior,
        }})
        return True

    async def send_interrupt(self, worker: str) -> WorkerEvent:
        async with self._lock:
            ws = self._workers.get(worker)
            if ws is None:
                ws = WorkerState(name=worker, registered_at=time.time())
                self._workers[worker] = ws
            event = WorkerEvent(
                event_id=_new_event_id(),
                ts=time.time(),
                kind="interrupt",
                content="",
                meta={},
            )
            ws.events.append(event)
            ws.last_activity = event.ts
            self._persist(worker)
        await self._fan_out(worker, {"event": "interrupt", "data": {}})
        return event

    # ── queries ──────────────────────────────────────────────────────────────

    def events_since(
        self, worker: str, since: str | None = None, limit: int = 100
    ) -> list[WorkerEvent]:
        ws = self._workers.get(worker)
        if ws is None:
            return []
        if not since:
            return ws.events[-limit:]
        start = 0
        for i, e in enumerate(ws.events):
            if e.event_id == since:
                start = i + 1
                break
        return ws.events[start : start + limit]

    def list_pending_permissions(
        self, worker: str | None = None
    ) -> list[dict[str, Any]]:
        out = []
        for ws in self._workers.values():
            if worker and ws.name != worker:
                continue
            for p in ws.pending_permissions:
                out.append({
                    "worker": ws.name,
                    "request_id": p.request_id,
                    "tool_name": p.tool_name,
                    "description": p.description,
                    "input_preview": p.input_preview,
                })
        return out

    # ── SSE fan-out ──────────────────────────────────────────────────────────

    async def _fan_out(self, worker: str, message: dict[str, Any]) -> None:
        subs = self._outbound_subscribers.get(worker, [])
        for q in subs:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, worker: str) -> AsyncIterator[dict[str, Any]]:
        """Yield SSE events pushed to this worker after subscription time.

        Intended to back a StreamingResponse. Caller iterates until the
        client disconnects.

        If an active subscriber already exists for this worker name, the
        older subscription is evicted — only the newest forwarder receives
        events. Prevents silent fan-out to duplicate sessions that caused
        inconsistent replies (2026-04-16 incident).
        """
        import logging
        _log = logging.getLogger(__name__)
        existing = self._outbound_subscribers.get(worker, [])
        if existing:
            _log.warning(
                "subscribe: evicting %d existing subscriber(s) for %s — duplicate session detected",
                len(existing), worker,
            )
            for old_q in list(existing):
                try:
                    old_q.put_nowait({"event": "closed", "data": {"reason": "superseded"}})
                except asyncio.QueueFull:
                    pass
            self._outbound_subscribers[worker] = []

        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._outbound_subscribers.setdefault(worker, []).append(q)
        try:
            while True:
                msg = await q.get()
                if msg.get("event") == "closed":
                    break
                yield msg
        finally:
            subs = self._outbound_subscribers.get(worker, [])
            if q in subs:
                subs.remove(q)

    async def wait_for_activity(
        self, worker: str, since: str | None, timeout_s: float = 30.0
    ) -> list[WorkerEvent]:
        """Block until a new event arrives or timeout. Returns new events."""
        existing = self.events_since(worker, since)
        if existing:
            return existing
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.2)
            new = self.events_since(worker, since)
            if new:
                return new
        return []
