# tubemail-channel

A Claude Code plugin that gives a worker session a bidirectional control channel
to a TubeMail hub. It implements the `experimental.claude/channel` and
`experimental.claude/channel/permission` capabilities so an orchestrator (another
Claude Code session, or any HTTP client of the hub) can deliver work, observe
permission prompts, and receive structured replies.

## SECURITY WARNING — this plugin gives the configured hub full control of your Claude Code session

`tubemail-channel` lets the TubeMail hub at `TUBEMAIL_HUB_URL` drive the
Claude Code session it is installed in. Whoever controls that hub — or
whoever has the `TUBEMAIL_SECRET` bearer — can:

- Send arbitrary messages and harness commands (`/clear`, `/exit`,
  `/compact`, `/mcp …`, `/rename …`) into your session.
- Approve permission prompts remotely, granting the LLM tools you would
  have declined in person.
- Send raw keystrokes directly to the worker's pty, including shell
  commands that execute as your user.
- Read every screenshot, recording, and event from the session.
- Interrupt, restart, or stop the worker.

In short: anyone with control of the hub can do anything to your Claude
Code session that you could do at the keyboard.

**Only point `TUBEMAIL_HUB_URL` at a hub you 100% control and trust.**
Do not install this plugin against someone else's TubeMail instance, a
SaaS, or a public demo unless you fully accept that the operator can
read and drive your session. Treat `TUBEMAIL_SECRET` like a root
password — anyone with that value can do everything listed above
without ever touching the hub host.

This README documents the contract the plugin offers to the LLM running inside
the worker session — chiefly the **MCP tools** the LLM can call, the
**notifications** the LLM should pay attention to, and the **environment
variables** that wire it all together.

## Environment variables

| Var | Required | Purpose |
|---|---|---|
| `TM_WORKER_NAME` | yes | Worker name registered with the hub. The Stop hook self-skips when unset, so a session without this var behaves like a normal Claude Code session. |
| `TUBEMAIL_SECRET` | yes | Bearer token for hub HTTP. Never logged. |
| `TUBEMAIL_HUB_URL` | no (defaults to `http://localhost:8001`) | Base URL of the hub. **Empty string falls back to the default** — some launch wrappers export `TUBEMAIL_HUB_URL=''` instead of leaving it unset, so the empty case is treated as unset. |
| `TUBEMAIL_STOP_HOOK_RETRIES` | no (default `3`) | Stop-hook retry count. See `hooks/post_stop_relay.py`. |
| `TUBEMAIL_STOP_HOOK_VERIFY` | no (default off) | When `1`, the Stop hook re-fetches the just-posted event via `GET /tubemail/<worker>/events` to confirm persistence. Doubles request count; off by default. |
| `TUBEMAIL_STOP_HOOK_SPOOL_DIR` | no (test only) | Sandbox the per-worker spool directory. Production never sets this. |

## MCP tools

The plugin advertises three tools to the LLM via the standard MCP `tools/list`
surface. All three are safe for the LLM to call any time — they do not have
side effects beyond the explicitly named action.

### `reply`

Send a structured reply up to the orchestrator.

```json
{"text": "task complete", "meta": {"progress": 100}}
```

Fails with JsonRpcError `-32603` if the hub POST fails (after the hub-client's
default timeout). Use `channel_health` to confirm the link before relying on a
reply for critical messages.

### `ack`

Acknowledge an inbound channel event without a text body. Useful for quick
"received" responses.

Fails with JsonRpcError `-32603` if the hub POST fails. Note: this used to be
silently swallowed (QM #206 dogfooded the failure mode where the LLM thought
its ack landed but never did) — now the LLM learns the truth and can retry or
warn in its reply.

### `channel_health`

Return the channel plugin's view of the hub link. Inputs: none.

```json
{
  "connected": true,
  "registered": true,
  "register_failures_since_boot": 0,
  "last_outbound_success_at": 1714998765.123,
  "hub_url": "http://localhost:8001"
}
```

Use this **before emitting a critical reply** (e.g. a `qm-report` fence or a
PR-status update). If `connected` or `registered` is false, or
`last_outbound_success_at` is `null` after you have already called `reply` once
in this session, the link is unhealthy and your reply may not reach the
orchestrator. In that case, surface the status in your visible reply so the
human reading the timeline can see it.

## Slash commands

Installing this plugin also installs three slash commands the LLM can invoke
(or that the user can type) inside the worker session. They live in
`channel/commands/` and are auto-discovered by Claude Code at plugin load.

