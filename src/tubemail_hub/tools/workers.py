"""Orchestrator-facing MCP tools.

The Orchestrator Claude Code session calls these tools to drive worker
Claude Code sessions connected to TubeMail via the tubemail forwarder.

Tool names follow the `<service>_<action>` convention (`tm_*`) so they
don't collide when multiple MCP servers are loaded together.

Two meta-tools — `get_instructions` and `refresh_tools` — stay unprefixed
by convention; every MCP server in this ecosystem ships them with the same
names so the AI can rely on them without special-casing per server.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

from tubemail_hub.bridge.engine import BridgeEngine

logger = logging.getLogger(__name__)

# Built-in harness commands that must be typed into the pty by the manager
# process (the model cannot invoke these). Everything else — including skills
# like /python-coder, /investigate — goes through tubemail to the model.
HARNESS_COMMANDS = {
    # Session management
    "/exit",
    "/quit",
    "/clear",
    "/reset",
    "/new",
    "/resume",
    "/continue",
    "/branch",
    "/fork",
    "/rename",
    "/compact",
    # Configuration
    "/config",
    "/settings",
    "/model",
    "/effort",
    "/fast",
    "/theme",
    "/color",
    "/keybindings",
    "/permissions",
    "/allowed-tools",
    "/plan",
    # Code & file
    "/diff",
    "/rewind",
    "/checkpoint",
    "/copy",
    "/export",
    "/add-dir",
    # Debugging & diagnostics
    "/debug",
    "/doctor",
    "/hooks",
    "/context",
    "/cost",
    "/usage",
    "/status",
    "/stats",
    "/insights",
    # IDE & environment
    "/ide",
    "/desktop",
    "/app",
    "/terminal-setup",
    "/statusline",
    "/sandbox",
    # Account & auth
    "/login",
    "/logout",
    "/upgrade",
    "/extra-usage",
    "/passes",
    "/privacy-settings",
    # Web & remote
    "/remote-control",
    "/rc",
    "/teleport",
    "/tp",
    "/autofix-pr",
    "/remote-env",
    # MCP & integrations
    "/mcp",
    "/chrome",
    "/install-github-app",
    "/install-slack-app",
    # Cloud platforms
    "/setup-bedrock",
    "/setup-vertex",
    # Memory & agents
    "/memory",
    "/agents",
    "/init",
    # Misc
    "/help",
    "/verbose",
    "/btw",
    "/tasks",
    "/bashes",
    "/release-notes",
    "/powerup",
    "/reload-plugins",
    "/team-onboarding",
    "/plugin",
    "/voice",
    "/stickers",
    "/mobile",
    "/ios",
    "/android",
    "/web-setup",
}


def _is_harness_command(message: str) -> bool:
    """True if the message is a built-in harness command that needs pty
    routing."""
    first_word = message.strip().split()[0] if message.strip() else ""
    return first_word in HARNESS_COMMANDS


def _detect_caller_worker() -> str | None:
    """Best-effort detection of which worker is calling this MCP tool.

    Checks (in priority order):
    1. The ``x-tm-worker`` HTTP header on the inbound MCP request — a
       forward-compatible signal nothing sends today, but worth wiring
       so a future ``.mcp.json`` env-substituted header (or a client-
       side header injection) can fold in without a hub change.
    2. ``TM_WORKER_NAME`` in the hub process's env — works when the hub
       is colocated with a single worker (typical local dev where one
       claude-tm session both runs the hub and acts as the worker).

    Returns the worker name string, or ``None`` if no signal is
    available. This is *advisory* — callers MUST gracefully fall through
    when ``None`` is returned, because in containerized multi-tenant
    deployments neither signal is set today.
    """
    try:
        from fastmcp.server.dependencies import get_http_request

        req = get_http_request()
        if req is not None:
            hdr = req.headers.get("x-tm-worker", "")
            if hdr and hdr.strip():
                return hdr.strip()
    except Exception:
        # No active HTTP request context (test harness, in-process call,
        # or stdio transport) — fall through to env-var detection.
        pass
    env = os.environ.get("TM_WORKER_NAME", "").strip()
    return env or None


def register(mcp, engine: BridgeEngine) -> None:
    """Register worker-orchestration tools + meta-tools on the given
    FastMCP."""

    # TODO(v0.2): make the project list configurable via env var.
    # Every ecosystem project always appears, even if no worker is running.
    # Matching is by the cwd basename of each worker, so role-scoped workers
    # (e.g. leanspecs-code-tm, leanspecs-spec-tm) group under their project.
    _ECOSYSTEM_PROJECTS = [
        "leanspecs",
        "iris-qa",
        "architrix",
        "actuatrix",
        "tubemail",
    ]

    def _project_of(w: dict) -> str:
        cwd = w.get("cwd", "")
        if cwd:
            return Path(cwd).name
        name = w["name"].removesuffix("-tm")
        for proj in _ECOSYSTEM_PROJECTS:
            if name == proj or name.startswith(f"{proj}-"):
                return proj
        return ""

    @mcp.tool
    def tm_list_workers(include_stale: bool = False) -> str:
        """List all worker Claude Code sessions with online/offline status.

        Returns a color-coded formatted table:
        - 🟢 online — forwarder has an active SSE connection
        - 💤 exited cleanly — user /exit'd (forwarder POSTed /goodbye)
        - 🔴 offline — no active connection and no clean-exit signal
          (crashed, hung, killed, or network lost)

        All ecosystem projects (leanspecs, iris-qa, architrix, actuatrix,
        tubemail) always appear. Workers are grouped by project (by
        cwd basename), so role-scoped workers like leanspecs-code-tm and
        leanspecs-spec-tm render as roles under a single leanspecs header.
        Manager entries are shown inline. Set `include_stale=True` to also
        include test leftovers.
        """
        all_workers = engine.list_workers()

        # Manager online lookup + forwarder version from manager's state
        manager_online: dict[str, bool] = {}
        manager_version: dict[str, str] = {}
        for w in all_workers:
            if w["name"].endswith("-manager"):
                base = w["name"].removesuffix("-manager")
                manager_online[base] = w["online"]
                manager_version[base] = w.get("forwarder_version", "")

        # Bucket real workers by project; non-ecosystem -tm workers go to extras
        by_project: dict[str, list[dict]] = {p: [] for p in _ECOSYSTEM_PROJECTS}
        extras: list[dict] = []
        for w in all_workers:
            if w["name"].endswith("-manager"):
                continue
            proj = _project_of(w)
            if proj in by_project:
                by_project[proj].append(w)
            elif w["name"].endswith("-tm") or include_stale:
                extras.append(w)

        def _status_icon(w: dict) -> str:
            # 🟢 online, 💤 cleanly exited (user typed /exit and POSTed
            # /goodbye), 🔴 offline (crashed / hung / killed — no goodbye).
            if w["online"]:
                return "🟢"
            if w.get("exited_cleanly"):
                return "💤"
            return "🔴"

        def _fmt_row(w: dict, indent: str = "") -> str:
            status = _status_icon(w)
            mgr_up = manager_online.get(w["name"])
            mgr = "mgr:🟢" if mgr_up else ("mgr:🔴" if mgr_up is not None else "mgr:--")
            version = manager_version.get(w["name"], "") or "—"
            pending = (
                f" ⚠️{w['pending_count']} pending" if w.get("pending_count") else ""
            )
            # Strip the monorepo root prefix to keep the row visually compact.
            # DISCIPLIN_RUN_HOST_ROOT is set by docker-compose to the operator's
            # disciplin-run checkout path. Falls back to no-op when unset.
            _host_root = os.environ.get("DISCIPLIN_RUN_HOST_ROOT", "")
            _prefix = (_host_root.rstrip("/") + "/") if _host_root else ""
            cwd = w.get("cwd", "")
            if _prefix and cwd.startswith(_prefix):
                cwd = cwd[len(_prefix):]
            name_w = max(10, 25 - len(indent))
            return (
                f"{indent}{status} {w['name']:<{name_w}} {mgr:<7} "
                f"{version:<22} {w['state']:<20} {cwd}{pending}"
            )

        lines: list[str] = []
        for proj in _ECOSYSTEM_PROJECTS:
            workers = sorted(by_project[proj], key=lambda x: x["name"])
            if not workers:
                lines.append(
                    f"🔴 {proj:<25} {'mgr:--':<7} {'—':<22} " f"{'not started':<20}"
                )
            elif len(workers) == 1:
                lines.append(_fmt_row(workers[0]))
            else:
                any_online = any(w["online"] for w in workers)
                all_clean = all(
                    w.get("exited_cleanly") for w in workers if not w["online"]
                )
                if any_online:
                    header_status = "🟢"
                elif all_clean:
                    header_status = "💤"
                else:
                    header_status = "🔴"
                lines.append(
                    f"{header_status} {proj:<25} {'':<7} {'':<22} "
                    f"{len(workers)} roles"
                )
                for i, w in enumerate(workers):
                    branch = "└─ " if i == len(workers) - 1 else "├─ "
                    lines.append(_fmt_row(w, indent=branch))

        for w in sorted(extras, key=lambda x: (not x["online"], x["name"])):
            lines.append(_fmt_row(w))

        header = (
            f"{'':2} {'Worker':<25} {'Mgr':<7} {'Ver':<22} "
            f"{'State':<20} {'Directory'}"
        )
        return header + "\n" + "\n".join(lines)

    @mcp.tool
    async def tm_send(
        worker: str, message: str, meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a command or message to a worker Claude Code session.

        Routing is automatic:
        - Built-in harness commands (/compact, /clear, /exit, /effort, /model,
          /help, /verbose, /fast, /cost, /mcp, /resume, /usage, /terminal-setup)
          are typed into the session's terminal by the manager process via pty.
        - Everything else (work orders, skill invocations like /python-coder,
          free-text messages) goes through tubemail as a channel event that the
          worker's Claude model sees and acts on.

        You don't need to think about routing — just send the message.

        Returns `{event_id, queued_at, routed_to}` — pass `event_id` as the
        `since` cursor to `tm_receive` / `tm_wait_for_activity` to read replies.
        """
        if _is_harness_command(message):
            # Route to manager — it types the command into the pty
            manager = f"{worker}-manager"
            event = await engine.enqueue_inbound(
                manager, message, {"kind": f"type:{message}"}
            )
            return {
                "event_id": event.event_id,
                "queued_at": event.ts,
                "routed_to": "manager (harness command)",
            }
        else:
            # Route through tubemail channel to the worker's Claude model
            event = await engine.enqueue_inbound(worker, message, meta or {})
            return {
                "event_id": event.event_id,
                "queued_at": event.ts,
                "routed_to": "worker (tubemail channel)",
            }

    @mcp.tool
    def tm_receive(
        worker: str, since: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Read recent events on a worker's timeline.

        Events include inbound messages (from orchestrator), outbound replies
        (from worker's reply tool), permission requests, permission responses,
        and interrupts. If `since` is provided (an event_id from a prior call),
        only events strictly after that id are returned. Without `since`, returns
        the last `limit` events.

        Use this to poll for worker replies after calling `tm_send`, or combine
        with `tm_wait_for_activity` to avoid tight polling loops.
        """
        events = engine.events_since(worker, since=since, limit=limit)
        return [
            {
                "event_id": e.event_id,
                "ts": e.ts,
                "kind": e.kind,
                "content": e.content,
                "meta": e.meta,
            }
            for e in events
        ]

    @mcp.tool
    def tm_status(worker: str) -> dict[str, Any]:
        """Get the current state of a worker.

        Returns `state` as `idle` / `busy` / `waiting_permission` / `unknown`
        plus `pending_count` (number of unresolved permission requests),
        `last_activity` (epoch timestamp of most recent event), and `event_count`.

        `waiting_permission` means the worker has at least one tool-approval
        prompt awaiting a decision via `tm_respond_permission`.
        """
        ws = engine.get_worker(worker)
        if ws is None:
            return {
                "state": "unknown",
                "pending_count": 0,
                "last_activity": 0.0,
                "event_count": 0,
            }
        return {
            "state": ws.status_state(),
            "pending_count": len(ws.pending_permissions),
            "last_activity": ws.last_activity,
            "event_count": len(ws.events),
        }

    @mcp.tool
    def tm_my_inbox(limit: int = 20) -> dict[str, Any]:
        """Return recent events on the caller worker's own timeline.

        DEPRECATED for the standard containerized topology — resolves
        identity from ``TM_WORKER_NAME`` in the HUB process's own env,
        which is empty when the hub runs in a Docker container and the
        worker runs on the host. It returns a misleading "TM_WORKER_NAME
        not set" error even when the worker session's env is populated
        correctly. Prefer the /sync-inbox skill flow: read
        ``$TM_WORKER_NAME`` from the session's own shell, then call
        ``tm_receive(worker=<name>)`` with an explicit worker argument.
        See QM #555 for the transcript that caught this on
        iris-qa-tm's fresh-restart e2e.

        Kept in place so old callers still route through the hub without
        crashing; do not extend it. Any new call site that needs the
        caller-worker inbox should adopt the env-read + ``tm_receive``
        pattern instead.

        Resolves identity from the TM_WORKER_NAME env var set by the
        claude-tm wrapper. Intended for use on restart via /sync-inbox:
        the worker catches up on any commands that arrived during the
        restart window (python exit → bash re-exec → --continue) that
        weren't delivered through the channel plugin because SSE was
        briefly down.

        Returns `{worker, events}` where events follow the same shape as
        tm_receive (inbound, outbound, permission_request,
        permission_response, interrupt). Compare inbound events against
        your conversation context to decide what you already acted on.

        Returns `{error}` if TM_WORKER_NAME is unset — this tool only
        works inside a claude-tm-launched session.
        """
        name = os.environ.get("TM_WORKER_NAME", "").strip()
        if not name:
            return {
                "error": "TM_WORKER_NAME not set — tm_my_inbox only works from inside a claude-tm session",
            }
        events = engine.events_since(name, since=None, limit=limit)
        return {
            "worker": name,
            "events": [
                {
                    "event_id": e.event_id,
                    "ts": e.ts,
                    "kind": e.kind,
                    "content": e.content,
                    "meta": e.meta,
                }
                for e in events
            ],
        }

    @mcp.tool(task=True)
    async def tm_wait_for_activity(
        worker: str, since: str | None = None, timeout_s: float = 30.0
    ) -> list[dict[str, Any]]:
        """Block until the worker produces a new event, or until timeout.

        Returns events strictly after the `since` cursor. Use this instead of
        polling `tm_receive` in a tight loop when you want to wait for the
        worker's reply to a `tm_send` call.

        Long-running by design (`task=True`): the tool returns a task ID
        immediately and runs the wait in the background, so callers can
        pass `timeout_s > 60` without hitting Claude Code's MCP-call
        timeout. Returns an empty list on timeout (not an error).
        """
        events = await engine.wait_for_activity(
            worker, since=since, timeout_s=timeout_s
        )
        return [
            {
                "event_id": e.event_id,
                "ts": e.ts,
                "kind": e.kind,
                "content": e.content,
                "meta": e.meta,
            }
            for e in events
        ]

    @mcp.tool
    def tm_pending_permissions(worker: str | None = None) -> list[dict[str, Any]]:
        """List all pending tool-approval prompts across workers (or one
        worker).

        When a worker's Claude Code session wants to use a tool that requires
        approval (Bash, Edit, etc.), the prompt is forwarded here via Claude
        Code's permission-relay capability. Each entry has `worker`, `request_id`,
        `tool_name`, `description`, and `input_preview`.

        Call `tm_respond_permission` to allow or deny. Omit the `worker` argument
        to see pending prompts across all workers.
        """
        return engine.list_pending_permissions(worker=worker)

    @mcp.tool
    async def tm_respond_permission(
        worker: str,
        request_id: str,
        behavior: Literal["allow", "deny"],
    ) -> dict[str, Any]:
        """Allow or deny a pending tool-approval prompt from a worker.

        `request_id` comes from `tm_pending_permissions`. `behavior` must be
        either `"allow"` or `"deny"`. Returns `{ok: bool}` — `ok` is `False`
        if no matching pending request was found (e.g. already resolved, or
        the worker has disconnected).

        Once resolved, the worker's Claude Code session proceeds immediately
        with the original tool call (allow) or surfaces a denial (deny).
        """
        ok = await engine.resolve_permission(worker, request_id, behavior)
        return {"ok": ok}

    @mcp.tool
    async def tm_sweep_stale_permissions(
        worker: str | None = None,
    ) -> dict[str, Any]:
        """Drop pending_permission entries that have gone stale.

        Three independent staleness signals, each catching a different
        failure class:

        1. **Event-timeline path** — a worker outbound event (Stop
           relay, ack, explicit reply) recorded strictly AFTER a
           pending request's timestamp proves the gate resolved
           locally (an LLM can't produce output while blocked on a
           permission prompt). Catches the "hub blip lost the resolve
           event" class.
        2. **Connection-drop path** — when a worker's last SSE
           subscriber leaves without a re-registration, every pending
           entry is cleared (`subscribe()`'s `finally` block in the
           bridge engine). Catches the "claude-tm restarted, channel
           reconnected with empty local pending" class.
        3. **Heartbeat threshold** — if a request's matching event
           exists but the worker has produced NO event for
           `_SWEEP_HEARTBEAT_S` (15 min default), the entry is
           presumed orphaned. Catches the "Claude session went stale
           while the forwarder subscription stayed alive" class
           (QM #416, 2026-05-18).

        Pass `worker` to sweep one specific worker, or omit to sweep
        every known worker. Returns
        `{swept: {<worker>: <count>}, total: <int>}` — only workers
        where at least one entry was dropped appear in `swept`. The
        hub also runs this sweep at startup AND on every minute tick
        (`_periodic_sweep_pending_permissions` in `server.py`), so
        recovery time is bounded to ~60s even without operator
        intervention.
        """
        if worker is not None:
            n = await engine.sweep_stale_permissions(worker)
            return {
                "swept": {worker: n} if n else {},
                "total": n,
            }
        results = await engine.sweep_stale_permissions_all()
        return {
            "swept": results,
            "total": sum(results.values()),
        }

    @mcp.tool
    async def tm_interrupt(worker: str) -> dict[str, Any]:
        """Send an interrupt event to a worker.

        The worker's prompt must be trained to honor interrupts — typical
        behavior is to pause the current work, acknowledge via the `ack`
        channel tool, and await further instruction. Use this to course-correct
        a worker that has gone off-track without killing its session.
        """
        event = await engine.send_interrupt(worker)
        return {"ok": True, "event_id": event.event_id}

    # ── Manager control tools ──────────────────────────────────────────────

    @mcp.tool
    async def tm_restart(worker: str, fresh: bool = False) -> dict[str, Any]:
        """Force-restart a worker's Claude process via its manager.

        Routes a `force_restart` command to `<worker>-manager`. The manager
        kills the Claude child process and restarts it. Use this when:
        - Claude is hung and not responding to tubemail messages
        - The tubemail channel to the worker is dead
        - You need to force a restart that Claude can't do for itself

        By default (fresh=false) the manager restarts with `--continue`,
        preserving conversation context. Set fresh=true when the point of
        the restart is to RECOVER from a bad conversation state (identity
        loss after a self-issued /clear, an unrecoverable prompt loop,
        etc.): the manager omits `--continue` for exactly one restart
        cycle, so the child gets a fresh conversation and the startup
        sequence types the automatic /rename to re-register cleanly.
        Once the child's empty prompt is ready the manager also auto-types
        `/sync-inbox` so the fresh session catches up on any timeline
        events that arrived during the restart window (missed work orders,
        pending QM items) instead of sitting idle at an empty prompt.
        Subsequent crash-recovery restarts revert to `--continue` and
        skip the auto-catchup.

        Duplicate restart signals arriving within ~10 seconds of one
        already accepted are dropped by the manager (logged as a
        warning). This debounce guards against client-side transport
        double-delivery — the same signal being replayed within
        ~100ms — which would otherwise kill the newborn fresh child
        before it finishes booting and silently downgrade the fresh
        restart to a --continue crash-recovery restart. Legitimate
        consecutive restarts a minute apart are unaffected.

        For a polite restart (when Claude IS responsive), send a tubemail
        message asking it to run the `/restart` skill instead.
        """
        manager = f"{worker}-manager"
        meta: dict[str, Any] = {"kind": "force_restart"}
        if fresh:
            meta["fresh"] = True
        event = await engine.enqueue_inbound(manager, "force_restart", meta)
        return {
            "ok": True,
            "event_id": event.event_id,
            "routed_to": manager,
            "fresh": bool(fresh),
        }

    @mcp.tool
    async def tm_stop(worker: str) -> dict[str, Any]:
        """Force-stop a worker and its manager process entirely.

        Routes a `force_stop` command to `<worker>-manager`. The manager
        kills the Claude child process, removes its PID file, unregisters
        from the hub, and exits. The worker is completely gone after this.

        Use this when you want to shut down a worker permanently. For a
        polite stop (pause work, stay running), use `tm_interrupt` instead.
        For a polite exit, send 'please /exit' via `tm_send`.
        """
        manager = f"{worker}-manager"
        event = await engine.enqueue_inbound(
            manager, "force_stop", {"kind": "force_stop"}
        )
        return {"ok": True, "event_id": event.event_id, "routed_to": manager}

    @mcp.tool
    async def tm_purge_worker(worker: str) -> dict[str, Any]:
        """Permanently remove a worker from the registry — both the in-memory
        state and the on-disk file under
        `/data/tubemail/workers/<worker>.json`.

        Use this to drop stale entries left behind by Claude sessions
        that exited without re-registering — the registry otherwise
        grows for the life of the data volume. Companion to the
        startup auto-purge that drops offline workers > 24h old (and
        the per-worker delete in the web UI's roster).

        Online workers and ones with pending permissions can still be
        purged with this tool — it is unconditional. Use
        `tm_stop` / `tm_interrupt` for live workers; use this only on
        ones you're sure are dead.
        """
        ok = await engine.purge_worker(worker)
        return {"ok": ok, "worker": worker}

    @mcp.tool
    async def tm_keystroke(worker: str, keys: str) -> dict[str, Any]:
        """Send raw keystrokes to a worker's terminal.

        Use this to interact with interactive dialogs (menus, prompts)
        that require arrow keys, Escape, number selection, etc.

        `keys` is a human-readable key sequence. Supported tokens
        (case-insensitive, space-separated):

            enter, esc/escape, up, down, left, right, tab,
            backspace, delete, space, or any literal text.

        Examples:
            tm_keystroke("worker", "down down enter")  — move down twice, select
            tm_keystroke("worker", "escape")            — dismiss dialog
            tm_keystroke("worker", "2 enter")           — type 2, press Enter
            tm_keystroke("worker", "hello enter")       — type hello, press Enter
        """
        KEY_MAP = {
            "enter": b"\r",
            "esc": b"\x1b",
            "escape": b"\x1b",
            "up": b"\x1b[A",
            "down": b"\x1b[B",
            "right": b"\x1b[C",
            "left": b"\x1b[D",
            "tab": b"\t",
            "backspace": b"\x7f",
            "delete": b"\x1b[3~",
            "space": b" ",
        }
        raw = bytearray()
        for token in keys.split():
            mapped = KEY_MAP.get(token.lower())
            if mapped:
                raw.extend(mapped)
            else:
                raw.extend(token.encode())

        hex_str = raw.hex()
        manager = f"{worker}-manager"
        event = await engine.enqueue_inbound(
            manager, f"keystroke:{keys}", {"kind": f"keystroke:{hex_str}"}
        )
        return {
            "event_id": event.event_id,
            "queued_at": event.ts,
            "routed_to": manager,
            "bytes_sent": len(raw),
        }

    @mcp.tool
    async def tm_update_manager(worker: str, force: bool = False) -> dict[str, Any]:
        """Re-exec a worker's tubemail.manager Python process to pick up
        updated source.

        Use after shipping a change to the tubemail channel code (e.g.
        new manager features) when you don't want to walk to the worker's
        terminal and kill/relaunch it by hand.

        Safety gate: refuses unless the worker's state is `idle`. A running
        turn, a pending permission prompt, or an unknown worker all block
        the update so in-flight work isn't interrupted. The /exit keystroke
        the manager types is buffered until claude is ready, but while the
        worker is on a permission prompt that keystroke would be typed
        into the dialog as text. Pass `force=True` to bypass the check
        (e.g. the worker is genuinely stuck and needs recycling anyway).

        Flow: manager receives `kind=update_manager`, types /exit into the
        claude child, exits python with code 42. The claude-tm bash
        wrapper's restart loop sees 42 and re-execs python — which reloads
        tubemail from disk (editable install picks up new source).
        `--continue` is added automatically so claude resumes the same
        conversation; the session name (and role baked into it) is
        preserved.

        Returns `{event_id, routed_to}` on dispatch, or `{error, state}`
        if the safety gate declined. Update takes several seconds; poll
        `tm_list_workers` for the new manager's forwarder_version to
        confirm.
        """
        if not force:
            ws = engine.get_worker(worker)
            state = ws.status_state() if ws is not None else "unknown"
            if state != "idle":
                return {
                    "error": f"worker not idle (state={state}); pass force=True to override",
                    "state": state,
                    "pending_count": len(ws.pending_permissions) if ws else 0,
                }
        manager = f"{worker}-manager"
        event = await engine.enqueue_inbound(
            manager, "update_manager", {"kind": "update_manager"}
        )
        return {
            "event_id": event.event_id,
            "queued_at": event.ts,
            "routed_to": manager,
            "forced": force,
            "note": "worker will re-exec its python manager and resume claude with --continue",
        }

    @mcp.tool
    async def tm_clear_and_send(
        worker: str,
        message: str,
        delay_s: float = 1.5,
        restore_name: bool = True,
    ) -> dict[str, Any]:
        """Atomically /clear the worker's conversation, then dispatch a new
        task.

        Use when starting an unrelated work order and you want a fresh
        context — prevents prior-turn reasoning bias and context rot.
        See the `context-hygiene` skill for when to clear vs keep.

        Flow:
          1. /clear is typed into the worker's pty via the manager.
          2. Wait `delay_s` for claude to finish wiping its conversation.
          3. If `restore_name` (default), type `/rename <worker>` into the
             pty so the session name survives the clear. Claude's /clear
             resets the session to the directory-derived default; without
             this step the worker reappears under its repo name and
             tubemail loses track of which session is which.
          4. Wait `delay_s` again for the rename to settle.
          5. The actual `message` is routed through the tubemail channel
             to the now-fresh claude session.

        Do NOT use this when continuing an in-flight task or when the
        worker is waiting on a permission prompt — you'll lose work and
        the /clear keystroke can hit the wrong surface.

        Returns event_ids for both the clear and the message so you can
        track them independently. When `restore_name` is true the response
        also carries `rename_event_id`.
        """
        # Safety gate — same rule as tm_update_manager
        ws = engine.get_worker(worker)
        state = ws.status_state() if ws is not None else "unknown"
        if state != "idle":
            return {
                "error": f"worker not idle (state={state}); clear+send is only safe when idle",
                "state": state,
                "pending_count": len(ws.pending_permissions) if ws else 0,
            }
        manager = f"{worker}-manager"
        import asyncio as _asyncio

        clear_event = await engine.enqueue_inbound(manager, "/clear", {"kind": "clear"})
        await _asyncio.sleep(max(0.0, delay_s))

        rename_event_id: str | None = None
        if restore_name:
            # Restore the session name claude resets after /clear. Routed
            # to the manager (it owns the pty), not the worker — the
            # manager's rename: handler types `/rename <name>\r` into the
            # session. The previous delay_s gave /clear time to finish
            # so this keystroke doesn't bleed into an unsettled prompt.
            rename_event = await engine.enqueue_inbound(
                manager, f"/rename {worker}", {"kind": f"rename:{worker}"}
            )
            rename_event_id = rename_event.event_id
            await _asyncio.sleep(max(0.0, delay_s))
        #end if

        msg_event = await engine.enqueue_inbound(worker, message, {})
        return {
            "clear_event_id": clear_event.event_id,
            "rename_event_id": rename_event_id,
            "message_event_id": msg_event.event_id,
            "delay_s": delay_s,
            "restored_name": restore_name,
            "routed_to": {
                "clear": manager,
                "rename": manager if restore_name else None,
                "message": worker,
            },
        }

    @mcp.tool(task=True)
    async def tm_reconnect_mcp(worker: str, server: str) -> dict[str, Any]:
        """Reconnect ANOTHER worker's failed MCP server (orchestrator path —
        the hub round-trips through that worker's manager).

        For YOUR OWN session's failed MCP server, use
        ``mcp__tubemail-channel__reconnect_mcp(server=<name>)`` instead —
        it talks to the local claude-tm manager over a Unix socket and
        works even when the tubemail hub itself is the failed MCP. The
        orchestrator path here cannot recover its own session: if your
        MCP transport to tubemail is stale, the call returns "Session
        not found" from the protocol layer before any tool dispatch.

        The worker's manager drives the /mcp dialog deterministically:
        open dialog → navigate to the named server → select Reconnect.
        Returns when the manager reports the outcome (up to ~25 s).

        Long-running by design (`task=True`): the dialog driver does up
        to 22 s of screen-polling between keystrokes, which would race
        Claude Code's 60 s MCP timeout if a manager-disconnect storm
        added a few extra seconds of hub latency. Returns a task ID
        immediately, polls in the background, and surfaces the final
        result via the standard MCP task-status protocol.

        Use this instead of tm_screenshot+tm_keystroke chains when an MCP
        server shows `✘ failed` in the worker's /mcp dialog. The manual
        path is slow and fragile — raw `enter` can trigger "Remote Control
        view" shortcuts, arrow keys sometimes don't advance the cursor,
        and you can't see the worker's screen from their own session.

        Returns `{ok, server, detail}`. On timeout returns
        `{error, detail}` without a definitive ok flag. When the caller's
        identity is detectable AND matches ``worker`` (self-target),
        returns ``{ok: false, server, detail, suggested_tool}`` instead
        of round-tripping the hub — the orchestrator path is the wrong
        tool for that case.
        """
        caller = _detect_caller_worker()
        if caller is not None and caller == worker:
            return {
                "ok": False,
                "server": server,
                "detail": (
                    "self-target via the hub will fail when the hub or your "
                    "own session is stale — use "
                    f"mcp__tubemail-channel__reconnect_mcp(server='{server}')"
                    " instead, which talks to the local manager via a Unix "
                    "socket and bypasses the hub."
                ),
                "suggested_tool": "mcp__tubemail-channel__reconnect_mcp",
            }
        manager = f"{worker}-manager"
        # Anchor the wait at the inbound event we just enqueued — without
        # this, wait_for_activity(since=None) returns instantly with
        # whatever stale events are already in the manager's history,
        # so we'd return the timeout error even when the manager
        # eventually posts a successful reconnect_mcp_result.
        inbound = await engine.enqueue_inbound(
            manager,
            f"reconnect_mcp:{server}",
            {"kind": f"reconnect_mcp:{server}"},
        )
        result_event = await engine.wait_for_matching_event(
            manager,
            since=inbound.event_id,
            match=lambda e: (
                e.kind == "outbound" and e.meta.get("kind") == "reconnect_mcp_result"
            ),
            timeout_s=30.0,
        )
        if result_event is not None:
            import json

            try:
                return json.loads(result_event.content)
            except (json.JSONDecodeError, ValueError):
                return {"raw": result_event.content}
        return {
            "error": "manager did not post a reconnect_mcp_result within 30s",
            "detail": "worker may be unresponsive; try tm_screenshot to inspect",
        }

    @mcp.tool(task=True)
    async def tm_self_reconnect_mcp(server: str) -> dict[str, Any]:
        """Reconnect a failed MCP server on THIS worker.

        Convenience wrapper around `tm_reconnect_mcp` for self-reconnect:
        the worker name is resolved from the `TM_WORKER_NAME` env var set
        by the claude-tm wrapper, so the LLM does not have to thread its
        own identity through the call.

        Use this any time a `mcp__<server>__*` tool starts returning
        "server disconnected" or vanishes from the available tool list,
        AND the tubemail MCP itself is still reachable. If tubemail is
        the failed server, this tool is unreachable too — use the
        channel-side `mcp__tubemail-channel__reconnect_mcp(server)` tool
        instead, which talks to the local manager via a Unix socket and
        bypasses the hub.

        Returns `{ok, server, detail}` (same shape as tm_reconnect_mcp)
        or `{error}` if `TM_WORKER_NAME` is unset (this tool only works
        from inside a claude-tm-launched session).
        """
        worker = os.environ.get("TM_WORKER_NAME", "").strip()
        if not worker:
            return {
                "error": (
                    "TM_WORKER_NAME not set — tm_self_reconnect_mcp only works "
                    "from inside a claude-tm session. Use tm_reconnect_mcp"
                    "(worker, server) explicitly instead."
                ),
            }
        manager = f"{worker}-manager"
        inbound = await engine.enqueue_inbound(
            manager,
            f"reconnect_mcp:{server}",
            {"kind": f"reconnect_mcp:{server}"},
        )
        result_event = await engine.wait_for_matching_event(
            manager,
            since=inbound.event_id,
            match=lambda e: (
                e.kind == "outbound" and e.meta.get("kind") == "reconnect_mcp_result"
            ),
            timeout_s=30.0,
        )
        if result_event is not None:
            import json

            try:
                return json.loads(result_event.content)
            except (json.JSONDecodeError, ValueError):
                return {"raw": result_event.content}
        return {
            "error": "manager did not post a reconnect_mcp_result within 30s",
            "detail": "worker may be unresponsive; try tm_screenshot to inspect",
        }

    @mcp.tool
    async def tm_health(worker: str) -> dict[str, Any]:
        """Get process-level health stats from a worker's manager.

        Routes a `health_check` request to `<worker>-manager`. The manager
        replies with: cpu_percent, memory_mb, uptime_s, child_alive, and
        restart_count. Use this to decide whether a seemingly-hung worker
        is actually doing work (high CPU) or truly stuck (zero CPU for
        minutes).

        Returns the health data after waiting for the manager's response.
        """
        manager = f"{worker}-manager"
        inbound = await engine.enqueue_inbound(
            manager, "health_check", {"kind": "health_check"}
        )
        # Wait for the manager to reply with FRESH health stats. Anchor
        # at the inbound we just sent so we don't return a stale
        # health_response from a prior call.
        result_event = await engine.wait_for_matching_event(
            manager,
            since=inbound.event_id,
            match=lambda e: (
                e.kind == "outbound" and e.meta.get("kind") == "health_response"
            ),
            timeout_s=5.0,
        )
        if result_event is not None:
            import json

            try:
                return json.loads(result_event.content)
            except (json.JSONDecodeError, ValueError):
                return {"raw": result_event.content}
        return {"error": "manager did not respond within 5s", "child_alive": "unknown"}

    # ── Recording tools ──────────────────────────────────────────────────

    @mcp.tool
    async def tm_recording_toggle(worker: str, enabled: bool) -> dict[str, Any]:
        """Turn pty-output recording on or off for a single worker.

        When on, every byte the worker's manager emits is teed to two files
        on the hub: an asciinema `.cast` (full fidelity, replayable) and a
        `.frames.jsonl` (ANSI-stripped text frames, what `tm_get_recording`
        reads). Files rotate by size; older recordings are GC'd.

        New workers inherit the global default (set on /settings) at first
        register. This tool overrides the per-worker flag and persists it
        across hub restarts. Returns the resulting status snapshot.
        """
        ws = engine.get_worker(worker)
        if ws is None:
            return {"ok": False, "error": f"unknown worker: {worker!r}"}
        ok = await engine.set_recording_enabled(worker, enabled)
        rec = engine.recorder
        status = (
            rec.status(worker)
            if rec is not None
            else {
                "worker": worker,
                "enabled": enabled,
                "active_file": None,
                "files": [],
            }
        )
        return {"ok": ok, **status}

    @mcp.tool
    def tm_recording_status(worker: str) -> dict[str, Any]:
        """Return the current recording state for a worker.

        Includes the on/off flag, the name of the active cast file (if
        recording is on), and metadata for every file on disk for this
        worker (path, size, modified, active). Use this before calling
        `tm_get_recording` so you know which files to slice.
        """
        rec = engine.recorder
        ws = engine.get_worker(worker)
        if rec is None:
            return {
                "worker": worker,
                "enabled": ws.recording_enabled if ws else False,
                "active_file": None,
                "files": [],
                "note": "recorder not configured on this hub",
            }
        return rec.status(worker)

    @mcp.tool
    def tm_get_recording(
        worker: str,
        since: str | None = None,
        until: str | None = None,
        grep: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Read recorded text frames for a worker, filtered by time + regex.

        Each frame is `{"t": ISO-8601 UTC, "delta": "<text>"}` — one entry
        per chunk the manager streamed, with ANSI escape codes removed so
        the text is grep-friendly. Frames span every `.frames.jsonl` file
        on disk for the worker in chronological order.

        Args:
          since: Inclusive lower bound, ISO-8601 (e.g. "2026-04-25T12:00:00Z").
          until: Exclusive upper bound, ISO-8601.
          grep: Regex matched against the `delta` field.
          limit: Max frames returned. Default 200; pass tighter `since` to page.

        Returns `{worker, frames, truncated}` where `truncated` is True if
        the slice held more than `limit` and the oldest in-window frames
        were returned.
        """
        rec = engine.recorder
        if rec is None:
            return {
                "worker": worker,
                "frames": [],
                "truncated": False,
                "error": "recorder not configured on this hub",
            }
        frames = rec.read_frames(
            worker,
            since=since,
            until=until,
            grep=grep,
            limit=limit + 1,
        )
        truncated = len(frames) > limit
        if truncated:
            frames = frames[:limit]
        return {"worker": worker, "frames": frames, "truncated": truncated}

    @mcp.tool
    async def tm_screenshot(worker: str, max_lines: int = 50) -> str:
        """Capture what's currently on a worker's terminal screen.

        The manager reads its pty output buffer and returns the last
        `max_lines` lines of ANSI-stripped text. Use this to see what a
        worker is doing without needing it to respond — works even when
        the worker is mid-generation, stuck on a prompt, or hung.

        Returns the screen content as plain text.
        """
        manager = f"{worker}-manager"
        inbound = await engine.enqueue_inbound(
            manager, "screenshot", {"kind": "screenshot"}
        )
        # Anchor the wait at the inbound we just sent — without this,
        # `since=None` returns a stale screenshot from a previous call
        # and the live screen never gets captured.
        result_event = await engine.wait_for_matching_event(
            manager,
            since=inbound.event_id,
            match=lambda e: (
                e.kind == "outbound" and e.meta.get("kind") == "screenshot"
            ),
            timeout_s=5.0,
        )
        if result_event is not None:
            return result_event.content
        return "(manager did not respond within 5s — may be offline)"

    # ── Meta tools (unprefixed by convention) ────────────────────────────────

    @mcp.tool
    def get_instructions() -> str:
        """Return TubeMail's server usage instructions, recommended workflows,
        and domain concepts.

        Call this to re-read the instructions at any time — especially
        after context compaction or when unsure how to use the server's
        tools.
        """
        from tubemail_hub.server import SERVER_INSTRUCTIONS

        return SERVER_INSTRUCTIONS

    from tubemail_hub.refresh import register_refresh_tool

    register_refresh_tool(mcp)


__all__ = ["register"]
