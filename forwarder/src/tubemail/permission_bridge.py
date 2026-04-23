"""Permission bridge: connects Claude Code's PreToolUse hook to the hub.

Architecture:

    Claude Code fires PreToolUse hook (stdin JSON)
        │
        ▼
    auto-approve-safe.sh sees TM_WORKER_NAME + socket → proxies to socket
        │
        ▼
    HookServer (this module) on /tmp/tubemail-hook-<worker>.sock
        1. Looks up matching pending in PendingBuffer (by tool_name + input hash)
        2. Runs risk policy (same Haiku LOW/MEDIUM/HIGH as auto-approve-safe.sh)
        3. On allow: POSTs /permission-response to the hub with the real request_id
           → hub's pending_permissions clears
        4. Returns hook-format JSON to the shell script, which relays to Claude

    Meanwhile, Claude Code also emits notifications/claude/channel/permission_request
    via the MCP channel. channel.py records each request in the same PendingBuffer
    so the HookServer can correlate.

Why a unix socket and not HTTP-to-hub directly? Because the hook process (bash)
doesn't know the request_id — only the forwarder does (from the channel notification).
The socket lets the hook ask the forwarder "what's the pending request matching
this tool call?" The forwarder has that context; the shell script doesn't.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .hub_client import HubClient

logger = logging.getLogger(__name__)

# ── config ───────────────────────────────────────────────────────────────────
PENDING_TTL_S = 30.0  # drop un-matched buffer entries after this long
MATCH_WAIT_S = 0.75   # max time the hook server waits for a matching notification
HAIKU_TIMEOUT_S = 6.0
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Read-only tools always allowed (mirrors auto-approve-safe.sh)
_READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "Search", "WebSearch", "WebFetch"}

# Fallback safe-readonly shell pattern (mirrors auto-approve-safe.sh). Only
# used when the Haiku API is unavailable or returns a non-LOW/MEDIUM/HIGH result.
_SAFE_READONLY_RE = re.compile(
    r"^\s*(ls|cat|head|tail|wc|file|stat|which|type|echo|printf|date|pwd|whoami|"
    r"uname|id|env|printenv|git (status|log|diff|show|branch|tag|remote|rev-parse|describe))\b"
)
_DANGER_CHAIN_RE = re.compile(r"[;&|`$]\(")
_PIPE_RE = re.compile(r"\|")


# ── pending buffer ───────────────────────────────────────────────────────────

class ApprovalExchange:
    """Two-sided matcher for hook approvals and channel permission_requests.

    Either side can arrive first:
        hook-first:  Claude fires the PreToolUse hook, blocks on its result.
                     The channel notification is emitted only AFTER the hook
                     returns. So when the hook's policy decides allow, there's
                     no matching request_id yet — we bank the approval.
                     When the notification arrives later, it consumes the
                     banked approval and posts permission_response to the hub.
        request-first: Channel notification arrives before hook (rare but
                     possible if the hook is delayed by Haiku latency). The
                     request is banked; the hook's allow decision consumes it
                     and posts permission_response immediately.

    Both sides try to pair against the other; whoever arrives second triggers
    the hub resolve. Keyed by tool_name with FIFO pairing — Claude processes
    tool calls near-serially per worker, so multi-pending per tool is rare.

    Entries that don't pair within `ttl_s` are reaped. A banked approval that
    never sees a matching request is benign (the tool was auto-approved locally
    and never appeared in the channel). A banked request that never sees an
    approval stays forwarded to the hub for orchestrator decision.
    """

    def __init__(self, ttl_s: float = PENDING_TTL_S):
        self._ttl = ttl_s
        # Two per-tool FIFO queues — whichever has entries when the other
        # side fires gets paired.
        self._requests: dict[str, list[tuple[str, float]]] = {}  # tool → [(request_id, ts)]
        self._approvals: dict[str, list[float]] = {}             # tool → [ts]
        self._lock = asyncio.Lock()

    async def offer_request(
        self, request_id: str, tool_name: str
    ) -> bool:
        """Channel side: a permission_request notification arrived.

        Returns True if a banked approval matched (caller should POST resolve
        to the hub). False if no approval is waiting (caller should forward
        the request as pending to the hub as usual).
        """
        if not request_id or not tool_name:
            return False
        now = time.monotonic()
        async with self._lock:
            self._reap_locked(now)
            approvals = self._approvals.get(tool_name)
            if approvals:
                approvals.pop(0)
                if not approvals:
                    self._approvals.pop(tool_name, None)
                return True
            self._requests.setdefault(tool_name, []).append((request_id, now))
            return False

    async def offer_approval(self, tool_name: str) -> str | None:
        """Hook side: the risk policy decided allow for a tool call.

        Returns a request_id if a banked request matched (caller should POST
        resolve to the hub). None if no request is waiting (caller banks the
        approval and returns allow to the hook; the later request will pair).
        """
        if not tool_name:
            return None
        now = time.monotonic()
        async with self._lock:
            self._reap_locked(now)
            requests = self._requests.get(tool_name)
            if requests:
                request_id, _ts = requests.pop(0)
                if not requests:
                    self._requests.pop(tool_name, None)
                return request_id
            self._approvals.setdefault(tool_name, []).append(now)
            return None

    def _reap_locked(self, now: float) -> None:
        cutoff = now - self._ttl
        for key in list(self._requests.keys()):
            kept = [(rid, ts) for (rid, ts) in self._requests[key] if ts >= cutoff]
            if kept:
                self._requests[key] = kept
            else:
                self._requests.pop(key, None)
        for key in list(self._approvals.keys()):
            kept = [ts for ts in self._approvals[key] if ts >= cutoff]
            if kept:
                self._approvals[key] = kept
            else:
                self._approvals.pop(key, None)


# Backwards-compat alias during the transition. Remove once all call sites
# switch over.
PendingBuffer = ApprovalExchange


# ── risk policy (port of auto-approve-safe.sh) ───────────────────────────────

class RiskPolicy:
    """Same three-tier logic as auto-approve-safe.sh.

    1. Read-only tools → allow
    2. Non-Bash → defer (let the normal prompt flow handle it)
    3. Bash: call Haiku for LOW/MEDIUM/HIGH
       - LOW → allow
       - MEDIUM/HIGH → defer
       - API failure / unexpected → fail-closed, allow only safe-readonly patterns
    """

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    @staticmethod
    def _load_api_key() -> str:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if key:
            return key
        key_file = Path.home() / ".claude" / "anthropic_api_key"
        if key_file.is_file():
            try:
                mode = key_file.stat().st_mode & 0o777
                if mode not in (0o600, 0o400):
                    try:
                        key_file.chmod(0o600)
                    except OSError:
                        logger.debug("failed to chmod 600 on %s", key_file)
                return key_file.read_text().strip()
            except OSError:
                return ""
        return ""

    async def decide(self, tool_name: str, tool_input: Any) -> str:
        """Return one of: 'allow', 'defer'. ('deny' reserved for future use.)"""
        if tool_name in _READ_ONLY_TOOLS:
            return "allow"
        if tool_name != "Bash":
            return "defer"

        command = ""
        if isinstance(tool_input, dict):
            command = str(tool_input.get("command", "")).strip()
        if not command:
            return "defer"

        api_key = self._load_api_key()
        if not api_key:
            return self._fallback_heuristic(command)

        try:
            risk = await self._haiku_rate(command, api_key)
        except Exception as e:
            logger.warning("haiku risk call failed: %s — falling back to heuristic", e)
            return self._fallback_heuristic(command)

        if risk == "LOW":
            return "allow"
        if risk in ("MEDIUM", "HIGH"):
            return "defer"
        return self._fallback_heuristic(command)

    async def _haiku_rate(self, command: str, api_key: str) -> str:
        resp = await self._client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": HAIKU_MODEL,
                "max_tokens": 10,
                "system": (
                    "You are a shell command safety rater. "
                    "Respond with exactly one word: LOW, MEDIUM, or HIGH."
                ),
                "messages": [{
                    "role": "user",
                    "content": (
                        "Rate the risk of this shell command being run on a "
                        "developer local machine:\n\n" + command
                    ),
                }],
            },
            timeout=HAIKU_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                break
        match = re.search(r"\b(LOW|MEDIUM|HIGH)\b", text.upper())
        return match.group(1) if match else ""

    @staticmethod
    def _fallback_heuristic(command: str) -> str:
        # Fail-closed: reject chained/piped commands, allow only clearly safe readonly.
        if _DANGER_CHAIN_RE.search(command):
            return "defer"
        if _PIPE_RE.search(command):
            return "defer"
        if _SAFE_READONLY_RE.match(command):
            return "allow"
        return "defer"


# ── socket server ────────────────────────────────────────────────────────────

class HookServer:
    """Unix socket server that the PreToolUse hook talks to.

    Protocol (intentionally simple — no HTTP framing):
        client connects, writes the full hook stdin JSON, half-closes write
        server reads until EOF, decides, writes response JSON, closes

    Response JSON is the exact shape auto-approve-safe.sh would emit:
        allow:  {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                        "decision": {"behavior": "allow"}}}
        defer:  {}                   — empty object; hook relays, Claude prompts user
        deny:   {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                        "decision": {"behavior": "deny",
                                                     "reason": "..."}}}
    """

    def __init__(
        self,
        *,
        worker: str,
        exchange: ApprovalExchange,
        policy: RiskPolicy,
        hub: HubClient,
    ):
        self._worker = worker
        self._exchange = exchange
        self._policy = policy
        self._hub = hub
        self._server: asyncio.base_events.Server | None = None
        self._socket_path = Path(f"/tmp/tubemail-hook-{worker}.sock")

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    async def start(self) -> None:
        # Clean up a stale socket from a prior crashed forwarder. Safe because
        # the path is per-worker and we're the only writer for that worker.
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except OSError:
                logger.warning("could not unlink stale socket %s", self._socket_path)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        try:
            os.chmod(self._socket_path, 0o600)
        except OSError:
            logger.debug("chmod 600 on socket failed — continuing")
        logger.info("hook socket listening at %s", self._socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except OSError:
                pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.read(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("hook client read timed out")
            writer.close()
            return

        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            logger.warning("hook client sent invalid JSON")
            writer.write(b"{}")
            await writer.drain()
            writer.close()
            return

        response = await self._decide(payload)
        try:
            writer.write(json.dumps(response).encode("utf-8"))
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            logger.debug("hook client disconnected before response")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        tool_name = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input", {}) or {}

        decision = await self._policy.decide(tool_name, tool_input)

        if decision == "allow":
            # Try to pair with an already-banked channel request. If matched,
            # post resolve to the hub now. If not, bank the approval so the
            # channel handler can resolve when the notification arrives.
            request_id = await self._exchange.offer_approval(tool_name)
            if request_id:
                try:
                    await self._hub.post_permission_response(
                        request_id=request_id, behavior="allow"
                    )
                except Exception:
                    logger.exception(
                        "posting permission_response to hub failed "
                        "(hook still approves locally)"
                    )
            else:
                logger.debug(
                    "hook allowed %s; banked approval — will resolve when "
                    "the matching channel notification arrives", tool_name
                )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            }

        if decision == "deny":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "deny",
                        "reason": "blocked by tubemail risk policy",
                    },
                }
            }

        # 'defer' — empty object lets the normal permission dialog appear.
        return {}


__all__ = ["PendingBuffer", "RiskPolicy", "HookServer"]