| Command | What it does |
|---|---|
| `/restart` | Cleanly restart this Claude Code session via the manager (manager types `/exit`, then re-execs with `--continue` so conversation context survives). Use after editing CLAUDE.md, MCP config, or skills. |
| `/sync-inbox` | After `/restart`, catch up on inbound tubemail events that arrived during the restart window (the SSE subscription was briefly down and doesn't replay). |
| `/reconnect-mcp` | Reconnect a failed MCP server on this worker without manually driving `/mcp`. Picks the right tool (`tm_self_reconnect_mcp` for any non-tubemail server, `mcp__tubemail-channel__reconnect_mcp` if tubemail itself is down). |

If you maintain a `CLAUDE.md` for your project, point workers at these
commands explicitly so the LLM knows when to reach for them. A minimal
snippet:

```markdown
## TubeMail worker conventions

- An MCP server shows ✘ failed (or its tools vanish): run `/reconnect-mcp`.
- After editing CLAUDE.md, MCP config, or installing new skills: `/restart`,
  then `/sync-inbox`.
- Never drive the `/mcp` dialog manually with screenshot+keystroke chains —
  use `/reconnect-mcp`, which is deterministic and survives the
  Remote-Control-view trap.
```

## Notifications the LLM should watch for

The channel plugin pushes events to the LLM via `notifications/claude/channel`.
Most carry a `meta.source` that identifies the origin. One specific shape is
worth handling explicitly:

### `meta.kind = "channel_health"`

A health-warning event. Two cases produce this:

1. **Init-time register failure.** The plugin booted but the hub didn't accept
   the registration. `meta.phase = "init"`, `meta.level = "error"`. Replies
   may not reach the orchestrator until the hub recovers.
2. **SSE-loop register failures crossed the threshold (5 consecutive).** The
   reconnect loop has been failing repeatedly. `meta.consecutive_failures`
   tells you how many.

Both forms include `meta.error` with the underlying exception message. If the
LLM sees one of these notifications, it should:

- Call `channel_health` to confirm the current state.
- If still unhealthy, include the warning in its visible reply so the human (or
  the next downstream consumer) is aware.

The notification fires **once per outage** — a successful register clears the
latch so a subsequent flap can fire its own notification.

## Stop-hook (POST-on-turn-end relay)

`hooks/post_stop_relay.py` runs at every Claude Code Stop event and POSTs the
assistant's last message text to `/tubemail/<worker>/outbound`. This is the
fallback path for when the LLM doesn't explicitly call `reply` — Quartermaster
and other auto-extract consumers read from the outbound stream, so a missed
reply means a missed qm-report fence.

### Durability layers

If the hub is briefly unreachable, the hook will not silently lose the event:

1. **Retry with backoff** — 3 attempts with 0.5s, 2s, 5s waits (configurable via
   `TUBEMAIL_STOP_HOOK_RETRIES`).
2. **Local spool** — when all retries fail, the body is written to
   `~/.claude/tubemail-spool/<worker>/<ts>-<sha>.json` (mode 0600). The next
   Stop event drains the spool oldest-first before posting the new event.
3. **Exit 2 only when both POST and spool fail** — Claude Code surfaces
   non-zero hook exits in the session UI, so the user sees the loss instead of
   it being silently swallowed.

## Troubleshooting

**"My qm-report fence isn't reaching QM."**

1. Call `channel_health()`. If `connected` is false or
   `register_failures_since_boot` > 0, the link has been unhealthy. Recovery
   is automatic but the historical event may have been spooled (Stop hook) or
   dropped (direct `reply` call).
2. Check `~/.claude/tubemail-spool/<worker>/` for spooled events that haven't
   drained yet — if entries are present, the hub is unreachable from the
   spool's perspective.
3. Read `tail -f` on the worker's claude-tm log; the Stop hook logs WARNING
   lines per retry attempt and per spool eviction.

**"channel_health says registered but my replies aren't appearing in
`tm_receive`."**

That state usually means the hub accepted the registration but the
forwarder->hub path is fine while the orchestrator->hub read path is failing.
Check the hub's `/health` endpoint and `tm_status` from the orchestrator side.

**"I'm seeing `channel_health` notifications repeatedly."**

Each notification corresponds to a fresh outage — the latch is fire-once per
outage. If they keep coming, the link is genuinely flapping (not the
notification logic). Check the hub's logs and the network path between the
worker and the hub.

## References

- Stop-hook durability: QM queue 187 (dogfooded), QM queue 205 (fix).
- Channel health surface: QM queue 206.
- Hub HTTP routes: `src/tubemail_hub/bridge/http.py`.
- Engine state model: `src/tubemail_hub/bridge/engine.py`.
