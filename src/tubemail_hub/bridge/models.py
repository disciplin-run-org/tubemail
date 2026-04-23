"""Pydantic models for the tubemail HTTP API and bridge engine."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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

    def status_state(self) -> str:
        if self.pending_permissions:
            return "waiting_permission"
        # Busy if last event was outbound within a short idle window — simplistic heuristic
        return "idle"
