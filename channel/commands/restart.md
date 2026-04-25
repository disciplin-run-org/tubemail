---
name: restart
description: Restart this Claude Code session cleanly — picks up new CLAUDE.md, MCP tools, and skills on restart. Signals the claude-tm manager which types /exit into the session and restarts with --continue to preserve conversation context.
---

# /restart — Clean restart via the claude-tm manager

You are restarting this Claude Code session to pick up changes (new CLAUDE.md,
new MCP tools, new skills, updated code). The manager process will type `/exit`
into the session, then restart claude with `--continue` so the conversation
context is preserved.

## Steps

1. **Determine the manager name.** The manager entity is `<session>-manager`.
   The session name follows the pattern `<dirname>-tm`, e.g. for a session
   named `leanspecs-tm`, the manager is `leanspecs-tm-manager`.

2. **Signal the manager via TubeMail MCP.** Call:
   ```
   mcp__tubemail__tm_send(
     worker="<session>-manager",
     message="restart",
     meta={"kind": "restart"}
   )
   ```
   The manager will set its restart flag and type `/exit` into the session.

3. **Confirm to the user.** After sending the signal, tell the user:
   "Restart signal sent. The manager will type /exit and restart with --continue
   in a few seconds. New CLAUDE.md and tools will be loaded on restart."

4. **Do nothing else.** The manager handles the actual exit and restart. Do NOT
   try to run /exit yourself — the manager types it for you via the pty.

## Important

- The manager types `/exit` into the pty (as if the user typed it). Claude exits
  with code 0. The manager sees the restart flag and restarts with `--continue`.
- After restart, re-read CLAUDE.md and check for new tools with `refresh_tools()`.
- After restart, run `/sync-inbox` to catch any tubemail messages that arrived
  during the restart window — the channel plugin's SSE subscription was briefly
  down and doesn't replay missed events.
