"""HTTP client for the TubeMail tubemail bridge.

Forwarders use this to POST events up to the hub and subscribe to the
SSE stream for server-pushed events. Bearer-auth'd with TUBEMAIL_SECRET.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx
from httpx_sse import aconnect_sse

logger = logging.getLogger(__name__)


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

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self, path: str) -> str:
        return f"{self._base}/tubemail/{self._worker}{path}"

    async def register(self, cwd: str, pid: int | None = None) -> dict[str, Any]:
        resp = await self._client.post(
            self._url("/register"),
            json={"cwd": cwd, "pid": pid},
        )
        resp.raise_for_status()
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
        """
        delay = 0.5
        while True:
            try:
                # Re-register before connecting — idempotent, ensures the hub
                # has our entry even after a hub restart wiped its state.
                try:
                    await self.register(cwd=self._cwd, pid=None)
                except Exception:
                    logger.debug("re-register before SSE failed (will retry)")

                async with aconnect_sse(
                    self._client,
                    "GET",
                    self._url("/stream"),
                    headers=self._headers,
                ) as event_source:
                    delay = 0.5  # reset backoff on successful connect
                    async for sse in event_source.aiter_sse():
                        try:
                            data = json.loads(sse.data) if sse.data else {}
                        except json.JSONDecodeError:
                            logger.warning("bad SSE payload: %s", sse.data)
                            continue
                        yield {"event": sse.event or "message", "data": data}
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("SSE disconnect: %s — reconnecting in %.1fs", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
