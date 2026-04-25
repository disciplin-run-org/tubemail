"""Per-worker pty fan-out registry for the Integrated Terminal cap.

Kept separate from the event-log fan-out in `engine.py` because pty
bytes are volatile (not persisted) and high-volume. Each attached
browser WebSocket owns its own queue; the registry fans bytes from the
manager-side POST to every attached client.

Two kinds of items flow through the queue:
- `("bytes", bytes)`: pty output to render in xterm (binary WS frame).
- `("text", str)`: control message as JSON (text WS frame). Used for
  size echo, future "manager exiting" notices, etc.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Tuple, Union

logger = logging.getLogger(__name__)


# Tagged item the WS handler dispatches on.
PtyItem = Union[Tuple[str, bytes], Tuple[str, str]]


class PtyBridgeRegistry:
    """Browser-side fan-out only. The manager-side pty reader POSTs
    bytes to /tubemail/<worker>/pty-out; the router calls
    `push_output(worker, bytes)` here; we deliver to every attached
    client queue.

    Client writes flow via a separate path (browser WS → hub → forwarder
    SSE `pty_data` → manager writes to pty) handled by the WS router
    directly, not this registry.

    The recorder is an optional persistent "subscriber": when set, every
    pty chunk is also teed to the RecordingManager, and `has_any_client`
    treats an active recording as a reason to keep the manager streaming
    even with zero browser clients attached.
    """

    def __init__(self, recorder: Any | None = None) -> None:
        # worker → list of client queues. Items are PtyItem tuples.
        self._clients: dict[str, list[asyncio.Queue[PtyItem]]] = {}
        self._lock = asyncio.Lock()
        self._recorder = recorder

    def set_recorder(self, recorder: Any | None) -> None:
        """Attach a recorder after construction. Server.py uses this so the
        recorder can hold a reference to the engine without circular wiring.
        """
        self._recorder = recorder

    async def attach(self, worker: str) -> asyncio.Queue[PtyItem]:
        q: asyncio.Queue[PtyItem] = asyncio.Queue(maxsize=512)
        async with self._lock:
            self._clients.setdefault(worker, []).append(q)
            count = len(self._clients[worker])
        logger.info("pty_bridge: client attached to %s (total=%d)", worker, count)
        return q

    async def detach(self, worker: str, q: asyncio.Queue[PtyItem]) -> int:
        async with self._lock:
            lst = self._clients.get(worker, [])
            if q in lst:
                lst.remove(q)
            remaining = len(lst)
            if remaining == 0:
                self._clients.pop(worker, None)
        logger.info("pty_bridge: client detached from %s (remaining=%d)", worker, remaining)
        return remaining

    def attached_count(self, worker: str) -> int:
        return len(self._clients.get(worker, []))

    def has_any_client(self, worker: str) -> bool:
        """True if any consumer wants the manager to keep streaming —
        either an attached browser WS or an active recording. Returning
        False here causes /pty-out to 404, which the manager treats as
        "stop streaming."
        """
        if self.attached_count(worker) > 0:
            return True
        if self._recorder is not None and self._recorder.is_recording(worker):
            return True
        return False

    async def push_output(self, worker: str, data: bytes) -> None:
        """Manager pushed pty output; tee to the recorder (if any) and fan
        to every attached client as a BINARY WS frame.

        Recorder writes happen first because the recording is persistent
        state — losing a chunk to a slow browser queue is acceptable, but
        a chunk missing from disk is unrecoverable.
        """
        if self._recorder is not None:
            try:
                self._recorder.write(worker, data)
            except Exception as e:
                # Recording must never break the bridge. Log and keep
                # fanning to browsers.
                logger.warning(
                    "pty_bridge: recorder write failed for %s: %s",
                    worker, e,
                )
        clients = list(self._clients.get(worker, []))
        for q in clients:
            try:
                q.put_nowait(("bytes", data))
            except asyncio.QueueFull:
                logger.warning(
                    "pty_bridge: client queue full for %s — dropping one frame",
                    worker,
                )
                try:
                    q.put_nowait(("bytes", b""))  # soft-close signal
                except asyncio.QueueFull:
                    pass

    async def push_control(self, worker: str, msg: dict[str, Any]) -> None:
        """Manager pushed a control message (size update, etc.); fan to
        every attached client as a TEXT WS frame carrying JSON."""
        clients = list(self._clients.get(worker, []))
        if not clients:
            return
        try:
            payload = json.dumps(msg)
        except (TypeError, ValueError) as e:
            logger.warning("pty_bridge: bad control payload: %s", e)
            return
        for q in clients:
            try:
                q.put_nowait(("text", payload))
            except asyncio.QueueFull:
                logger.warning(
                    "pty_bridge: client queue full for %s — dropping control",
                    worker,
                )
