"""claude-tm — managed Claude Code worker session wired into TubeMail.

This is the user-facing entry point installed by ``pip install tubemail-channel``.
It launches ``claude`` inside :mod:`tubemail.manager` (a Python process
manager that runs claude in a pty) and supervises the manager with a
restart loop that matches the legacy bash script's semantics:

* exit code 0 → clean ``/exit``; loop terminates.
* exit code 42 (``EXIT_UPDATE_MANAGER``) → manager asked for a re-exec
  so it can pick up updated tubemail source. We restart the subprocess
  with ``--continue`` appended.
* anything else → crash. We retry up to ``TM_MAX_CRASH_RESTARTS``
  (default 5), sleeping 2s between attempts, then give up.

Usage::

    cd /path/to/project && claude-tm                  # → <project>-tm
    cd /path/to/project && claude-tm --role spec      # → <project>-spec-tm
    TM_WORKER_NAME=foo claude-tm                      # → foo-tm
    claude-tm --role spec --continue                  # extra args pass through

Environment::

    TUBEMAIL_SECRET        required. Bearer shared with the hub.
    TUBEMAIL_HUB_URL       default http://localhost:8001.
    TM_WORKER_NAME         override auto-derived worker name.
    TM_FORCE=1             start even if the pidfile says we're already running.
    TM_MAX_CRASH_RESTARTS  default 5.
    TUBEMAIL_ENV_FILE      path to a KEY=value file sourced before run.
    TM_SKIP_MCP_BOOTSTRAP=1  skip the auto-registration of the
                           ``tubemail-channel`` MCP entry in ``.mcp.json``
                           (for users who manage their MCP config externally).

Env files are layered, nearest first, and every layer is read — a file
that lacks a key never shadows a later file that has it. Precedence is
per key, not per file (first layer to define a key wins; environment
variables already set in the shell always win over all files):

1. ``$TUBEMAIL_ENV_FILE``, when set.
2. ``.env`` walking up from the current working directory (capped at
   five parent levels — covers the common case of a repo-root ``.env``
   while you launch ``claude-tm`` from a subdirectory, without scanning
   the entire filesystem).
3. ``~/.config/tubemail/.env``.

Layering matters because plenty of repos carry a ``.env`` for unrelated
reasons (a Vite frontend, say). Such a file supplies its own keys without
making the global ``~/.config/tubemail/.env`` fallback unreachable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=value file. Strips matched surrounding quotes."""
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
            val = val[1:-1]
        if key:
            out[key] = val
    return out


# Cap the upward .env walk so we never scan the entire filesystem. Five
# levels covers any reasonable monorepo layout (project → repo → group
# → org → workspace → home) without surprises.
_ENV_WALK_MAX_LEVELS = 5


def _find_dotenv_upward(start: Path) -> Path | None:
    """Walk up from ``start`` looking for ``.env``. Stop at the home dir or root."""
    home = Path.home().resolve()
    cur = start.resolve()
    seen = 0
    while seen <= _ENV_WALK_MAX_LEVELS:
        cand = cur / ".env"
        if cand.is_file():
            return cand
        if cur == home or cur.parent == cur:
            return None
        cur = cur.parent
        seen += 1
    return None


