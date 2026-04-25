"""Short-lived single-use tickets for browser→hub WebSocket auth.

Browsers cannot set custom headers on `new WebSocket()`. The bearer
stays in the Authorization header of a bearer-authed HTTPS POST to
`/api/pty-ticket`; the server issues a single-use, 30-second ticket;
the browser opens `wss://.../ws/pty/<worker>?ticket=<t>`. The ticket in
the URL may leak into access logs and browser history, but it's
worthless after one use or after 30s — the real secret never leaves
the header.

See jjstack/ceo-plans/jesper-main-eng-20260423-222000.md, Correction 3,
for the full rationale.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Ticket:
    token: str
    worker: str
    expires_at: float  # monotonic time


class TicketStore:
    """In-memory ticket map with atomic single-use semantics."""

    def __init__(self, ttl_s: float = 30.0):
        self._ttl = ttl_s
        self._tickets: dict[str, Ticket] = {}
        self._lock = asyncio.Lock()

    async def issue(self, worker: str) -> str:
        token = secrets.token_urlsafe(32)
        async with self._lock:
            self._tickets[token] = Ticket(
                token=token,
                worker=worker,
                expires_at=time.monotonic() + self._ttl,
            )
        return token

    async def consume(self, token: str, worker: str) -> bool:
        """Atomic pop-and-check. Returns True iff the token was valid,
        unexpired, and scoped to this worker. A valid token for a
        different worker is rejected (no lateral movement)."""
        async with self._lock:
            t = self._tickets.pop(token, None)
        if t is None:
            return False
        if t.worker != worker:
            logger.warning(
                "TicketStore.consume: ticket scoped to %s used against %s",
                t.worker, worker,
            )
            return False
        if time.monotonic() > t.expires_at:
            return False
        return True

    async def sweep(self) -> int:
        """Remove expired-but-unused tickets. Returns how many were evicted.
        Safe to call from a background task every N seconds."""
        async with self._lock:
            now = time.monotonic()
            stale = [k for k, v in self._tickets.items() if v.expires_at < now]
            for k in stale:
                del self._tickets[k]
        return len(stale)

    def size(self) -> int:
        """Current number of outstanding (unused, unexpired) tickets."""
        return len(self._tickets)
