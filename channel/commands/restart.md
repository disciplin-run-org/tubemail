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
     re-registers with a clean conversation." (fresh).

4. **Do nothing else.** The manager handles the actual exit and restart. Do NOT
   try to run /exit yourself — the manager types it for you via the pty.

## Important

- The manager types `/exit` into the pty (as if the user typed it). Claude exits
  with code 0. The manager sees the restart flag and restarts (with or without
  `--continue`, depending on the fresh flag).
- After restart, re-read CLAUDE.md and check for new tools with `refresh_tools()`.
- After restart, run `/sync-inbox` to catch any tubemail messages that arrived
  during the restart window — the channel plugin's SSE subscription was briefly
  down and doesn't replay missed events.
- The fresh flag only affects the very next restart cycle. If the worker then
  crashes and the manager restarts it a second time, that second restart uses
  `--continue` (crash recovery is unchanged).
