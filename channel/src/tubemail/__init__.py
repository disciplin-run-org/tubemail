"""tubemail: Claude Code channel plugin that relays events to TubeMail.

Loaded by `claude --dangerously-load-development-channels server:tubemail-channel`
as a stdio subprocess of a Claude Code worker session. Speaks JSON-RPC over
stdin/stdout to Claude Code and HTTP/SSE to the TubeMail hub.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _base_version() -> str:
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("tubemail")
    except Exception:
        return "unknown"


def _git_state(repo_dir: Path) -> str:
    """Return a short SHA and optional '.dirty' suffix, or empty on failure.

    Lets us distinguish multiple in-progress dev builds sharing the same
    pyproject version — critical for knowing which workers have the latest
    local edits.
    """
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip()
    except Exception:
        return ""
    dirty = ""
    try:
        subprocess.check_call(
            ["git", "-C", str(repo_dir), "diff", "--quiet", "HEAD"],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            timeout=2,
        )
    except subprocess.CalledProcessError:
        dirty = ".dirty"
    except Exception:
        dirty = ""
    return f"+{sha}{dirty}"


def _full_version() -> str:
    base = _base_version()
    # __file__ is .../tubemail/__init__.py; go up two to channel/, then
    # its parent which is the monorepo root where the git repo lives.
    repo_dir = Path(__file__).resolve().parent
    git = _git_state(repo_dir)
    return f"{base}{git}"


def _runtime_version() -> str:
    """Stable version + a process-start tag so a re-exec shows up as a
    different version string in tools that report `forwarder_version`
    (the web UI roster, `tm_list_workers`). Without this, two
    consecutive rolls produce identical version strings — code IS
    fresh, but operators see no signal that anything changed.

    Format: `0.1.0+a3b8020.dirty @22:13` — the time-of-day suffix is
    short enough to fit the version column and changes on every
    re-exec, including dev-mode edits where git state stays the same.
    """
    import time as _time
    base = _full_version()
    return f"{base} @{_time.strftime('%H:%M')}"


__version__ = _runtime_version()
