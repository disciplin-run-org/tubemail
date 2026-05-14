# RCA — /health reported 65 workers + 4 pending when the user had 14 workers and nothing in flight

**Date:** 2026-04-24 16:28 local
**Reporter:** Jesper ("I have 14 workers, not 65. Nothing is using this at the moment.")
**Repo:** tubemail

## The claim that got me in trouble

Just before recommending the rebuild, I curled `/health` and relayed:

> 65 workers are connected to the current hub, 4 pending permissions are in flight right now.

Both numbers were wrong. Reality:

- **14 workers** actually online (SSE-connected forwarders).
- **0 pending permissions** actually awaiting a decision.

I then warned the user about shared-system impact I had fabricated from bad data. That's the failure I'm RCA-ing.

## The two whys

### First why — why did `/health` report 65 workers?

`server.py:95` does `worker_count = len(engine.list_workers())`. `list_workers()`
iterates `self._workers`, which is populated at startup by `_load_all()` —
everything ever registered whose `*.json` state file is still on disk.

Evidence:

```
$ docker exec tubemail-tubemail-hub-1 ls /data/tubemail/workers/ | wc -l
65
$ ls ./tubemail/data/tubemail/workers/ | wc -l
0   # the data-dir bind-mount isn't used by the current (pre-session) container
```

65 persisted files, 14 online forwarders. The per-row `online: false` flag is
correct on each row, but the **top-line count in /health doesn't
distinguish online from offline** — it's a historical registry count
labeled `worker_count`, which reads as "workers right now."

### Second why — why did `/health` report 4 pending permissions?

`server.py:96` does `pending = len(engine.list_pending_permissions())`. This
iterates every worker — online or not — and sums each worker's
`pending_permissions` list.

`register_worker` already clears pending on re-register (engine.py:103-104). But
if a worker crashed / got killed / went offline without coming back, its
`pending_permissions` list persists forever on disk. On hub restart those
entries load back in and remain counted.

So `pending: 4` was almost certainly four zombie permission prompts from
dead worker sessions. No human was waiting on them.

### Root cause, one level deeper

Both failures share a pattern: **persisted data is treated as live state**.
Every metric the hub reports about "what's happening now" is computed over
the all-time registry instead of the live subscription set. `is_online(name)`
already exists (engine.py:168) and correctly returns "has an active SSE
subscriber" — but nothing filters metrics through it.

That's structural, not a one-off. The same shape would bite any new
top-line metric we added to `/health` (e.g. "flows in progress", "pty
bridges attached") if we continued the pattern.

## Why I relayed the bad numbers as confident fact

A second failure, worth naming. I had agency to query the hub more
skeptically:

- I could have called `tm_list_workers()` via MCP — that also uses
  `is_online()`, so its output distinguishes online/offline rows. I didn't.
- I could have looked at `docker logs tubemail-tubemail-hub-1` to see
  recent forwarder activity. I didn't.
- I could have called `tm_pending_permissions()` — which iterates the
  persisted list but at least returns the actual entries, which I'd have
  seen were stale by their timestamps. I didn't.

Instead I took one `/health` curl at face value and built a warning
narrative on top ("65 workers are connected… 4 pending in flight…"). That
narrative anchored the user's expectations falsely, and the user had to
correct me.

## Permanent fix

Three changes to `/health` and two to the engine. They compose into the
rule: **live metrics report only on live state**.

### Engine changes

1. **`list_pending_permissions()` gains a `online_only: bool = False`
   argument.** When true, filter to workers where `is_online(name)` returns
   true. Keeps the existing all-time variant available for admin / debug
   tools that genuinely want it (e.g. "show me zombies").
2. **Clear pending permissions on subscriber close.** When a forwarder's
   SSE subscription closes without a re-register following (i.e. the
   worker truly went offline), drop the pending list for that worker. Any
   pending prompt that was awaiting a decision at the moment the worker
   died is not actionable anyway — the Claude session that generated it
   is gone. Today's code only clears on re-register, which leaves a long
   window of stale state.

### /health changes

3. **Replace `worker_count` with `workers_online` + `workers_total`.**
   `workers_online` is what humans care about. `workers_total` stays for
   disk / capacity monitoring.
4. **`pending_permissions` in /health uses the new `online_only=True`
   path.** Stale prompts from dead sessions no longer count.
5. **`safe_to_restart` derives from online-worker state only.** Today the
   logic is `not (is_busy and is_busy())` where `is_busy=lambda: pending > 0` —
   so any stale pending locks the flag to False forever. After (4), the
   flag is meaningful again.

### One-line behavioral guarantee

After the fix, a hub with no connected workers and no real in-flight work
reports:

```json
{
  "workers_online": 0,
  "pending_permissions": 0,
  "safe_to_restart": true
}
```

regardless of how many stale state files live on disk.

## Tests

Three regression tests to pin the behavior:

1. `test_health_workers_online_excludes_offline` — load a data-dir with
   5 state files on disk, register 2 of them (SSE-subscribe), assert
   `workers_online == 2` and `workers_total == 5`.
2. `test_health_pending_excludes_zombies` — plant a pending permission on
   a worker that never re-registers, assert `/health`'s
   `pending_permissions == 0`.
3. `test_safe_to_restart_ignores_stale_state` — same setup as (2),
   assert `safe_to_restart is True`.

## Process change — avoid confident numbers from a single source

New rule for myself: **when reporting live system state to the user,
cross-check at least two sources before giving a number.** For tubemail
that's `/health` + `tm_list_workers` MCP + `tm_pending_permissions` MCP.
Three sources that should agree; when they diverge, the diverging one
is the bug. Had I done that once today, I'd have caught the bug before
writing the warning.

## Not a bug — things that worked correctly

- `is_online()` on engine itself is correct. The bug is upstream of it,
  in the callers that bypass it.
- Per-row `online: false` on `list_workers()` output is correct — the
  UI would render those rows as red/grey, which is right.
- `register_worker` clears stale pending on re-register (engine.py:103).
  The gap is only for workers that never come back.
