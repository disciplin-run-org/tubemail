"""Pydantic models for the tubemail HTTP API and bridge engine."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

# How long a trailing `inbound` event marks a worker as "busy" before
# decaying back to "idle". The hub can't tell whether a worker actively
# processed a message or quietly ignored it — many work-order replies go
# back as code edits or git commits rather than channel outbound events.
# Without decay, any worker whose author forgot to call the reply tool
# stays "busy" forever in the roster.
#
# 10 minutes is empirically long enough to cover normal work bursts but
# short enough that an idle worker doesn't sit in the wrong state across
# a coffee break. Workers that DO emit progress outbound events (heartbeat,
# partial reply, permission request) reset the trailing-inbound clock so
# they remain "busy" through the active window.
BUSY_DECAY_S = 600.0


class RegisterRequest(BaseModel):
    cwd: str
    pid: int | None = None
    forwarder_version: str | None = None


class RegisterResponse(BaseModel):
    worker: str
    cursor: str


class InboundEvent(BaseModel):
    """A message from the orchestrator destined for a worker."""

    content: str
    meta: dict[str, Any] = Field(default_factory=dict)


class OutboundEvent(BaseModel):
    """A worker's reply (from its reply tool) going back to the orchestrator."""

    text: str
    meta: dict[str, Any] = Field(default_factory=dict)


class PermissionRequestPayload(BaseModel):
    """Worker's Claude Code is asking for tool approval."""

    request_id: str
    tool_name: str
    description: str = ""
    input_preview: str = ""


class PermissionResponsePayload(BaseModel):
    request_id: str
    behavior: Literal["allow", "deny"]


class WorkerEvent(BaseModel):
    """A single event on a worker's timeline — persisted and queryable."""

    event_id: str
    ts: float
    kind: Literal["inbound", "outbound", "permission_request", "permission_response", "interrupt"]
    content: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class WorkerState(BaseModel):
    """Per-worker persistent state."""

    name: str
    cwd: str = ""
    registered_at: float = 0.0
    last_activity: float = 0.0
    forwarder_version: str = ""
    events: list[WorkerEvent] = Field(default_factory=list)
    pending_permissions: list[PermissionRequestPayload] = Field(default_factory=list)
    # Set to True when the forwarder posts /goodbye before shutting down.
    # Reset to False on the next register (new session starting). Lets the
    # hub distinguish "user /exit'd cleanly" from "crashed / hung / killed".
    exited_cleanly: bool = False
    # Per-worker recording toggle. New workers inherit the global default
    # from HubConfig.recording_enabled_by_default at first register; flipping
    # this on/off via tm_recording_toggle / the web UI persists across
    # restarts. The actual file-writing is owned by RecordingManager — this
    # field is just the source of truth for "should the bridge be teeing
    # this worker's pty bytes to disk right now."
    recording_enabled: bool = False
    # Last-known context window percentage parsed from the worker's TUI
    # status bar (`context X%`). Set by the manager pushing updates to
    # POST /tubemail/<worker>/context-pct whenever the value changes.
    # None until the first parse — workers running older managers stay
    # at None forever, which the UI renders as "—".
    context_pct: int | None = None

    def status_state(self) -> str:
        if self.pending_permissions:
            return "waiting_permission"
        # Busy = orchestrator handed work to the worker and no reply has come
        # back yet. The only reliable signal the hub has is the timeline:
        # a trailing `inbound` means work is in flight; a trailing `outbound`
        # (or any non-inbound) means the worker has reported done.
        #
        # If the trailing inbound is older than BUSY_DECAY_S, treat as idle.
        # Many work orders complete with a code edit or commit rather than a
        # channel outbound, so without this decay the worker stays "busy"
        # forever — e.g. jjstack-tm sat in busy for 2+ days after handling
        # two work orders silently (2026-04-25 investigation).
        if self.events and self.events[-1].kind == "inbound":
            if time.time() - self.events[-1].ts > BUSY_DECAY_S:
                return "idle"
            return "busy"
        return "idle"
