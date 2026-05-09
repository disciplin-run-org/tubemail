---
name: reconnect-mcp
description: Reconnect a failed MCP server on this worker without manually driving /mcp. Picks the right tool based on which server failed (tubemail itself vs. anything else) and falls back gracefully. Trigger when a `mcp__<server>__*` tool returns "server disconnected" or vanishes from the available tool list, when /mcp shows ✘ failed, or when the user says "reconnect <server>" / "<server> mcp died" / "fix mcp connection".
---

# /reconnect-mcp — Reconnect a failed MCP server on this worker

You are running as a tubemail worker (`$TM_WORKER_NAME` is set). One of your
MCP servers stopped responding — either a system-reminder announced that
`mcp__<server>__*` tools are no longer available, or a tool call returned
the canonical "server disconnected" error, or `/mcp` shows `✘ failed`.

**Never drive /mcp manually with screenshot+keystroke chains.** They're
slow, fragile, and `enter` can trigger Remote Control view. Use one of the
two deterministic tools below.

## Decision: which tool

| Failed server | Tool to call | Why |
|---|---|---|
| Anything **except** `tubemail` (e.g. `leanspecs`, `iris-qa`, `quartermaster`) | `mcp__tubemail__tm_self_reconnect_mcp(server="<name>")` | Routes through the tubemail hub to your own manager, which drives /mcp deterministically. |
| `tubemail` itself | `mcp__tubemail-channel__reconnect_mcp(server="tubemail")` | The hub is unreachable, so the channel plugin talks to your manager directly via a local Unix socket — no hub round-trip needed. |
| You're not sure which one is the channel and which is the hub | Try the channel-side tool first — it works in both cases. | The channel-side tool tries the local socket first and falls back to the hub. |

Both tools return `{ok: bool, server: str, detail: str}`.

## Examples

Self-reconnect a failed leanspecs MCP (tubemail still up):

```
mcp__tubemail__tm_self_reconnect_mcp(server="leanspecs")
```

Reconnect the tubemail MCP itself when it has dropped:

```
mcp__tubemail-channel__reconnect_mcp(server="tubemail")
```

After each call, verify the reconnect by re-running a representative tool
from the reconnected server (e.g. `mcp__leanspecs__settings_read` or
`mcp__tubemail__tm_status`).

## Anti-patterns (stop doing these)

- ❌ Manually driving `/mcp` via `tm_screenshot` + `tm_keystroke` + sleeps.
- ❌ `tm_send(worker=self, message="/mcp")` — types the slash command but
  doesn't navigate the dialog. The cleanup dance (escape Remote Control,
  re-open, navigate) is exactly what these tools exist to do for you.
- ❌ Asking the user to run `/mcp` themselves — that wastes their attention
  when the tool exists.
- ❌ Threading your own worker name through `tm_reconnect_mcp(worker, server)`
  when `tm_self_reconnect_mcp(server)` already reads `$TM_WORKER_NAME`.

## When NOT to use

- The MCP **server** itself is down (e.g. the leanspecs container is
  stopped). This skill reconnects the *client*, not the server. Bring the
  container back up first, then reconnect.
- The failure is upstream (OAuth handshake broken, server not in
  `.mcp.json`). The reconnect will just fail again — fix the cause.

## Return shape

```
{
  "ok": true,
  "server": "leanspecs",
  "detail": "reconnected"
}
```

On failure:

```
{
  "ok": false,
  "server": "leanspecs",
  "detail": "server not found in dialog; listed: [google-workspace, iris-qa, ...]"
}
```

If `detail` mentions `server not found` or an OAuth/Auth error, the fix
is upstream — escalate to the orchestrator or a human, don't loop on
reconnect.