def _load_env_files() -> None:
    """Populate os.environ from optional env files, layering every candidate.

    Candidates are read nearest-first and ALL of them are read: ``setdefault``
    gives per-key first-wins precedence, so an earlier file beats a later one
    for keys it defines while later files still fill in the keys it omits.
    Never overwrites keys already present in the environment.

    Do not short-circuit after the first existing file — that made
    ``~/.config/tubemail/.env`` unreachable from any repo carrying an
    unrelated ``.env``. See test_load_env_files_falls_back_to_global_*.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("TUBEMAIL_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    found = _find_dotenv_upward(Path.cwd())
    if found is not None:
        candidates.append(found)
    candidates.append(Path.home() / ".config" / "tubemail" / ".env")
    for cand in candidates:
        if not cand.is_file():
            continue
        for k, v in _parse_env_file(cand).items():
            os.environ.setdefault(k, v)


def _has_tubemail_channel_entry(path: Path) -> bool:
    """True iff ``path`` is a readable JSON object whose ``mcpServers``
    map already contains a ``tubemail-channel`` key. Any read or parse
    failure returns False (treat the file as if it had no entry)."""
    try:
        content = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(content, dict):
        return False
    servers = content.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    return "tubemail-channel" in servers


def _ensure_mcp_channel_entry(
    *,
    project_root: Path | None = None,
    user_home: Path | None = None,
) -> str | None:
    """Idempotently register the ``tubemail-channel`` MCP server in
    ``~/.mcp.json`` so claude's
    ``--dangerously-load-development-channels server:tubemail-channel``
    flag can resolve it.

    The pip package installs the ``tubemail`` CLI on PATH but doesn't
    register the MCP entry — without this bootstrap, a freshly-installed
    user hits ``"no MCP server configured with that name"`` on the first
    ``claude-tm`` invocation and has to hand-edit JSON. (Andre's first
    run, 2026-05-17.)

    **Write target**: user-global ``~/.mcp.json``, NOT project-local
    ``./.mcp.json``. The earlier version of this bootstrap wrote into
    the project file, which dirtied the git tree of every consumer repo
    in the ecosystem (QM #430, 2026-05-20 — leanspecs-spec-tm flagged a
    persistent ``M .mcp.json`` showing up in audits across leanspecs,
    iris-qa, quartermaster, architrix, actuatrix). User-global keeps
    consumer repos clean and matches what Andre did manually on first
    install.

    Behavior:

    - If the current project's ``./.mcp.json`` already lists the entry,
      do nothing (legacy migration: workers that ran the old wrapper
      have the entry in their project file; we respect what's there
      rather than duplicate-register).
    - If ``~/.mcp.json`` already lists the entry, do nothing.
    - Otherwise write ``{"command": "tubemail"}`` into a new or merged
      ``~/.mcp.json``. The entry is intentionally minimal — no
      ``TUBEMAIL_SECRET`` or ``TUBEMAIL_HUB_URL`` in the ``env`` block —
      so secrets are inherited from ``claude-tm``'s env at spawn time
      and never land in a file.
    - If ``~/.mcp.json`` exists but isn't a valid JSON object, refuse
      to clobber it; print actionable instructions and return.

    Set ``TM_SKIP_MCP_BOOTSTRAP=1`` to opt out entirely (for users who
    manage their MCP config externally).

    Returns the absolute path of the file written, or ``None`` if no
    write was needed.
    """
    if os.environ.get("TM_SKIP_MCP_BOOTSTRAP", "").strip() == "1":
        return None

    project_root = project_root or Path.cwd()
    user_home = user_home or Path.home()

    project_path = project_root / ".mcp.json"
    user_path = user_home / ".mcp.json"

    # Already satisfied somewhere? Leave it alone — including any
    # user-customised entry (different command, extra args) so we
    # don't overwrite intentional tweaks. The project-local check is
    # for legacy compatibility: workers that ran the old wrapper have
    # the entry there; respect it rather than duplicate-register.
    if _has_tubemail_channel_entry(project_path):
        return None
    if _has_tubemail_channel_entry(user_path):
        return None

    # Merge into existing user file when possible; create when not.
    existing: dict
    if user_path.exists():
        try:
            parsed = json.loads(user_path.read_text())
        except (OSError, json.JSONDecodeError):
            print(
                f"claude-tm: {user_path} exists but isn't valid JSON; "
                "skipping auto-registration to avoid clobbering your edits.",
                file=sys.stderr,
            )
            print(
                "  Add this entry to your ~/.mcp.json manually so "
                "claude-tm can resolve the channel:",
                file=sys.stderr,
            )
            print(
                '    "tubemail-channel": {"command": "tubemail"}',
                file=sys.stderr,
            )
            return None
        if not isinstance(parsed, dict):
            print(
                f"claude-tm: {user_path} is JSON but not an object; "
                "skipping auto-registration. Add 'tubemail-channel' manually.",
                file=sys.stderr,
            )
            return None
        existing = parsed
    else:
        existing = {}

    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        print(
            f"claude-tm: {user_path}.mcpServers is not an object; "
            "skipping auto-registration. Add 'tubemail-channel' manually.",
            file=sys.stderr,
        )
        return None
    servers["tubemail-channel"] = {"command": "tubemail"}

    user_path.write_text(json.dumps(existing, indent=2) + "\n")
    print(
        f"claude-tm: registered 'tubemail-channel' MCP entry in {user_path}",
        file=sys.stderr,
    )
    print(
        "  (set TM_SKIP_MCP_BOOTSTRAP=1 to disable this auto-registration)",
        file=sys.stderr,
    )
    return str(user_path)


def _parse_args(argv: list[str]) -> tuple[str, list[str]]:
    """Pop ``--role NAME`` / ``--role=NAME`` from argv. Return ``(role, remaining)``."""
    role = ""
    out: list[str] = []
    it = iter(argv)
    for tok in it:
        if tok == "--role":
            try:
                role = next(it)
            except StopIteration:
                print("claude-tm: --role requires a value", file=sys.stderr)
                sys.exit(2)
        elif tok.startswith("--role="):
            role = tok[len("--role=") :]
        else:
            out.append(tok)
    return role, out


def _resolve_session_name(role: str) -> str:
    """Compute the worker session name.

    Precedence: ``$TM_WORKER_NAME`` > basename of cwd, with ``-tm`` always
    appended and ``-<role>`` injected before ``-tm`` when role is set.
    """
    override = os.environ.get("TM_WORKER_NAME", "").strip()
    if override:
        return f"{override}-tm"
    base = Path.cwd().name or "worker"
    if role:
        return f"{base}-{role}-tm"
    return f"{base}-tm"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another uid — still alive.
        return True
    except OSError:
        return False
    return True


def _check_pidfile(session_name: str) -> None:
    """Refuse to start if a live process already owns the pidfile."""
    pidfile = Path(f"/tmp/claude-tm-{session_name}.pid")
    if not pidfile.exists():
        return
    try:
        existing = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return
    if not _pid_alive(existing):
        return
    if os.environ.get("TM_FORCE", "").strip() == "1":
        print(
            f"claude-tm: TM_FORCE=1 — starting alongside existing pid {existing}",
            file=sys.stderr,
        )
        return
    print(
        f"claude-tm: worker '{session_name}' already running as pid {existing}",
        file=sys.stderr,
    )
    print(
        "claude-tm: pass TM_FORCE=1 to start anyway, "
        "or use --role <name> for a distinct session",
        file=sys.stderr,
    )
    sys.exit(1)


def _has_continue_flag(args: list[str]) -> bool:
    return any(a in {"--continue", "-c"} for a in args)


# Sentinel for "manager wants a re-exec so it can pick up updated source."
# Duplicated from tubemail.manager.EXIT_UPDATE_MANAGER to avoid importing
# the manager module (and all of its dependencies) in this thin wrapper.
EXIT_UPDATE_MANAGER = 42


_HELP_TEXT = """\
claude-tm — managed Claude Code worker session wired into TubeMail.

