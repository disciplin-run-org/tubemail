"""Persistent flow store for the tubemail Saved Messages / Flow Shell cap.

A "flow" is a named message template the operator (human UI or
Quartermaster over MCP) can invoke against a target worker. Each
invocation produces a run log keyed by `run_id` — the inbound event
that was delivered plus any outbound events that followed (captured
while the run is in flight).

v1 flows are single-step (`{worker, body}` pairs). The persisted shape
already carries a `run_id` so chained flows can layer on in v1.1 without
a schema break.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# Same shape as the worker-name validator — controls filesystem paths.
_FLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")


def _validate_flow_name(name: str) -> None:
    if not _FLOW_NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError(f"invalid flow name: {name!r}")


class Flow(BaseModel):
    name: str
    body: str
    # Optional default target worker; operator can override per run.
    default_worker: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    last_run_at: float | None = None


class RunLogEntry(BaseModel):
    event_id: str
    ts: float
    kind: str
    content: str
    meta: dict[str, Any] = Field(default_factory=dict)


class RunLog(BaseModel):
    run_id: str
    flow_name: str
    worker: str
    started_at: float
    first_event_id: str | None = None
    finished_at: float | None = None
    events: list[RunLogEntry] = Field(default_factory=list)


class FlowStore:
    """Flat-file persistence under `/data/tubemail/flows/` and
    `/data/tubemail/runs/`. Atomic writes (tmp + rename) so a crash
    mid-save leaves a valid-or-absent file, never a half-written one.
    """

    def __init__(self, data_dir: Path | str):
        self._data_dir = Path(data_dir)
        self._flows_dir = self._data_dir / "flows"
        self._runs_dir = self._data_dir / "runs"
        self._flows_dir.mkdir(parents=True, exist_ok=True)
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    # ── flow CRUD ────────────────────────────────────────────────────────

    def _flow_file(self, name: str) -> Path:
        _validate_flow_name(name)
        return self._flows_dir / f"{name}.json"

    async def save(
        self,
        name: str,
        body: str,
        default_worker: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Flow:
        _validate_flow_name(name)
        async with self._lock:
            path = self._flow_file(name)
            now = time.time()
            if path.exists():
                existing = Flow.model_validate_json(path.read_text())
                existing.body = body
                existing.default_worker = default_worker
                existing.meta = meta or {}
                existing.updated_at = now
                flow = existing
            else:
                flow = Flow(
                    name=name, body=body, default_worker=default_worker,
                    meta=meta or {}, created_at=now, updated_at=now,
                )
            self._atomic_write(path, flow.model_dump_json(indent=2))
            return flow

    async def get(self, name: str) -> Flow | None:
        try:
            path = self._flow_file(name)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            return Flow.model_validate_json(path.read_text())
        except Exception as e:
            logger.warning("FlowStore.get: failed to parse %s: %s", path.name, e)
            return None

    async def delete(self, name: str) -> bool:
        try:
            path = self._flow_file(name)
        except ValueError:
            return False
        if not path.exists():
            return False
        path.unlink()
        return True

    async def list_all(self) -> list[Flow]:
        flows: list[Flow] = []
        for path in sorted(self._flows_dir.glob("*.json")):
            if not _FLOW_NAME_RE.fullmatch(path.stem) or ".." in path.stem:
                logger.warning("FlowStore.list_all: skipping invalid name %s", path.name)
                continue
            try:
                flows.append(Flow.model_validate_json(path.read_text()))
            except Exception as e:
                logger.warning("FlowStore.list_all: failed to parse %s: %s", path.name, e)
                continue
        return flows

    # ── runs ─────────────────────────────────────────────────────────────

    def _run_file(self, run_id: str) -> Path:
        # run_id is server-generated (secrets.token_urlsafe) — still pass it
        # through the same shape check to prevent a later external-facing
        # caller from injecting something crafted.
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", run_id):
            raise ValueError(f"invalid run_id: {run_id!r}")
        return self._runs_dir / f"{run_id}.json"

    async def start_run(
        self, flow_name: str, worker: str, first_event_id: str
    ) -> str:
        run_id = secrets.token_urlsafe(16)
        log = RunLog(
            run_id=run_id, flow_name=flow_name, worker=worker,
            started_at=time.time(), first_event_id=first_event_id,
        )
        async with self._lock:
            self._atomic_write(self._run_file(run_id), log.model_dump_json(indent=2))
            # Update flow's last_run_at
            try:
                flow = await self.get(flow_name)
                if flow is not None:
                    flow.last_run_at = log.started_at
                    self._atomic_write(
                        self._flow_file(flow_name),
                        flow.model_dump_json(indent=2),
                    )
            except Exception as e:
                logger.warning("FlowStore.start_run: could not update last_run_at: %s", e)
        return run_id

    async def get_run(self, run_id: str) -> RunLog | None:
        try:
            path = self._run_file(run_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            return RunLog.model_validate_json(path.read_text())
        except Exception as e:
            logger.warning("FlowStore.get_run: failed to parse %s: %s", path.name, e)
            return None

    async def append_run_event(
        self, run_id: str, entry: RunLogEntry
    ) -> bool:
        """Append one event to an in-flight run log. Returns False if the
        run doesn't exist."""
        async with self._lock:
            log = await self.get_run(run_id)
            if log is None:
                return False
            log.events.append(entry)
            self._atomic_write(self._run_file(run_id), log.model_dump_json(indent=2))
            return True

    async def finish_run(self, run_id: str) -> None:
        async with self._lock:
            log = await self.get_run(run_id)
            if log is None:
                return
            log.finished_at = time.time()
            self._atomic_write(self._run_file(run_id), log.model_dump_json(indent=2))

    # ── internals ────────────────────────────────────────────────────────

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        import os
        os.replace(tmp, path)
