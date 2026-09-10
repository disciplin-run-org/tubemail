---
name: sync-inbox
description: After a claude-tm restart, catch up on commands that arrived during the restart window. Resolves the worker's own name from its session environment (TM_WORKER_NAME), reads its timeline with tm_receive, and reconciles — stopping at the newest session-boundary marker. Invoked as `/sync-inbox fresh` after a fresh restart (no conversation context) and bare after a `--continue` restart.
---

# /sync-inbox — Catch up on commands that arrived during restart

You are starting (or resuming via `--continue`) after the claude-tm manager
re-exec'd python. The channel plugin's SSE subscription was briefly down
during that window. Any tubemail message that arrived in that gap is
persisted on the hub but was NOT delivered to your conversation as a
live channel event — so you need to pull it explicitly.

## Two modes — pick before you read anything

The reconcile rule depends on whether you have a conversation context to
reconcile *against*:

| Invoked as | Restart flavor | Rule |
|---|---|---|
| `/sync-inbox fresh` | fresh (no `--continue`) | Trust the timeline. You remember nothing; the session-boundary marker is your only evidence of what is settled. |
| `/sync-inbox` (bare) | `--continue` | Trust your context. Compare the timeline against what you can see yourself having done. |

The manager types the `fresh` argument for you on the fresh-restart path
(`_spawn_post_fresh_sync_inbox`). If the argument is absent but you also
have no prior conversation visible above this turn, treat it as **fresh** —
the guard matters more than the label.

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
   `channel/src/tubemail/manager.py`: `os.environ["TM_WORKER_NAME"]
   = session_name`).

   If the output is empty, skip to "Genuinely not a claude-tm worker"
   below.

2. **Read your own timeline via `tm_receive`.** Which read depends on the
   mode you picked above.

   **Fresh restart:**

   ```
   mcp__tubemail__tm_receive_since_boundary(worker="<name>", limit=20)
   ```

   This starts the window strictly after the newest session-boundary
   marker on your timeline. Everything above that marker belongs to a
   session that has ended and is settled by definition — you must not
   re-execute any of it. If no marker exists, the read degrades to the
   ordinary tail and step 3's fresh rule applies instead.

   **Use this tool, not `tm_receive(..., since_boundary=True)`.** The flag
   fails open: a session holding a stale tool schema drops the unknown
   kwarg, the call still succeeds, and you get the full tail while
   believing you got the boundary-scoped read — silently re-running work
   the previous session already finished. The dedicated tool cannot fail
   that way; if your schema is stale it errors with "unknown tool", which
   tells you to run `mcp__tubemail__refresh_tools` and retry.

   If you end up on the flag form anyway, verify it applied: **a
   boundary-scoped read never contains a `session_boundary` event.** If
   one appears in your results, the filter did not apply — refresh tools
   and re-read; do not treat that result as settled.

   **`--continue` restart:**

   ```
   mcp__tubemail__tm_receive(worker="<name>", limit=20)
   ```

   Either way you get a list of recent events on your own timeline, mixed
   inbound / outbound / permission_request / permission_response /
   interrupt / session_boundary.

   Do NOT call `tm_my_inbox` for this. That tool resolves identity from
   the HUB process's `os.environ`, which is empty in the standard
   containerized topology and returns a misleading "TM_WORKER_NAME not
   set" error even when your session's env is populated correctly.
   `tm_receive` with an explicit `worker=` argument is the identity-safe
   read.

3. **Decide what is unhandled.**

   **Fresh restart.** You have no context, so "did I already handle this?"
   is not a question you can answer — do not try. Two deterministic rules
   replace it:

   - Everything at or above the newest `session_boundary` event is
     settled. `tm_receive_since_boundary` already removed it; if you are
     reading a full timeline for any reason, stop scanning at that event.
   - **With no boundary marker anywhere on the timeline**, treat only the
     *trailing* inbound events as live: those after the newest event of
     any other kind (`outbound`, `permission_request`,
     `permission_response`, `interrupt`, `session_boundary`). An inbound
     with a later event of another kind after it was already being worked
     on by the session that is now gone.

   **`--continue` restart.** For each `kind=inbound` event:

   - Check your conversation context: did you see this message's text
     arrive and respond to it (outbound reply, tool calls that advanced
     the work, or ack)?
   - Look at the timeline: is there a matching `kind=outbound` from you
     right after this inbound, or a downstream `permission_request` that
     stems from the inbound's ask? Either confirms the inbound was
     handled.

