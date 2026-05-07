"""HTTP client for the TubeMail tubemail bridge.

Forwarders use this to POST events up to the hub and subscribe to the
SSE stream for server-pushed events. Bearer-auth'd with TUBEMAIL_SECRET.

Health-tracking surface (added 2026-05-07 after QM #205/#206 dogfooded
silent registration drift): every register/post/stream operation updates
`registered`, `connected`, `register_failures_since_boot`, and
`last_outbound_success_at`. The Channel exposes these via the
`channel_health` MCP tool so the LLM can confirm replies will land
before relying on them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
from httpx_sse import aconnect_sse

logger = logging.getLogger(__name__)

# Number of consecutive SSE-loop register failures before the client fires
# its `health_notifier` callback. Once tripped, the client won't fire again
# until a successful register resets the streak — so the LLM gets one
# clear "your hub link is unhealthy" signal per outage, not one per attempt.
UNHEALTHY_NOTIFY_THRESHOLD = 5


class HubClient:
    def __init__(
        self,
        base_url: str,
        worker: str,
        secret: str,
        *,
        cwd: str = "",
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
        health_notifier: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._worker = worker
        self._cwd = cwd
        self._headers = {"Authorization": f"Bearer {secret}"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, read=None),
            headers=self._headers,
        )
        # Health-tracking state. Set by the various ops below; consumed by
        # `health()` and the `channel_health` MCP tool. None of these are
        # protected by a lock — single-threaded asyncio per HubClient.
        self._registered: bool = False
        self._connected: bool = False
        self._register_failures_since_boot: int = 0
        self._consecutive_register_failures: int = 0
        self._last_outbound_success_at: float | None = None
        # Async callback the Channel uses to push a one-time notification
        # to the LLM once the SSE-loop register failures cross the
        # UNHEALTHY_NOTIFY_THRESHOLD. Latched so we only fire once per
        # outage; reset by the next successful register.
        self._health_notifier = health_notifier
        self._unhealthy_notified = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def hub_url(self) -> str:
        return self._base

    def health(self) -> dict[str, Any]:
        """Snapshot of the client's view of the hub link.

        - `connected`: is the SSE stream currently open?
        - `registered`: did the most recent register call succeed?
        - `register_failures_since_boot`: monotonic counter — useful for
          spotting flapping links across a session.
        - `last_outbound_success_at`: epoch seconds of the last successful
          POST /outbound, or None if never successful.
        - `hub_url`: base URL the client is talking to (for the LLM to
          quote in qm-reports without leaking secrets).
        """
        return {
            "connected": self._connected,
            "registered": self._registered,
            "register_failures_since_boot": self._register_failures_since_boot,
            "last_outbound_success_at": self._last_outbound_success_at,
            "hub_url": self._base,
        }

    def _url(self, path: str) -> str:
        return f"{self._base}/tubemail/{self._worker}{path}"

    async def register(self, cwd: str, pid: int | None = None) -> dict[str, Any]:
        # Include forwarder_version so the roster can display "what code
        # is this worker actually running?" — without this the channel-
        # side worker shows up with version='' while only the manager-
        # side version surfaces, which is misleading.
        from tubemail import __version__ as _version
        try:
            resp = await self._client.post(
                self._url("/register"),
                json={"cwd": cwd, "pid": pid, "forwarder_version": _version},
            )
            resp.raise_for_status()
        except Exception:
            self._registered = False
            self._register_failures_since_boot += 1
            self._consecutive_register_failures += 1
            raise
        #end try
        self._registered = True
        self._consecutive_register_failures = 0
        # New healthy register clears the latch so a subsequent outage can
        # fire its own notification.
        self._unhealthy_notified = False
        return resp.json()

    async def unregister(self) -> None:
        try:
            await self._client.post(self._url("/unregister"))
        except httpx.HTTPError:
            pass  # best-effort on shutdown

    async def post_outbound(self, text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._client.post(
            self._url("/outbound"),
            json={"text": text, "meta": meta or {}},
        )
        resp.raise_for_status()
        # Stamp success only when the hub returned 2xx — partial failures
        # (network ok, hub rejected) shouldn't make the health card lie.
        self._last_outbound_success_at = time.time()
        return resp.json()

    async def post_permission_request(
        self,
        request_id: str,
        tool_name: str,
        description: str = "",
        input_preview: str = "",
    ) -> dict[str, Any]:
        resp = await self._client.post(
            self._url("/permission-request"),
            json={
                "request_id": request_id,
                "tool_name": tool_name,
                "description": description,
                "input_preview": input_preview,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def post_permission_response(
        self,
        request_id: str,
        behavior: str,
    ) -> dict[str, Any]:
        """Forward a locally-resolved permission back to the hub.

        Called when the user (or auto-approve hook) resolves a permission
        at the terminal. This lets TubeMail learn approval patterns.
        """
        resp = await self._client.post(
            self._url("/permission-response"),
            json={"request_id": request_id, "behavior": behavior},
        )
        resp.raise_for_status()
        return resp.json()

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Async generator yielding SSE events from the hub.

        Each yielded dict has keys `event` (str) and `data` (parsed JSON).
        Reconnects automatically on any error with exponential backoff
        capped at 10s.  Re-registers before each SSE connect so the hub
        knows about us after a restart.

        Re-register failures are logged at WARNING (was DEBUG before
        QM #206) and tracked in `register_failures_since_boot`. After
        `UNHEALTHY_NOTIFY_THRESHOLD` consecutive failures the client
        invokes `_health_notifier` once so the LLM gets a single
        actionable signal that the hub link is flapping.
        """
        delay = 0.5
        while True:
            try:
                # Re-register before connecting — idempotent, ensures the hub
                # has our entry even after a hub restart wiped its state.
                try:
                    await self.register(cwd=self._cwd, pid=None)
                except Exception as register_err:
                    logger.warning(
                        "SSE re-register failed (n=%d since boot): %s",
                        self._register_failures_since_boot,
                        register_err,
                    )
                    # Fire a one-time health notification when the failure
                    # streak crosses the threshold. The latch resets on the
                    # next successful register (see `register()`).
                    if (
                        self._consecutive_register_failures
                        >= UNHEALTHY_NOTIFY_THRESHOLD
                        and not self._unhealthy_notified
                        and self._health_notifier is not None
                    ):
                        self._unhealthy_notified = True
                        try:
                            await self._health_notifier({
                                "content": (
                                    f"tubemail-channel: hub re-register has "
                                    f"failed {self._consecutive_register_failures}x "
                                    f"in a row — replies may not be reaching the "
                                    f"orchestrator. Call channel_health() to confirm."
                                ),
                                "meta": {
                                    "source": "tubemail-channel",
                                    "kind": "channel_health",
                                    "level": "warn",
                                    "register_failures_since_boot":
                                        self._register_failures_since_boot,
                                    "consecutive_failures":
                                        self._consecutive_register_failures,
                                    "error": str(register_err),
                                },
                            })
                        except Exception:
                            # The notifier itself failing must not crash the
                            # SSE loop — that would compound the problem.
                            logger.exception("health_notifier raised")
                        #end try
                    #end if
                #end try

                async with aconnect_sse(
                    self._client,
                    "GET",
                    self._url("/stream"),
                    headers=self._headers,
                ) as event_source:
                    delay = 0.5  # reset backoff on successful connect
                    self._connected = True
                    try:
                        async for sse in event_source.aiter_sse():
                            try:
                                data = json.loads(sse.data) if sse.data else {}
                            except json.JSONDecodeError:
                                logger.warning("bad SSE payload: %s", sse.data)
                                continue
                            yield {"event": sse.event or "message", "data": data}
                        #end for
                    finally:
                        # Iterator exit (clean EOF or exception) — connection
                        # is no longer live.
                        self._connected = False
                    #end try
            except asyncio.CancelledError:
                self._connected = False
                raise
            except Exception as e:
                self._connected = False
                logger.warning("SSE disconnect: %s — reconnecting in %.1fs", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
