"""Durable forwarding for permission_response events.

When the user (or an auto-approve hook) resolves a permission prompt at
the worker's terminal, Claude Code emits ``notifications/claude/channel/
permission`` and the channel plugin forwards it to the hub via
``HubClient.post_permission_response``. The naive path is a single POST
that logs and drops on failure — meaning a hub blip at the moment a
permission is resolved leaves the hub thinking the worker is still
``waiting_permission`` forever, even as the worker keeps producing
outbound events past the gate.

This module mirrors the Stop hook's retry+spool design (#205) for the
same class of bug:

1. Try the POST up to ``_MAX_RETRIES`` times with exponential backoff.
2. If all retries fail, write the resolution to the local spool at
   ``~/.claude/tubemail-spool/<worker>/permission-<ts>-<rid>.json``
   (mode 0600). On the next successful POST, the channel drains the
   spool oldest-first so resolutions land in the order they happened.
3. The channel also drains the spool at startup, so a hub blip that
   outlives the worker's session still recovers the moment the worker
   reconnects.

Spool path can be overridden with ``TUBEMAIL_PERMISSION_SPOOL_DIR`` for
tests; production never sets it. Capped at ``_SPOOL_CAP`` files per
worker so a long-running outage cannot exhaust disk — oldest entries
are dropped with a warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Backoff schedule between POST attempts. The Stop hook uses 0.5/2/5; we
# match it so operators don't have to remember two timing tables.
_BACKOFF_S: tuple[float, ...] = (0.5, 2.0, 5.0)
_MAX_RETRIES = len(_BACKOFF_S) + 1  # initial try + len(_BACKOFF_S) retries

# Cap per-worker spool depth so a multi-day outage cannot fill the disk.
# 200 covers far more permission resolutions than any real session
# generates between hub-recovery events; older entries are dropped LRU.
_SPOOL_CAP = 200

# Env var that lets tests sandbox the spool root.
SPOOL_ROOT_ENV = "TUBEMAIL_PERMISSION_SPOOL_DIR"


def _spool_root() -> Path:
    override = os.environ.get(SPOOL_ROOT_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude" / "tubemail-spool"


def _spool_dir(worker: str) -> Path:
    # The hub validates worker names before they ever reach this layer,
    # but normalise defensively: any "/" or ".." would otherwise let a
    # crafted name escape the spool root.
    safe = worker.replace("/", "_").replace("..", "_")
    return _spool_root() / safe


class PermissionResponseSpool:
    """Tracks per-worker spooled permission_response payloads.

    Owns the on-disk directory and the drain logic. Stateless across
    process restarts — every read consults disk so a fresh channel
    process picks up whatever the previous one left behind.
    """

    def __init__(self, worker: str) -> None:
        self._worker = worker

    @property
    def directory(self) -> Path:
        return _spool_dir(self._worker)

    def write(self, request_id: str, behavior: str) -> bool:
        """Persist one resolution to disk. Returns True on success.

        Caps the spool at ``_SPOOL_CAP`` entries — drops the oldest with
        a logged WARNING when full. Atomic via tmp file + os.replace so
        a partially-written entry never gets drained as a real one.
        """
        try:
            d = self.directory
            d.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(d, 0o700)
            existing = sorted(d.glob("permission-*.json"))
            while len(existing) >= _SPOOL_CAP:
                victim = existing.pop(0)
                logger.warning(
                    "permission spool full at %d for %s; dropping oldest %s",
                    _SPOOL_CAP, self._worker, victim.name,
                )
                with contextlib.suppress(OSError):
                    victim.unlink()
            ts = f"{time.time():.6f}"
            # request_id is already restricted by the channel to ascii
            # short tokens; still, sanitize defensively for filesystem use.
            safe_rid = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in request_id
            )[:32]
            path = d / f"permission-{ts}-{safe_rid}.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "request_id": request_id,
                "behavior": behavior,
                "spooled_at": ts,
            }))
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            return True
        except OSError as err:
            logger.error("permission spool write failed for %s: %s", self._worker, err)
            return False

    def list_pending(self) -> list[Path]:
        """Spooled entries oldest first. Empty list if directory absent."""
        if not self.directory.is_dir():
            return []
        return sorted(self.directory.glob("permission-*.json"))


async def post_permission_response_durable(
    hub: Any,
    spool: PermissionResponseSpool,
    request_id: str,
    behavior: str,
    *,
    sleep_fn=asyncio.sleep,
) -> bool:
    """Try to forward a permission resolution to the hub with retry+spool.

    Returns True if the POST succeeded (with or without retries), False
    if the resolution had to be spooled. Either way the caller can
    consider the resolution persisted — the spool drains on the next
    successful POST or on channel restart.

    ``sleep_fn`` is injectable so tests can run the retry chain at zero
    real-world latency.
    """
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            await hub.post_permission_response(request_id, behavior)
            return True
        except (httpx.HTTPError, OSError) as err:
            last_error = err
            if attempt < _MAX_RETRIES - 1:
                delay = _BACKOFF_S[attempt]
                logger.warning(
                    "post_permission_response attempt %d/%d failed (%s); "
                    "retrying in %.1fs",
                    attempt + 1, _MAX_RETRIES, err, delay,
                )
                await sleep_fn(delay)
            else:
                logger.warning(
                    "post_permission_response attempt %d/%d failed (%s); "
                    "spooling",
                    attempt + 1, _MAX_RETRIES, err,
                )
    spooled = spool.write(request_id, behavior)
    if not spooled:
        # Spool failed too — last-resort log so an operator scanning
        # logs sees the full loss path. The hub will still discover
        # the resolution next time the worker re-registers (which
        # clears all pending), so this is recoverable, but worth
        # surfacing.
        logger.error(
            "permission_response for request_id=%s lost — POST failed "
            "(%s) AND spool write failed; relying on next worker "
            "re-register to clear hub-side state",
            request_id, last_error,
        )
    return False


async def drain_spool(
    hub: Any,
    spool: PermissionResponseSpool,
    *,
    sleep_fn=asyncio.sleep,
) -> int:
    """POST every spooled permission_response oldest-first.

    Stops on the first failure so we don't burn the full retry chain on
    every entry when the hub is still down. Returns the count of
    entries successfully drained.

    Each successful POST removes the spool file; entries that fail to
    drain stay on disk for the next attempt.
    """
    drained = 0
    for path in spool.list_pending():
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as err:
            logger.warning(
                "skipping unreadable spool entry %s: %s", path.name, err,
            )
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        rid = payload.get("request_id", "")
        beh = payload.get("behavior", "")
        if not rid or beh not in ("allow", "deny"):
            logger.warning("skipping malformed spool entry %s", path.name)
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        try:
            await hub.post_permission_response(rid, beh)
        except (httpx.HTTPError, OSError) as err:
            logger.info(
                "drain_spool stopped at %s (%s); will retry next time",
                path.name, err,
            )
            return drained
        with contextlib.suppress(OSError):
            path.unlink()
        drained += 1
    if drained:
        logger.info("drain_spool: forwarded %d spooled responses", drained)
    return drained