Usage:
  claude-tm                       # worker name = <basename of cwd>-tm
  claude-tm --role NAME           # name = <basename>-<role>-tm
  TM_WORKER_NAME=foo claude-tm    # name = foo-tm
  claude-tm --continue            # extra args forwarded to claude

Environment:
  TUBEMAIL_SECRET        required. Bearer shared with the hub.
  TUBEMAIL_HUB_URL       default http://localhost:8001.
  TM_WORKER_NAME         override auto-derived worker name.
  TM_FORCE=1             ignore an existing pidfile and start anyway.
  TM_MAX_CRASH_RESTARTS  default 5.
  TUBEMAIL_ENV_FILE      explicit path to a KEY=value file.
  TM_SKIP_MCP_BOOTSTRAP=1  skip the auto-registration of the
                         `tubemail-channel` MCP entry in `.mcp.json`.

Env files are layered, nearest first: $TUBEMAIL_ENV_FILE, then `.env`
walking up from cwd (cap: 5 parent levels), then ~/.config/tubemail/.env.
Every layer is read; the first one to define a key wins, so a local .env
without TUBEMAIL_SECRET still falls back to the global file. Existing
env vars are never overwritten.
"""


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {"-h", "--help"}:
        print(_HELP_TEXT)
        return

    _load_env_files()

    if not os.environ.get("TUBEMAIL_SECRET", "").strip():
        print(
            "claude-tm: TUBEMAIL_SECRET is not set.\n"
            "  Export it in your shell, drop it in ./.env or "
            "~/.config/tubemail/.env,\n"
            "  or point TUBEMAIL_ENV_FILE at a KEY=value file before running.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Make sure the tubemail-channel MCP entry exists so claude's
    # `--dangerously-load-development-channels server:tubemail-channel`
    # flag (set by manager.py) can resolve the channel without a
    # hand-edit. Idempotent; skipped when TM_SKIP_MCP_BOOTSTRAP=1.
    _ensure_mcp_channel_entry()

    role, passthru = _parse_args(sys.argv[1:])
    session_name = _resolve_session_name(role)
    _check_pidfile(session_name)

    try:
        max_crash = int(os.environ.get("TM_MAX_CRASH_RESTARTS", "5"))
    except ValueError:
        max_crash = 5
    crash_count = 0

    while True:
        cmd = [sys.executable, "-m", "tubemail.manager", session_name, *passthru]
        try:
            rc = subprocess.call(cmd)
        except KeyboardInterrupt:
            sys.exit(130)

        if rc == 0:
            return
        if rc == EXIT_UPDATE_MANAGER:
            print(
                "claude-tm: manager-update signal (rc=42), restarting",
                file=sys.stderr,
            )
            crash_count = 0
            if not _has_continue_flag(passthru):
                passthru.append("--continue")
            continue

        crash_count += 1
        if crash_count >= max_crash:
            print(
                f"claude-tm: {max_crash} crashes in a row "
                f"(last rc={rc}), giving up",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"claude-tm: manager exited rc={rc}, restart #{crash_count}",
            file=sys.stderr,
        )
        time.sleep(2)
        if not _has_continue_flag(passthru):
            passthru.append("--continue")


if __name__ == "__main__":
    main()
