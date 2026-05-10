"""Claude Code channel plugin logic.

Implements the `experimental.claude/channel` and
`experimental.claude/channel/permission` capabilities. Relays events in
both directions between the worker's Claude Code session (stdio) and the
TubeMail hub (HTTP/SSE).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from .hub_client import HubClient
from .jsonrpc import JsonRpcError, JsonRpcStdio
from .local_ipc import SOCK_ENV, LocalIPCError
from .local_ipc import request as local_ipc_request
from .permission_durability import (
    PermissionResponseSpool,
    drain_spool,
    post_permission_response_durable,
)

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "tubemail"
SERVER_VERSION = "0.1.0"


class Channel:
    """Wires the stdio JSON-RPC server to the hub client.

    Lifecycle:
        1. Claude Code spawns this process over stdio.
        2. Claude Code sends `initialize` — we reply with the experimental
           capabilities declaring us a channel plugin.
        3. We start the SSE subscription task to the hub in parallel.
        4. We pump events both ways until stdin closes.
    """

    def __init__(
        self,
        hub: HubClient,
        worker_name: str,
        cwd: str,
        *,
        rpc: JsonRpcStdio | None = None,
        pending_buffer: Any = None,
        permission_spool: PermissionResponseSpool | None = None,
    ):
        self._hub = hub
        self._worker = worker_name
        self._cwd = cwd
        self._rpc = rpc or JsonRpcStdio()
        self._initialized = asyncio.Event()
        self._stream_task: asyncio.Task | None = None
        self._pending_buffer = pending_buffer  # PendingBuffer | None
        # Per-worker permission_response spool. Without durability here, a
        # hub blip at the moment a permission gets resolved locally leaks
        # state — the LLM proceeds, but the hub stays stuck on
        # waiting_permission until the worker re-registers. The spool
        # writes failed POSTs to disk and drains on the next success or
        # at channel startup.
        self._permission_spool = permission_spool or PermissionResponseSpool(
            worker_name,
        )
        # Wire the hub's threshold-tripped health notifier through to the
        # LLM as a `notifications/claude/channel` event. The hub fires this
        # exactly once per outage (see UNHEALTHY_NOTIFY_THRESHOLD); the
        # callback is set after construction to avoid a chicken-and-egg
        # constructor dependency for callers that pre-build a HubClient.
        self._hub._health_notifier = self._send_channel_health_notification
        self._install_handlers()

    # ── handler installation ────────────────────────────────────────────────

    def _install_handlers(self) -> None:
        self._rpc.on_request("initialize", self._handle_initialize)
        self._rpc.on_notification("notifications/initialized", self._handle_initialized)
        self._rpc.on_request("tools/list", self._handle_tools_list)
        self._rpc.on_request("tools/call", self._handle_tools_call)
        self._rpc.on_request("ping", self._handle_ping)
        self._rpc.on_unknown_notification(self._handle_unknown_notification)

    # ── MCP protocol handlers ────────────────────────────────────────────────

    async def _handle_initialize(self, params: Any) -> dict[str, Any]:
        logger.info("initialize from Claude Code: %s", params)
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "experimental": {
                    "claude/channel": {},
                    "claude/channel/permission": {},
                },
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        }

    async def _handle_initialized(self, _params: Any) -> None:
        """Client acks initialize. Start hub work now.

        If the initial register fails, we surface a `channel_health`
        notification to the LLM so it can include the warning in
        qm-reports rather than discovering the drift after the fact. The
        SSE pump still starts — the loop's per-connect re-register can
        recover when the hub comes back, and we don't want a transient
        boot-time blip to leave the worker permanently disconnected.
        """
        logger.info("initialized ack received — starting hub stream")
        try:
            await self._hub.register(cwd=self._cwd)
        except Exception as err:
            logger.warning("hub register failed at init: %s", err)
            await self._send_channel_health_notification(
                {
                    "content": (
                        "tubemail-channel: hub registration failed at init — "
                        "replies may not reach orchestrator. "
                        "Call channel_health() to confirm before relying on a reply."
                    ),
                    "meta": {
                        "source": "tubemail-channel",
                        "kind": "channel_health",
                        "level": "error",
                        "phase": "init",
                        "error": str(err),
                    },
                }
            )
        # end try
        # Drain any permission_response payloads that were spooled by a
        # previous channel process when the hub was unreachable. Best-
        # effort — if the hub is still down, drain_spool stops on the
        # first failure and the entries stay on disk for the next try.
        try:
            await drain_spool(self._hub, self._permission_spool)
        except Exception:
            logger.exception("permission spool drain at init failed")
        self._initialized.set()
        self._stream_task = asyncio.create_task(self._pump_hub_events())

    async def _send_channel_health_notification(self, payload: dict[str, Any]) -> None:
        """Push a single `notifications/claude/channel` event with a
        `channel_health` meta tag. Used by both init-time register failure and
        the SSE-loop threshold trip in HubClient.

        Errors are logged and dropped — the notification is purely
        advisory; if it fails we don't want to crash the channel.
        """
        try:
            await self._rpc.send_notification("notifications/claude/channel", payload)
        except Exception:
            logger.exception("channel_health notification send failed")
        # end try

    async def _handle_ping(self, _params: Any) -> dict[str, Any]:
        return {}

    async def _handle_tools_list(self, _params: Any) -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": "reply",
                    "description": (
                        "Send a reply message back up to the orchestrator "
                        "(TubeMail). Use this to report progress, ask "
                        "clarifying questions, or deliver results after "
                        "acting on an inbound channel event."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "The message body to deliver to the orchestrator.",
                            },
                            "meta": {
                                "type": "object",
                                "description": "Optional metadata tags (e.g. progress %, status).",
                                "additionalProperties": True,
                            },
                        },
                        "required": ["text"],
                    },
                },
                {
                    "name": "ack",
                    "description": (
                        "Acknowledge an interrupt or inbound event without a "
                        "text reply. Use this for quick confirmations."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                    },
                },
                {
                    "name": "channel_health",
                    "description": (
                        "Return current connection state to the tubemail "
                        "hub. Use this to verify replies will land before "
                        "relying on them — e.g. before emitting a critical "
                        "qm-report fence on a possibly-flapping link."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                    },
                },
                {
                    "name": "reconnect_mcp",
                    "description": (
                        "Reconnect a failed MCP server on this worker by "
                        "asking the local manager to drive the /mcp dialog. "
                        "Talks to the manager over a Unix-domain socket so "
                        "this works even when the tubemail hub itself is "
                        "the failed MCP — the only path that does. For "
                        "non-tubemail servers, prefer "
                        "`mcp__tubemail__tm_self_reconnect_mcp(server)`, "
                        "which uses the hub round-trip and gives the "
                        "orchestrator a complete event timeline. Returns "
                        "{ok, server, detail}."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "server": {
                                "type": "string",
                                "description": (
                                    "MCP server name as it appears in the "
                                    "/mcp dialog (e.g. 'tubemail', "
                                    "'leanspecs', 'iris-qa')."
                                ),
                            },
                        },
                        "required": ["server"],
                    },
                },
            ]
        }

    async def _handle_tools_call(self, params: Any) -> dict[str, Any]:
        name = params.get("name") if isinstance(params, dict) else None
        args = params.get("arguments", {}) if isinstance(params, dict) else {}

        if name == "reply":
            text = args.get("text", "")
            meta = args.get("meta", {}) or {}
            try:
                await self._hub.post_outbound(text, meta)
            except Exception as e:
                logger.exception("reply forwarding failed")
                raise JsonRpcError(-32603, f"hub post_outbound failed: {e}")
            return {"content": [{"type": "text", "text": "delivered to tubemail"}]}

        if name == "ack":
            # ack used to swallow forwarding errors with "ignored" — that
            # gave the LLM a false confirmation when the hub never saw the
            # ack. Mirror the `reply` path: any forwarding failure becomes
            # a JsonRpcError so the caller learns the truth and can either
            # retry or include the failure in its qm-report.
            try:
                await self._hub.post_outbound("", {"kind": "ack"})
            except Exception as e:
                logger.exception("ack forwarding failed")
                raise JsonRpcError(-32603, f"hub post_outbound failed: {e}")
            # end try
            return {"content": [{"type": "text", "text": "acked"}]}

        if name == "channel_health":
            health = self._hub.health()
            return {
                "content": [{"type": "text", "text": json.dumps(health)}],
                # Also return as structuredContent so newer Claude Code
                # versions that prefer typed responses can consume it
                # directly without re-parsing the text payload.
                "structuredContent": health,
            }

        if name == "reconnect_mcp":
            server = args.get("server", "")
            if not isinstance(server, str) or not server:
                raise JsonRpcError(
                    -32602,
                    "reconnect_mcp requires non-empty 'server' arg",
                )
            result = await self._do_reconnect_mcp(server)
            return {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "structuredContent": result,
            }

        raise JsonRpcError(-32601, f"unknown tool: {name}")

    async def _do_reconnect_mcp(self, server: str) -> dict[str, Any]:
        """Drive a reconnect via the local manager socket, then return its
        verdict. This is the only path that works when the tubemail hub itself
        is the failed MCP — the channel cannot use any tm_* tool (those require
        the hub) but the local socket is hub-independent.

        The socket path is exported by the manager via
        TUBEMAIL_LOCAL_SOCK and inherited by this process through the
        pty child. If the env var is unset, the manager is older than
        this feature or its bind failed — the LLM is told to use the
        hub-routed tm_self_reconnect_mcp instead, which works for every
        server EXCEPT tubemail itself.
        """
        sock_path = os.environ.get(SOCK_ENV, "").strip()
        if not sock_path:
            return {
                "ok": False,
                "server": server,
                "detail": (
                    f"{SOCK_ENV} not set — local IPC unavailable. Use "
                    "mcp__tubemail__tm_self_reconnect_mcp(server) instead "
                    "for any non-tubemail server."
                ),
            }
        request_id = uuid.uuid4().hex
        try:
            response = await local_ipc_request(
                sock_path,
                {
                    "action": "reconnect_mcp",
                    "server": server,
                    "request_id": request_id,
                },
            )
        except LocalIPCError as e:
            logger.warning("local-ipc reconnect_mcp failed: %s", e)
            return {
                "ok": False,
                "server": server,
                "detail": f"local IPC failed: {e}",
            }
        # Drop the echoed request_id from the result — the LLM doesn't
        # need it and it makes the structured response noisier.
        response.pop("request_id", None)
        return response

    async def _handle_unknown_notification(self, method: str, params: Any) -> None:
        """Catches permission_request, permission (resolution), and other
        channel notifications."""
        logger.debug("unknown notification %s: %s", method, params)

        if method == "notifications/claude/channel/permission_request":
            request_id = params.get("request_id", "")
            tool_name = params.get("tool_name", "")
            # Always post the request to the hub so the event timeline is
            # complete (permission_request shows up in tm_receive). Then
            # check the local ApprovalExchange — if the hook already banked
            # an approval for this tool, post the resolve right away so the
            # hub's pending_permissions list clears without waiting for the
            # orchestrator to intervene.
            try:
                await self._hub.post_permission_request(
                    request_id=request_id,
                    tool_name=tool_name,
                    description=params.get("description", ""),
                    input_preview=params.get("input_preview", ""),
                )
            except Exception:
                logger.exception("forwarding permission_request failed")

            if self._pending_buffer is not None and request_id and tool_name:
                try:
                    matched = await self._pending_buffer.offer_request(
                        request_id=request_id, tool_name=tool_name
                    )
                except Exception:
                    logger.exception("approval exchange offer_request failed")
                    matched = False
                if matched:
                    # Use the durable wrapper so a hub blip during
                    # auto-approve doesn't leak the same way as a
                    # user-driven resolution would. Spools on final
                    # failure; the next channel startup or successful
                    # POST drains it.
                    await post_permission_response_durable(
                        self._hub,
                        self._permission_spool,
                        request_id,
                        "allow",
                    )

        elif method == "notifications/claude/channel/permission":
            # Local resolution — user approved/denied at the terminal or
            # via hook. Forward to hub via the durable wrapper so a
            # transient hub blip at the moment of resolution doesn't
            # leave the hub stuck on `waiting_permission` forever.
            await post_permission_response_durable(
                self._hub,
                self._permission_spool,
                params.get("request_id", ""),
                params.get("behavior", "allow"),
            )

        else:
            logger.debug("unhandled notification: %s", method)

    # ── hub → worker direction ───────────────────────────────────────────────

    async def _pump_hub_events(self) -> None:
        """Subscribe to the hub SSE stream, translate events into
        notifications.

        Runs in an infinite loop — if the stream crashes (hub restart,
        network blip), it logs and re-enters the stream.  The
        hub_client.stream() generator handles reconnect + re-
        registration internally; this outer loop catches any escaping
        exceptions so the task stays alive.
        """
        while True:
            try:
                async for evt in self._hub.stream():
                    event_type = evt.get("event", "")
                    data = evt.get("data", {})
                    if event_type == "channel_event":
                        await self._rpc.send_notification(
                            "notifications/claude/channel",
                            {
                                "content": data.get("content", ""),
                                "meta": {
                                    **(data.get("meta") or {}),
                                    "source": "tubemail",
                                },
                            },
                        )
                    elif event_type == "permission_response":
                        await self._rpc.send_notification(
                            "notifications/claude/channel/permission",
                            {
                                "request_id": data.get("request_id", ""),
                                "behavior": data.get("behavior", "deny"),
                            },
                        )
                    elif event_type == "interrupt":
                        await self._rpc.send_notification(
                            "notifications/claude/channel",
                            {
                                "content": "interrupt",
                                "meta": {"source": "tubemail", "kind": "interrupt"},
                            },
                        )
                    else:
                        logger.debug("unhandled hub event: %s", event_type)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pump_hub_events crashed — restarting stream")
                await asyncio.sleep(2.0)

    # ── entry ────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        try:
            await self._rpc.run()
        finally:
            if self._stream_task is not None:
                self._stream_task.cancel()
                try:
                    await self._stream_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._hub.unregister()
            await self._hub.aclose()
