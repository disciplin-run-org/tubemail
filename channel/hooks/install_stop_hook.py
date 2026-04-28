#!/usr/bin/env python3
"""Idempotent installer for the tubemail-channel Stop hook.

Adds a `Stop` entry to the user's `~/.claude/settings.json` that
runs `post_stop_relay.py` on every assistant turn ending. The hook
itself self-skips when not in a tubemail-channel session
(`TM_WORKER_NAME` env var unset), so installing it system-wide is
safe — non-tubemail Claude Code sessions are unaffected.

Idempotent — running this script twice produces the same final
state. Safe to call from `claude-tm` on every invocation.

Pattern mirrors the existing `auto-approve-safe.sh` PreToolUse hook
that the user manually installed earlier; this one is auto-installed
by the manager so the user doesn't have to.
"""

from __future__ import annotations

# Standard Libraries
import json
import sys
from pathlib import Path


HOOK_SCRIPT_PATH = (
    Path(__file__).resolve().parent / "post_stop_relay.py"
)
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
HOOK_COMMAND = f"python3 {HOOK_SCRIPT_PATH}"


def _stderr(msg: str) -> None:
    sys.stderr.write(f"[install-stop-hook] {msg}\n")


def install() -> int:
    """Add the Stop hook to ~/.claude/settings.json. Returns 0 on success
    or no-op-already-present, 1 on hard failure."""
    if not HOOK_SCRIPT_PATH.exists():
        _stderr(f"hook script not found at {HOOK_SCRIPT_PATH}")
        return 1
    #end if

    if not SETTINGS_PATH.exists():
        # User hasn't created their settings.json yet — bail rather than
        # creating one, since we don't want to dictate their permissions
        # config.
        _stderr(
            f"{SETTINGS_PATH} does not exist; skipping. "
            "Run Claude Code at least once to generate it, then re-run."
        )
        return 0
    #end if

    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError) as err:
        _stderr(f"could not read {SETTINGS_PATH}: {err}")
        return 1
    #end try

    if not isinstance(settings, dict):
        _stderr(f"{SETTINGS_PATH} is not a JSON object")
        return 1
    #end if

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        _stderr("settings.hooks is not an object")
        return 1
    #end if

    stop_entries = hooks.get("Stop")
    if not isinstance(stop_entries, list):
        stop_entries = []
    #end if

    # Idempotent check — match by the exact command string.
    for entry in stop_entries:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks", []):
            if not isinstance(h, dict):
                continue
            if h.get("type") == "command" and h.get("command") == HOOK_COMMAND:
                # Already installed — nothing to do.
                return 0
            #end if
        #end for
    #end for

    # Append our entry.
    stop_entries.append(
        {
            "matcher": "",
            "hooks": [
                {"type": "command", "command": HOOK_COMMAND}
            ],
        }
    )
    hooks["Stop"] = stop_entries
    settings["hooks"] = hooks

    # Write back atomically — write to a temp file, then rename.
    tmp_path = SETTINGS_PATH.with_suffix(".json.tmp-tubemail-install")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        #end with
        tmp_path.replace(SETTINGS_PATH)
    except OSError as err:
        _stderr(f"write failed: {err}")
        try:
            tmp_path.unlink()
        except OSError:
            pass
        #end try
        return 1
    #end try

    _stderr(f"installed Stop hook -> {HOOK_COMMAND}")
    return 0


if __name__ == "__main__":
    sys.exit(install())
