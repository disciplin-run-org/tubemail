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
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

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

    def __init__(
        self,
        data_dir: Path | str = "/data/tubemail",
        *,
        recorder: Any | None = None,
        recording_default: bool = False,
        pty_bridges: Any | None = None,
    ):
        self._data_dir = Path(data_dir)
        self._workers_dir = self._data_dir / "workers"
        self._workers_dir.mkdir(parents=True, exist_ok=True)
        # Optional RecordingManager. Engine doesn't import it directly to
        # avoid a circular module ref; server.py constructs both and wires
        # the recorder in here. When None, recording is a no-op.
        self._recorder = recorder
        self._recording_default = recording_default
        # Optional PtyBridgeRegistry. Used so recording-toggle paths can
        # decide whether to fire pty_detach (only when no browsers also
        # want the stream).
        self._pty_bridges = pty_bridges
        self._workers: dict[str, WorkerState] = {}
        # One queue per active SSE subscription. worker -> list of queues.
        # Used for the forwarder-facing per-worker SSE at
        # `/tubemail/<worker>/stream` — each forwarder subscribes to its own
        # worker and receives events destined for that worker.
        self._outbound_subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        # Global subscriber queues for the frontend-facing SSE at
        # `/api/events/stream`. Each queue receives every event across every
        # worker, with the worker name injected into the message. Consumers:
        # the web UI roster + activity feed.
        self._global_subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._lock = asyncio.Lock()
        self._load_all()
        # Drop pending_permission entries that disk says are pending but
        # subsequent worker outbound activity proves were resolved. Without
        # this, a hub blip at the moment a permission was answered locally
        # leaves the entry pending forever — see _sweep_stale_for_worker
        # for the proven-stuck heuristic.
        self._sweep_stale_permissions_on_load()

    # ── persistence ──────────────────────────────────────────────────────────

    # Defense in depth: the HTTP layer validates worker names, but the engine
    # is also directly reachable from tests and future tools. Enforce the same
    # shape here so no code path can write outside the workers directory via
    # a crafted name.
    _WORKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")

    @classmethod
    def _check_worker_name(cls, name: str) -> None:
        if not cls._WORKER_NAME_RE.fullmatch(name) or ".." in name:
            raise ValueError(f"invalid worker name: {name!r}")

    def _worker_file(self, name: str) -> Path:
        self._check_worker_name(name)
        return self._workers_dir / f"{name}.json"

    def _load_all(self) -> None:
        for path in self._workers_dir.glob("*.json"):
            # Skip files whose stem doesn't match the worker-name shape —
            # they cannot have been written by a legitimate register call,
            # so trusting them as state would be a policy violation.
            if not self._WORKER_NAME_RE.fullmatch(path.stem) or ".." in path.stem:
                logging.getLogger(__name__).warning(
                    "_load_all: skipping state file with invalid name: %s", path.name
                )
                continue
            try:
                data = json.loads(path.read_text())
                ws = WorkerState.model_validate(data)
                self._workers[ws.name] = ws
                # If the worker had recording on when the hub last shut down,
                # reopen the recorder so output streams to a fresh file pair.
                # Each register() will start a new session anyway; this just
                # makes "browser-only" workers (no manager re-register yet)
                # still appear in `tm_recording_status`.
                if ws.recording_enabled and self._recorder is not None:
                    try:
                        self._recorder.start(ws.name)
                    except Exception as e:
                        logging.getLogger(__name__).warning(
                            "_load_all: failed to resume recording for %s: %s",
                            ws.name, e,
                        )
            except Exception as e:
                # Do not silently swallow — a corrupt state file is a signal
                # (disk corruption, schema drift, or tampering) and must be
                # observable. Skip this one and continue loading the rest.
                logging.getLogger(__name__).warning(
                    "_load_all: failed to parse %s: %s", path.name, e
                )

    # Worker outbound event kinds that prove the LLM's session resumed
    # past a permission gate. If any of these were posted strictly AFTER
    # a permission_request's timestamp, the request was answered locally
    # — the channel just failed to forward the resolution to the hub.
    # Anything in the worker's own `outbound` (Stop hook relay, ack tool,
    # explicit reply, the channel's own permission_response posting that
    # DID make it through) qualifies; an `interrupt` does not, because
    # an interrupt is what gets sent TO the worker to break it out of a
    # gate, not proof of progress past one.
    _PROOF_OF_RESUMED_KINDS = frozenset({"outbound", "permission_response"})

    # Brand-new permission requests need a grace window before the
    # sweeper considers them "stuck" — otherwise a request received
    # microseconds before a stop_relay outbound could be wrongly evicted.
    # 60s is conservative; permission prompts that genuinely need an
    # answer sit pending for many minutes.
    _SWEEP_GRACE_S = 60.0

    def _sweep_stale_for_worker(
        self, name: str, *, now: float | None = None, persist: bool = True
    ) -> int:
        """Drop pending_permission entries on `name` that the event timeline
        proves were resolved. Returns the number of entries dropped.

        For each pending entry, find its corresponding `permission_request`
        event in `ws.events`. If any worker outbound event with a kind in
        :attr:`_PROOF_OF_RESUMED_KINDS` was recorded after that ts, the
        permission must have been answered locally (the LLM cannot have
        produced subsequent output while still blocked on a permission
        prompt). Drop the pending entry and persist.

        Entries with no matching event in the timeline are left alone if
        they are within the grace window since `last_activity`; they may
        be brand-new requests whose event hasn't been appended yet from a
        racing call. Older orphans are dropped — they cannot be resolved
        because the worker's event log no longer references them.

        `persist=False` skips the per-sweep disk write — used by callers
        that are about to persist anyway (e.g. `record_outbound`) so the
        worker file is written once, not twice, on the common case.

        Caller must hold the engine lock OR be running before any async
        access (e.g. inside :meth:`__init__`).
        """
        ws = self._workers.get(name)
        if ws is None or not ws.pending_permissions:
            return 0
        now = now if now is not None else time.time()
        # Index permission_request events by request_id once so the
        # per-pending lookup is O(1) instead of O(n*m).
        request_ts: dict[str, float] = {}
        for ev in ws.events:
            if ev.kind == "permission_request":
                rid = ev.meta.get("request_id")
                if isinstance(rid, str) and rid:
                    request_ts[rid] = ev.ts
        # Pre-compute outbound timestamps once. We only need the LATEST
        # qualifying outbound — anything earlier than that can't help.
        latest_proof_ts = 0.0
        for ev in ws.events:
            if ev.kind in self._PROOF_OF_RESUMED_KINDS and ev.ts > latest_proof_ts:
                latest_proof_ts = ev.ts
        kept: list[Any] = []
        dropped = 0
        for p in ws.pending_permissions:
            req_ts = request_ts.get(p.request_id)
            if req_ts is None:
                # Orphan: no matching permission_request in events. Could
                # be brand-new (event append racing) or genuinely lost.
                # Use last_activity as the floor — if the worker has been
                # quiet for the grace window, the orphan is stale.
                age = now - ws.last_activity if ws.last_activity else 0.0
                if age > self._SWEEP_GRACE_S:
                    dropped += 1
                    continue
                kept.append(p)
                continue
            if latest_proof_ts > req_ts and (now - req_ts) > self._SWEEP_GRACE_S:
                dropped += 1
                continue
            kept.append(p)
        if dropped:
            ws.pending_permissions = kept
            if persist:
                self._persist(name)
            logging.getLogger(__name__).info(
                "sweep_stale_permissions: dropped %d proven-resolved "
                "pending_permissions on worker %s", dropped, name,
            )
        return dropped

    def _sweep_stale_permissions_on_load(self) -> None:
        """Run the sweeper across every loaded worker at hub startup.

        Sync because :meth:`__init__` is sync and no other coroutine can
        be touching the engine yet — the lock isn't needed and acquiring
        it from sync code would deadlock if it existed in this state. A
        runtime caller should use :meth:`sweep_stale_permissions` /
        :meth:`sweep_stale_permissions_all` instead.
        """
        for name in list(self._workers.keys()):
            try:
                self._sweep_stale_for_worker(name)
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "sweep_stale_permissions_on_load: worker %s raised %s",
                    name, e,
                )

    async def sweep_stale_permissions(self, name: str) -> int:
        """Async wrapper for runtime callers (e.g. admin tool)."""
        async with self._lock:
            return self._sweep_stale_for_worker(name)

    async def sweep_stale_permissions_all(self) -> dict[str, int]:
        """Sweep every known worker. Returns ``{worker: dropped_count}``
        for workers where at least one entry was dropped (clean workers
        are omitted to keep the response readable)."""
        async with self._lock:
            results: dict[str, int] = {}
            for name in list(self._workers.keys()):
                try:
                    n = self._sweep_stale_for_worker(name)
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "sweep_stale_permissions_all: worker %s raised %s",
                        name, e,
                    )
                    continue
                if n:
                    results[name] = n
            return results

    def _persist(self, name: str) -> None:
        ws = self._workers.get(name)
        if ws is None:
            return
        path = self._worker_file(name)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(ws.model_dump_json(indent=2))
        os.replace(tmp, path)

    def _load_from_disk(self, name: str) -> WorkerState | None:
        """Read a single worker's state file from disk if present and valid.
        Returns the parsed WorkerState or None on missing/corrupt file.

        Used by `_get_or_create_worker` as the second step before falling
        back to a fresh state — without this, any code path that hits
        `if ws is None:` on a worker missing from `_workers` (eviction,
        fresh process pre-_load_all, race against a partial restart)
        creates an empty state and overwrites the on-disk file with it,
        losing every event the worker had.

        Caller holds `_lock`. Failure modes (missing file, malformed JSON,
        schema drift) all return None — the caller will fall back to
        fresh state, same behaviour as before this helper existed.
        """
        try:
            path = self._worker_file(name)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return WorkerState.model_validate(data)
        except Exception as e:
            logging.getLogger(__name__).warning(
                "_load_from_disk(%s): file present but unreadable (%s); "
                "caller will create fresh state",
                name, e,
            )
            return None

    def _get_or_create_worker(
        self, name: str, *, defaults: dict[str, Any] | None = None
    ) -> WorkerState:
        """Resolve a worker state, preferring (in order): in-memory cache,
        on-disk file, fresh state. Inserts the resolved state into the
        in-memory cache. Caller holds `_lock`.

        This is the durability fix for QM #207 (queue 187 dogfooding):
        before this helper, every record_*/enqueue_inbound path that
        found `_workers[name]` empty would create a fresh WorkerState
        and `_persist` it — overwriting the on-disk file with empty
        events. A single eviction (or a process restart race that left
        the file on disk but the in-memory cache empty) erased every
        prior event for that worker silently. With this helper, the
        on-disk file is consulted first, so an eviction is a no-op.
        """
        ws = self._workers.get(name)
        if ws is not None:
            return ws
        ws = self._load_from_disk(name)
        if ws is not None:
            self._workers[name] = ws
            return ws
        # Truly new worker. Fresh state with the defaults the caller
        # supplied (most callers want registered_at=now and that's all).
        kwargs: dict[str, Any] = {"name": name, "registered_at": time.time()}
        if defaults:
            kwargs.update(defaults)
        ws = WorkerState(**kwargs)
        self._workers[name] = ws
        return ws

    # ── worker lifecycle ─────────────────────────────────────────────────────

    async def register_worker(
        self, name: str, cwd: str, forwarder_version: str | None = None
    ) -> str:
        """Register or refresh a worker. Returns the cursor to resume from.

        Clears any stale pending permissions — if the forwarder is re-registering,
        the old Claude session that generated those prompts is gone.
        """
        self._check_worker_name(name)
        async with self._lock:
            # Prefer disk to fresh state — see _get_or_create_worker for why.
            # is_new is true only when neither cache nor disk had this worker.
            had_in_memory = name in self._workers
            ws = self._get_or_create_worker(
                name,
                defaults={
                    "cwd": cwd, "last_activity": time.time(),
                    "forwarder_version": forwarder_version or "",
                    "recording_enabled": self._recording_default,
                },
            )
            now = time.time()
            # Whether this register is "new" depends on whether we had ANY
            # prior state (memory or disk), not just memory. A re-register
            # that hit disk preserves history but is not a new worker.
            is_new = (not had_in_memory) and not ws.events and ws.registered_at >= now - 1.0
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
            cursor = ws.events[-1].event_id if ws.events else ""
        # Sync the recorder to the worker's flag. Idempotent: if already
        # recording, keep the same file (don't rotate on every re-register).
        # Workers re-register frequently — both during forwarder reconnect
        # storms and after hub restarts — and rotating a file on each call
        # produces a flood of empty .cast files without any benefit.
        if self._recorder is not None:
            if ws.recording_enabled:
                if not self._recorder.is_recording(name):
                    try:
                        self._recorder.start(name)
                    except Exception as e:
                        logging.getLogger(__name__).warning(
                            "register_worker: recorder.start failed for %s: %s",
                            name, e,
                        )
            else:
                # Defensive: if a worker was recording then turned off and
                # the manager now re-registers, make sure we don't keep
                # writing.
                try:
                    self._recorder.stop(name)
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "register_worker: recorder.stop failed for %s: %s",
                        name, e,
                    )
        # Tell the worker's manager to start (or keep) streaming pty bytes
        # to the hub. Skip for `<X>-manager` registrations themselves —
        # managers don't have managers.
        if not name.endswith("-manager"):
            await self._sync_pty_stream(name)
        return cursor

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
                "recording_enabled": ws.recording_enabled,
                "context_pct": ws.context_pct,
            }
            for ws in self._workers.values()
        ]

    def get_worker(self, name: str) -> WorkerState | None:
        return self._workers.get(name)

    # ── recording knobs ──────────────────────────────────────────────────

    @property
    def recorder(self) -> Any | None:
        """The wired RecordingManager, or None if recording is disabled."""
        return self._recorder

    def set_recording_default(self, enabled: bool) -> None:
        """Update the global default applied to brand-new workers. Existing
        workers keep their per-worker setting."""
        self._recording_default = bool(enabled)

    async def update_active_state(self, name: str, is_active: bool) -> bool:
        """Record the manager's authoritative busy/idle observation for
        a worker. Returns True if the value changed (caller can fan out
        a UI ping if so), False otherwise.

        Called from POST /tubemail/<worker>/active. The manager parses
        claude's TUI for spinner / running-timer markers every 3s and
        pushes the boolean only on change, so this method gets called
        rarely under normal operation."""
        self._check_worker_name(name)
        async with self._lock:
            ws = self._workers.get(name)
            if ws is None:
                return False
            now = time.time()
            ws.observed_active_at = now
            ws.last_activity = now
            if ws.observed_active == is_active:
                # Heartbeat-only — refresh the freshness window without
                # firing a global event (no UI state change).
                self._persist(name)
                return False
            ws.observed_active = is_active
            self._persist(name)
        # State actually flipped. Quiet global ping so the roster
        # re-fetches without waiting on the next event.
        await self._fan_out_global_only(name, {
            "event": "active_state",
            "data": {"is_active": is_active},
        })
        return True

    async def update_context_pct(self, name: str, pct: int | None) -> bool:
        """Record the worker's most recent context-window % from its TUI
        status bar. Returns True if the worker exists and the value
        changed (so callers can choose to fan out a global UI update),
        False otherwise.

        Called from POST /tubemail/<worker>/context-pct, which the
        manager hits whenever it parses a new value out of its screen
        buffer.
        """
        self._check_worker_name(name)
        async with self._lock:
            ws = self._workers.get(name)
            if ws is None:
                return False
            new_value = None if pct is None else int(pct)
            if ws.context_pct == new_value:
                return False
            ws.context_pct = new_value
            ws.last_activity = time.time()
            self._persist(name)
        # Quiet global ping: lets the frontend re-fetch the roster so
        # the new context % shows up without waiting for the next event.
        await self._fan_out_global_only(name, {
            "event": "context_pct",
            "data": {"context_pct": new_value},
        })
        return True

    async def set_recording_enabled(self, name: str, enabled: bool) -> bool:
        """Toggle recording for a single worker. Persists the flag and starts
        or stops the recorder. Returns True if the worker was found, False
        otherwise.

        Also fires pty_attach / pty_detach SSE events to the worker's
        manager so the pty stream actually flows. Without this, recording
        stays silent on workers that have no browser attached — the
        manager only streams pty bytes when something explicitly asks
        for them. pty_attach is idempotent on the manager side, so
        firing it when a browser is already attached is a no-op +
        redraw. pty_detach is only fired when no browsers also want
        the stream.
        """
        self._check_worker_name(name)
        async with self._lock:
            ws = self._workers.get(name)
            if ws is None:
                return False
            ws.recording_enabled = bool(enabled)
            self._persist(name)
        if self._recorder is not None:
            try:
                if enabled:
                    self._recorder.start(name)
                else:
                    self._recorder.stop(name)
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "set_recording_enabled: recorder toggle failed for %s: %s",
                    name, e,
                )
        await self._sync_pty_stream(name)
        return True

    async def _sync_pty_stream(self, name: str) -> None:
        """Fire pty_attach or pty_detach to the named worker's manager so
        the stream state matches "any subscriber wants pty bytes."

        Subscribers today: browser pty WS clients + the recording
        toggle. If either wants the stream, manager streams. If
        neither does, manager stops.
        """
        manager = f"{name}-manager"
        wants_recording = (
            self._recorder is not None and self._recorder.is_recording(name)
        )
        has_browsers = (
            self._pty_bridges is not None
            and self._pty_bridges.attached_count(name) > 0
        )
        if wants_recording or has_browsers:
            await self._fan_out(manager, {"event": "pty_attach", "data": {}})
        else:
            await self._fan_out(manager, {"event": "pty_detach", "data": {}})

    # ── event recording (from orchestrator or worker) ────────────────────────

    async def enqueue_inbound(
        self, worker: str, content: str, meta: dict[str, Any] | None = None
    ) -> WorkerEvent:
        """Orchestrator sends a message → worker inbox. Fans out to SSE subs."""
        async with self._lock:
            ws = self._get_or_create_worker(worker)
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
            ws = self._get_or_create_worker(worker)
            event = WorkerEvent(
                event_id=_new_event_id(),
                ts=time.time(),
                kind="outbound",
                content=text,
                meta=meta or {},
            )
            ws.events.append(event)
            ws.last_activity = event.ts
            # Auto-sweep: this outbound is proof the LLM passed any prior
            # permission gate. Drop proven-resolved pending entries now
            # instead of waiting for the next admin call or hub restart.
            # The grace window inside _sweep_stale_for_worker protects new
            # requests from being evicted racing a parallel outbound.
            # Why this matters: when a hook (e.g. auto-approve-safe.sh)
            # short-circuits a permission locally and Claude Code runs the
            # tool without round-tripping a `permission` notification, no
            # permission_response ever reaches the hub — the entry would
            # otherwise persist forever.
            if ws.pending_permissions:
                self._sweep_stale_for_worker(worker, now=event.ts, persist=False)
            self._persist(worker)
        # Global-only fan-out: the event originated from the forwarder (this
        # endpoint is called by its POST /outbound), so don't echo it back.
        # The frontend global SSE needs to see worker replies to update the
        # roster's state (trailing outbound → idle) and any activity feed.
        await self._fan_out_global_only(worker, {
            "event": "outbound",
            "data": {"event_id": event.event_id, "content": text, "meta": meta or {}},
        })
        return event

    async def record_permission_request(
        self, worker: str, payload: PermissionRequestPayload
    ) -> WorkerEvent:
        """Worker's Claude Code is waiting for tool approval."""
        async with self._lock:
            ws = self._get_or_create_worker(worker)
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
        # Global-only fan-out: event originated from the forwarder, don't
        # echo back. Frontend Permission Inbox needs this to light up.
        await self._fan_out_global_only(worker, {
            "event": "permission_request",
            "data": {
                "event_id": event.event_id,
                "request_id": payload.request_id,
                "tool_name": payload.tool_name,
                "description": payload.description,
                "input_preview": payload.input_preview,
            },
        })
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
            ws = self._get_or_create_worker(worker)
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
        self,
        worker: str | None = None,
        *,
        online_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Pending-permission list. By default returns everything on disk
        (including stale entries from workers that crashed without
        re-registering). Pass `online_only=True` for a live-state view —
        entries from workers with no active SSE subscription are filtered
        out. `/health` and the web UI use the live view; admin / debug
        tools that want to see zombies use the default.
        """
        out = []
        for ws in self._workers.values():
            if worker and ws.name != worker:
                continue
            if online_only and not self.is_online(ws.name):
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

    def count_online_workers(self) -> int:
        """Number of *worker* sessions online right now. Excludes
        `-manager` entries — those are infrastructure, not sessions
        humans count. The web UI roster also filters managers out
        (they render inline as the mgr column), so this count must
        match what the user sees in the table.
        """
        return sum(
            1 for ws in self._workers.values()
            if self.is_online(ws.name) and not ws.name.endswith("-manager")
        )

    async def purge_worker(self, name: str) -> bool:
        """Remove a worker entirely — in-memory state, on-disk file, any
        residual subscribers. Returns True if a worker was found and
        removed, False otherwise. Use this to drain dead-session
        registry entries that have accumulated over time.
        """
        async with self._lock:
            ws = self._workers.pop(name, None)
            for q in self._outbound_subscribers.pop(name, []):
                try:
                    q.put_nowait({"event": "closed", "data": {"reason": "purged"}})
                except Exception:
                    pass
            if ws is None:
                # Even if we have no in-memory state, a stale file might
                # exist on disk — drop it. _worker_file validates the
                # name, so a malformed name still can't escape.
                try:
                    path = self._worker_file(name)
                except ValueError:
                    return False
                if path.exists():
                    path.unlink()
                    return True
                return False
            try:
                path = self._worker_file(name)
                if path.exists():
                    path.unlink()
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "purge_worker: failed to remove %s state file: %s", name, e
                )
            return True

    async def purge_stale_workers(
        self, max_age_s: float = 86400.0, now: float | None = None
    ) -> list[str]:
        """Drop offline workers whose last_activity is older than
        `max_age_s`. Default 24h. Online workers and ones with pending
        permissions are never purged regardless of age — those are
        load-bearing state. Returns names that were purged.

        Called on hub startup so the registry doesn't grow indefinitely
        as workers come and go over weeks. Manual purge of a specific
        worker uses purge_worker() / DELETE /api/workers/<name>.
        """
        cutoff = (now if now is not None else time.time()) - max_age_s
        candidates: list[str] = []
        for name, ws in list(self._workers.items()):
            if self.is_online(name):
                continue
            if ws.pending_permissions:
                continue
            if ws.last_activity > cutoff:
                continue
            candidates.append(name)
        for name in candidates:
            await self.purge_worker(name)
        if candidates:
            logging.getLogger(__name__).info(
                "purge_stale_workers: dropped %d offline workers older than %.0fs",
                len(candidates), max_age_s,
            )
        return candidates

    # ── SSE fan-out ──────────────────────────────────────────────────────────

    async def _fan_out(self, worker: str, message: dict[str, Any]) -> None:
        """Push a message to the forwarder-facing per-worker SSE subscribers
        AND to the hub-wide global subscribers (with worker injected).

        Call this for events that the worker's forwarder should see (e.g.
        `channel_event` inbound, `permission_response`, `interrupt`).

        For events that originate from the forwarder itself (outbound replies,
        permission requests), use `_fan_out_global_only` instead to avoid
        echoing the event back to the sender.
        """
        # Forwarder-facing (per-worker)
        subs = self._outbound_subscribers.get(worker, [])
        for q in subs:
            self._safe_put(q, message, worker, kind="forwarder")
        # Frontend-facing (global, with worker injected)
        self._fan_out_global_only_sync(worker, message)

    def _fan_out_global_only_sync(
        self, worker: str, message: dict[str, Any]
    ) -> None:
        """Push to global subscribers only. Used for events that come FROM
        the forwarder (outbound, permission_request) so they reach the web
        UI's global SSE without being echoed back to the forwarder.
        """
        if not self._global_subscribers:
            return
        # Inject worker so the frontend can route without state.
        msg = {**message, "worker": worker}
        for q in self._global_subscribers:
            self._safe_put(q, msg, worker, kind="global")

    async def _fan_out_global_only(
        self, worker: str, message: dict[str, Any]
    ) -> None:
        """Async-form of `_fan_out_global_only_sync` for call-site symmetry
        with `_fan_out`. No awaited work; wraps the sync helper."""
        self._fan_out_global_only_sync(worker, message)

    @staticmethod
    def _safe_put(
        q: asyncio.Queue[dict[str, Any]],
        message: dict[str, Any],
        worker: str,
        kind: str,
    ) -> None:
        """Put without silently dropping on QueueFull — log + sentinel so
        the slow subscriber's iterator terminates cleanly.
        """
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            logging.getLogger(__name__).warning(
                "_fan_out: %s subscriber queue full for %s — closing slow subscriber",
                kind, worker,
            )
            try:
                q.put_nowait({"event": "closed", "reason": "queue_full"})
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
            # Zombie cleanup: if the forwarder that owned this
            # subscription is gone (no other subs remain), drop any
            # pending permissions the dead Claude session never got to
            # answer. They'd otherwise inflate /health's pending count
            # forever. This is the "worker crashed without re-register"
            # path — register_worker already handles the "worker came
            # back cleanly" path.
            remaining = self._outbound_subscribers.get(worker, [])
            if not remaining:
                ws = self._workers.get(worker)
                if ws is not None and ws.pending_permissions:
                    n = len(ws.pending_permissions)
                    ws.pending_permissions.clear()
                    ws.last_activity = time.time()
                    try:
                        self._persist(worker)
                    except Exception as e:
                        logging.getLogger(__name__).warning(
                            "subscribe: failed to persist cleanup for %s: %s",
                            worker, e,
                        )
                    logging.getLogger(__name__).info(
                        "subscribe: cleared %d stale pending permissions for %s "
                        "after subscriber left without re-register",
                        n, worker,
                    )

    async def subscribe_all(self) -> AsyncIterator[dict[str, Any]]:
        """Hub-wide SSE for the web UI. Yields every event across every
        worker with the worker name injected into the message.

        Unlike `subscribe(worker)`, this never evicts — multiple browser
        tabs can share the stream without fighting. Slow subscribers get
        a `closed` sentinel on QueueFull (see `_safe_put`).
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        self._global_subscribers.append(q)
        try:
            while True:
                msg = await q.get()
                if msg.get("event") == "closed":
                    break
                yield msg
        finally:
            if q in self._global_subscribers:
                self._global_subscribers.remove(q)

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

    async def wait_for_matching_event(
        self,
        worker: str,
        since: str | None,
        match: Callable[[WorkerEvent], bool],
        timeout_s: float = 30.0,
    ) -> WorkerEvent | None:
        """Block until an event matching `match` arrives, or timeout.

        Differs from `wait_for_activity` in two important ways:

        1. It loops past intervening events that don't match. Tools like
           `tm_reconnect_mcp` care about a specific outbound reply
           (`reconnect_mcp_result`); without this loop, an unrelated
           inbound (e.g. a parallel `screenshot` request from another
           orchestrator) would wake the wait, the tool would scan and
           find no match, and return a false timeout.

        2. It advances the `since` cursor as it iterates so the next
           wait only blocks on TRULY new events.

        Returns the matching event, or None on timeout. The matching
        event is also written into the worker's event log; the caller
        does not need to combine this result with `events_since`.
        """
        cursor = since
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            new = await self.wait_for_activity(
                worker, cursor, timeout_s=remaining,
            )
            if not new:
                return None
            for e in new:
                if match(e):
                    return e
            # Advance cursor so the next wait_for_activity blocks past
            # the events we just looked at. Without this, the same
            # non-matching events would re-fire instantly each loop.
            cursor = new[-1].event_id
