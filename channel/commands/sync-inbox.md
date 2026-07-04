---
name: sync-inbox
description: After a claude-tm restart, catch up on commands that arrived during the restart window. Resolves the worker's own name from its session environment (TM_WORKER_NAME), reads its timeline with tm_receive, and compares against conversation context to find inbound commands not yet acted on.
---

# /sync-inbox — Catch up on commands that arrived during restart

You are starting (or resuming via `--continue`) after the claude-tm manager
re-exec'd python. The channel plugin's SSE subscription was briefly down
during that window. Any tubemail message that arrived in that gap is
persisted on the hub but was NOT delivered to your conversation as a
live channel event — so you need to pull it explicitly.

## Steps

1. **Resolve your worker name from the SESSION environment.** Use Bash
   to read the env var directly — this MUST run in the worker session,
   not on the hub, because the hub process's own env is unrelated to
   yours in every containerized deployment.

   ```
   echo "$TM_WORKER_NAME"
   ```

   Expected output: a single non-empty line like `iris-qa-tm` or
   `PycharmProjects-tm`. Every claude-tm-launched session sets this at
   the pty child, inherited from the manager process (see
   `channel/src/tubemail/manager.py:1850`: `os.environ["TM_WORKER_NAME"]
   = session_name`).

   If the output is empty, skip to "Genuinely not a claude-tm worker"
   below.

2. **Read your own timeline via `tm_receive`.**

   ```
   mcp__tubemail__tm_receive(worker="<name from step 1>", limit=20)
   ```

   Returns a list of recent events on your own timeline, mixed
   inbound / outbound / permission-request / permission-response /
   interrupt.

   Do NOT call `tm_my_inbox` for this. That tool resolves identity from
   the HUB process's `os.environ`, which is empty in the standard
   containerized topology and returns a misleading "TM_WORKER_NAME not
   set" error even when your session's env is populated correctly.
   `tm_receive` with an explicit `worker=` argument is the identity-safe
   read.

3. **Scan for inbound events you haven't yet acted on.** For each
   `kind=inbound` event:
   - Check your conversation context: did you see this message's text
     arrive and respond to it (outbound reply, tool calls that advanced
     the work, or ack)?
   - Look at the timeline: is there a matching `kind=outbound` from you
     right after this inbound, or a downstream `permission_request` that
     stems from the inbound's ask? Either confirms the inbound was
     handled.

4. **Process unhandled inbound events now.** For any inbound you can't
   confirm you handled, treat it as a fresh work order arriving this turn.
   Run it per your normal channel-event handling.

5. **Prefer false positives over false negatives.** If you can't tell
   whether you handled an event, treat it as unhandled and re-do it —
   re-doing a small ack or read is strictly better than dropping a work
   order. The exception: destructive or expensive operations (bulk deletes,
   model-fired LLM calls on the full spec) — for those, ask the
   orchestrator first via `reply` rather than re-executing blind.

## Genuinely not a claude-tm worker

If step 1's `echo "$TM_WORKER_NAME"` prints an empty line, this session
was launched outside the claude-tm wrapper (e.g. a plain `claude` CLI
run in a terminal). There is no worker timeline to reconcile — no SSE
subscription window ever existed, so there is no missed work by
definition.

Reply:

```
/sync-inbox: TM_WORKER_NAME empty in session env — not a claude-tm
worker. No worker timeline exists, no catch-up needed.
```

## When to use this

- Whenever you've been restarted via `--continue` — the restart reason
  includes tm_update_manager, tm_restart, a crash-recovery loop, and the
  user manually exiting and re-launching `claude-tm`.
- Automatically after a `tm_restart(worker, fresh=true)` — the manager
  types `/sync-inbox` for you once the fresh child's empty prompt is
  ready.
- Specifically NOT on first session startup (no restart window to catch
  up on — no missed events possible).

## How to tell if you were restarted

Signals:
- Your conversation starts mid-thought, not at a fresh prompt.
- You see a previous turn's thinking/tool-calls in your context.
- You were told to `/sync-inbox` by the restart runbook.
- The manager auto-typed `/sync-inbox` for you as part of the fresh-
  restart sequence — the pty just showed `/sync-inbox` appearing at the
  prompt without you typing it.

First-start signals (no need for /sync-inbox):
- Your first turn is a greeting or an initial work order, with no
  prior conversation visible.

## Output

Tell the user (or the orchestrator via `reply`) what you found:

```
/sync-inbox: worker <name>, scanned N events, M inbound, K unhandled.
Processing unhandled:
- <event_id> at <ts>: <summary> → <action>
```

Or if all caught up:

```
/sync-inbox: worker <name>, all N recent events already accounted for.
No missed work.
```

## Why this exists

The claude-tm architecture has a small window where inbound events can
fall through the cracks: when the channel's SSE subscription is torn
down (restart, crash, network blip) and re-established, the hub has the
events on disk but the channel plugin doesn't replay them into the
conversation. The hub's persisted timeline is the authoritative "what
arrived" record. This command is how a restarted worker reconciles.

Cheap alternative to channel-side event replay — the reasoning happens
at the worker's Claude level, visible in the transcript, using context
`--continue` already provides (or, for `fresh=true` restarts, using
timeline read-back as the substitute for the missing context).

## Why not tm_my_inbox

`tm_my_inbox` was the original entry point and still exists — it
resolves the worker name from `TM_WORKER_NAME` in the HUB process's
environment. That worked when the hub and worker were colocated (early
dev, both launched from the same `claude-tm` session). It does NOT
work in the standard containerized deployment: the hub runs in a Docker
container whose env has no `TM_WORKER_NAME`, so the tool returns
"TM_WORKER_NAME not set" even when the worker session's env is
populated correctly. Reading the session env with Bash and calling
`tm_receive(worker=...)` explicitly is the identity-safe path and works
regardless of where the hub runs. See QM #555 for the transcript that
caught this on iris-qa-tm's fresh-restart e2e.