4. **Process unhandled inbound events now.** For any inbound that survives
   step 3, treat it as a fresh work order arriving this turn. Run it per
   your normal channel-event handling.

5. **When in doubt, the two modes doubt differently.**

   **`--continue`: prefer false positives over false negatives.** If you
   can't tell whether you handled an event, treat it as unhandled and
   re-do it — re-doing a small ack or read is strictly better than
   dropping a work order. The exception: destructive or expensive
   operations (bulk deletes, model-fired LLM calls on the full spec) —
   for those, ask the orchestrator first via `reply` rather than
   re-executing blind.

   **Fresh: prefer asking over re-executing.** That rule inverts here.
   With no context you can confirm *nothing* was handled, so "when in
   doubt, re-do it" re-runs the entire timeline — exactly the accidental
   continuation a `/save-and-clear` was supposed to end. For anything
   ambiguous, `reply` to the orchestrator naming the events you are
   unsure about and wait, rather than executing them.

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
  types `/sync-inbox fresh` for you once the fresh child's empty prompt
  is ready.
- Specifically NOT on first session startup (no restart window to catch
  up on — no missed events possible).

## How to tell if you were restarted

Signals:
- Your conversation starts mid-thought, not at a fresh prompt.
- You see a previous turn's thinking/tool-calls in your context.
- You were told to `/sync-inbox` by the restart runbook.
- The manager auto-typed `/sync-inbox fresh` for you as part of the
  fresh-restart sequence — the pty just showed the command appearing at
  the prompt without you typing it.

First-start signals (no need for /sync-inbox):
- Your first turn is a greeting or an initial work order, with no
  prior conversation visible.

## Output

Tell the user (or the orchestrator via `reply`) what you found:

```
/sync-inbox (fresh): worker <name>, boundary <event_id> at <ts>,
scanned N events after it, K unhandled.
Processing unhandled:
- <event_id> at <ts>: <summary> → <action>
```

Or if all caught up:

```
/sync-inbox (fresh): worker <name>, nothing after the session boundary.
Clean slate — no missed work.
```

## Session boundaries

A session-boundary verb (`/save-and-clear`, `/save-and-exit`,
`/rollover`) posts a marker on the worker's own timeline just before the
session it belongs to goes away:

```
mcp__tubemail__tm_session_boundary(worker="<name>", reason="/save-and-clear")
```

That marker is a fact the successor can act on where an inference cannot
be trusted. A fresh session cannot pair an inbound with the outbound that
answered it — many work orders are answered with code and commits, not a
channel reply, so the pairing under-reports badly; and it has no context
of its own to check against, so "I can't confirm I handled it" is true of
literally everything. Without the marker, a `/save-and-clear` meaning
"new task, clean slate" replays the previous session's finished orders.

Notes for anything posting a marker:

- `tm_session_boundary` records `kind="session_boundary"` and is **not**
  delivered to the worker's channel. Do not post the marker with
  `tm_send` — that reaches the still-running session as a live work
  order saying its own work is settled.
- Read it back with `tm_receive_since_boundary`, never with
  `tm_receive(..., since_boundary=True)`. A stale client strips an
  unknown kwarg and the call still succeeds; a missing tool errors.
- Post the marker BEFORE any resume/self-message the successor must still
  act on (e.g. `/rollover`'s pre-posted `/resume-from-clear` order).
  Anything above the newest marker is invisible to a fresh start.
- The legacy shape — a `tm_send` whose body starts with
  `SESSION-BOUNDARY` — is still recognised, so timelines written before
  the event kind existed keep working. New callers should use the tool.

## Why this exists

The claude-tm architecture has a small window where inbound events can
fall through the cracks: when the channel's SSE subscription is torn
down (restart, crash, network blip) and re-established, the hub has the
events on disk but the channel plugin doesn't replay them into the
conversation. The hub's persisted timeline is the authoritative "what
arrived" record. This command is how a restarted worker reconciles.

Cheap alternative to channel-side event replay — the reasoning happens
at the worker's Claude level, visible in the transcript, using context
`--continue` already provides (or, for `fresh=true` restarts, using the
boundary-scoped timeline read as the substitute for the missing context).

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
