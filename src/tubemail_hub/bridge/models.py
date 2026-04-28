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
# 10 minutes is the absolute cap, used when we have no other signal that
# the worker has been alive since the inbound arrived. Workers running
# the post-2026-04 manager push context-pct heartbeats whenever Claude
# burns tokens — those bump last_activity above the inbound timestamp
# and let us decay much faster (BUSY_QUIET_S) once the heartbeats stop.
BUSY_DECAY_S = 600.0

# Once we've seen ANY post-inbound activity (context-pct push, register,
# recording POST, etc.), how long to wait without further activity before
# treating the worker as idle. 60 seconds is short enough that a worker
# that finished a work order silently flips to idle within a normal
# attention span, long enough to cover a tool call or short pause in
# token generation. Bug fixed 2026-04-26 — quartermaster-tm sat at
# `busy` after each silently-completed work order even though the
# manager was reporting "no more context-pct changes" continuously.
BUSY_QUIET_S = 60.0

# How long an `observed_active` push from the manager remains
# authoritative. The manager re-checks every 3s and re-POSTs only on
# change, so under normal operation observations stay valid forever
# (no churn). This freshness window is the disconnect-detection knob:
# if the manager dies / the network blips / the channel plugin loses
# its loop, the observation goes stale and `status_state()` falls
# back to event-timeline decay. 30s = ~10 missed status loops, which
# is "the manager is genuinely gone" not "it's slow."
OBSERVED_ACTIVE_FRESHNESS_S = 30.0


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
    # Authoritative busy/idle signal from the manager. The manager parses
    # claude's TUI for spinner / running-timer markers and pushes the
    # boolean on every change to POST /tubemail/<worker>/active. None
    # means "manager has never reported" (legacy manager, or worker just
    # registered) — `status_state()` falls back to event-timeline decay
    # in that case. Field added 2026-04-26 alongside BUSY_QUIET_S.
    observed_active: bool | None = None
    observed_active_at: float = 0.0

    def status_state(self) -> str:
        if self.pending_permissions:
            return "waiting_permission"
        now = time.time()

        # Authoritative signal: the manager parses claude's TUI screen
        # and pushes a boolean here whenever it changes. When fresh,
        # this beats every event-timeline heuristic — the manager has
        # direct line of sight to the pty, the hub does not.
        #
        # Only honored if we've seen a recent push. If the manager has
        # been silent longer than OBSERVED_ACTIVE_FRESHNESS_S (likely
        # crashed / disconnected / running a build of the manager that
        # predates this signal), fall through to the timeline-based
        # decay so the roster still gives the user a useful state.
        if (
            self.observed_active is not None
            and now - self.observed_active_at < OBSERVED_ACTIVE_FRESHNESS_S
        ):
            return "busy" if self.observed_active else "idle"

        # Fallback: event-timeline decay. Used by legacy managers and
        # during manager-disconnect windows.
        #
        # Busy = orchestrator handed work to the worker and no reply has come
        # back yet. The only reliable signal the hub has is the timeline:
        # a trailing `inbound` means work is in flight; a trailing `outbound`
        # (or any non-inbound) means the worker has reported done.
        #
        # The decay logic uses two windows:
        #
        # 1. BUSY_DECAY_S (10 min) — absolute cap on a stale inbound, used
        #    even when the worker has produced zero post-inbound signal.
        #    Catches workers running the legacy manager (no context-pct).
        # 2. BUSY_QUIET_S (60 s) — applied only when the worker HAS shown
        #    post-inbound activity (last_activity has advanced past the
        #    inbound's ts). The post-2026-04 manager pushes context-pct
        #    every time Claude burns tokens, so a worker actively
        #    generating bumps last_activity continuously. When those
        #    heartbeats stop for BUSY_QUIET_S, the worker has finished
        #    silently and should flip to idle even if the inbound itself
        #    is still inside the 10-min window.
        if self.events and self.events[-1].kind == "inbound":
            inbound_ts = self.events[-1].ts
            if now - inbound_ts > BUSY_DECAY_S:
                return "idle"
            if (
                self.last_activity > inbound_ts
                and now - self.last_activity > BUSY_QUIET_S
            ):
                return "idle"
            return "busy"
        return "idle"
