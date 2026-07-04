---
name: restart
description: Restart this Claude Code session cleanly — picks up new CLAUDE.md, MCP tools, and skills on restart. Signals the claude-tm manager which types /exit into the session and restarts (with --continue by default; pass fresh=true to restart WITHOUT --continue for identity recovery).
---

# /restart — Clean restart via the claude-tm manager

You are restarting this Claude Code session to pick up changes (new CLAUDE.md,
new MCP tools, new skills, updated code). The manager process will type `/exit`
into the session, then restart claude.

Two modes:

- **Default (resume)** — the manager restarts with `--continue` so conversation
  context is preserved. Use for reloading CLAUDE.md, MCP tools, or code.
- **Fresh** — the manager restarts WITHOUT `--continue`, so the startup
  sequence performs the automatic `/rename` and the worker re-registers
  cleanly. Use when the current conversation is corrupted or the worker has
  lost its identity (typical after a self-issued `/clear` on a QM-driven job).

The fresh flag is **one-shot**: any subsequent crash-recovery restart in the
same manager loop reverts to `--continue`.

## Steps

1. **Determine the manager name.** The manager entity is `<session>-manager`.
   The session name follows the pattern `<dirname>-tm`, e.g. for a session
   named `leanspecs-tm`, the manager is `leanspecs-tm-manager`.

2. **Signal the manager via TubeMail MCP.**

   Resume (default) — preserves conversation context:
   ```
   mcp__tubemail__tm_send(
     worker="<session>-manager",
     message="restart",
     meta={"kind": "restart"}
   )
   ```

   Fresh — drops conversation context, re-registers cleanly:
   ```
   mcp__tubemail__tm_send(
     worker="<session>-manager",
     message="restart fresh",
     meta={"kind": "restart", "fresh": True}
   )
   ```

   The manager will set its restart flag (and, for fresh, its one-shot
   fresh-restart flag) and type `/exit` into the session.

3. **Confirm to the user.** After sending the signal, tell the user either:
   - "Restart signal sent. The manager will type /exit and restart with
     --continue in a few seconds. New CLAUDE.md and tools will be loaded on
     restart." (default), or
   - "Fresh restart signal sent. The manager will type /exit and restart the
     session WITHOUT --continue, so the startup /rename runs and the worker
     re-registers with a clean conversation. Once the new prompt is ready
     the manager will also auto-type /sync-inbox, so the fresh session
     catches up on any timeline events that arrived during the restart
     window instead of sitting idle." (fresh).

4. **Do nothing else.** The manager handles the actual exit and restart. Do NOT
   try to run /exit yourself — the manager types it for you via the pty.

## Important

- The manager types `/exit` into the pty (as if the user typed it). Claude exits
  with code 0. The manager sees the restart flag and restarts (with or without
  `--continue`, depending on the fresh flag).
- After restart, re-read CLAUDE.md and check for new tools with `refresh_tools()`.
- After a DEFAULT restart, run `/sync-inbox` yourself to catch any tubemail
  messages that arrived during the restart window — the channel plugin's SSE
  subscription was briefly down and doesn't replay missed events.
- After a FRESH restart, the manager auto-types `/sync-inbox` for you once
  the child's empty prompt is ready (detected via the status-bar "context N%"
  marker, then a short settle delay). The auto-catchup is scoped to the
  fresh cycle only; default and crash-recovery restarts do NOT auto-type it.
  On timeout (rare — child never reaches the ready prompt) the manager logs
  a warning and skips rather than typing into a startup dialog.
- Duplicate restart signals arriving within ~10 seconds of one already
  accepted are dropped by the manager and logged with a warning. This
  guards against client-side transport double-delivery (a dying
  session's SSE reconnect can replay the same event within 100–200ms)
  which would otherwise kill the newborn fresh child before it boots
  and silently downgrade the fresh restart to a crash-recovery
  --continue restart. A legitimate second restart a minute later is
  unaffected by the debounce.
- The fresh flag only affects the very next restart cycle. If the worker then
  crashes and the manager restarts it a second time, that second restart uses
  `--continue` (crash recovery is unchanged).
