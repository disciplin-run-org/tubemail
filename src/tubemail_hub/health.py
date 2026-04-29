"""Standardized /health endpoint for the TubeMail hub.

Stripped from the monorepo helper — only the infrastructure fields tubemail
actually uses. No config/LiteLLM/GitHub/workspace/KPI wiring (those belong to
product MCP services, not a transport layer).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


def _read_git_sha() -> str:
    """Return the short (7-char) git SHA of the source the hub is running
    against, or '' on any failure. Drives the workers-roster "current vs
    stale" badge in the SPA: workers with the same SHA are running the
    same code as the hub; different SHA means restart-manager will
    update them.

    Resolution order:
      1. TUBEMAIL_GIT_SHA env var — explicit operator override.
      2. /host-tubemail-gitdir — when the parent
         docker-compose.override.yml bind-mounts the tubemail
         submodule's gitdir from the monorepo (.git/modules/tubemail),
         either git or a pure-Python HEAD parse can read the SHA from
         it.
      3. /host-tubemail/.git — when tubemail is checked out standalone
         (not as a submodule), .git is a real directory in the repo.
      4. '' if nothing above worked. The SPA falls back to its
         registered_at-based heuristic in that case.
    """
    env_sha = os.environ.get("TUBEMAIL_GIT_SHA", "").strip()
    if env_sha:
        return env_sha[:7]

    candidates = [
        Path("/host-tubemail-gitdir"),
        Path("/host-tubemail/.git"),
    ]
    for git_dir in candidates:
        if not git_dir.exists() or not git_dir.is_dir():
            continue

        try:
            sha = subprocess.check_output(
                ["git", "--git-dir", str(git_dir), "rev-parse",
                 "--short", "HEAD"],
                stderr=subprocess.DEVNULL, timeout=2,
            ).decode().strip()
            if sha:
                return sha
        except Exception:
            pass

        # Pure-Python fallback: read HEAD directly. Handles both
        # "ref: refs/heads/main" (loose) and packed-refs cases. Works
        # even if the git binary is missing from the container image.
        try:
            head = (git_dir / "HEAD").read_text().strip()
            if head.startswith("ref: "):
                ref = head[5:].strip()
                ref_file = git_dir / ref
                if ref_file.exists():
                    return ref_file.read_text().strip()[:7]
                packed = git_dir / "packed-refs"
                if packed.exists():
                    for line in packed.read_text().splitlines():
                        line = line.strip()
                        if line.endswith(f" {ref}"):
                            return line.split()[0][:7]
                continue
            return head[:7]  # detached HEAD
        except Exception:
            continue
    return ""


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
        "git_sha": _read_git_sha(),
        "uptime": uptime_str,
        "disk_mb": disk_mb,
        "safe_to_restart": not (is_busy and is_busy()),
    }
