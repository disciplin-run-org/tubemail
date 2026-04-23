"""Standardized /health endpoint for the TubeMail hub.

Stripped from the monorepo helper — only the infrastructure fields tubemail
actually uses. No config/LiteLLM/GitHub/workspace/KPI wiring (those belong to
product MCP services, not a transport layer).
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable


async def build_health_response(
    service: str,
    version: str,
    start_time: float,
    *,
    is_busy: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Build a health response with uptime + disk usage + busy state."""
    uptime_seconds = int(time.time() - start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m" if hours else f"{minutes}m {secs}s"

    disk_mb = 0.0
    try:
        for dirpath, _dirnames, filenames in os.walk("/data"):
            for f in filenames:
                disk_mb += os.path.getsize(os.path.join(dirpath, f))
        disk_mb = round(disk_mb / (1024 * 1024), 1)
    except Exception:
        pass

    return {
        "status": "ok",
        "service": service,
        "version": version,
        "uptime": uptime_str,
        "disk_mb": disk_mb,
        "safe_to_restart": not (is_busy and is_busy()),
    }
