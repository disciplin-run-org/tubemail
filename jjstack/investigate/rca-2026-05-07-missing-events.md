# RCA — quartermaster-tm event log loss (QM #187 / #207)

**Date:** 2026-05-07  
**Reporter:** quartermaster-tm (this session)  
**Method:** Verified Contributing-Factors Tree per `~/.claude/skills/jjstack/references/root-cause-analysis.md`

## Scope

| | |
|---|---|
| **IS** | `/data/tubemail/workers/quartermaster-tm.json` contains `events: [length 2]` despite `last_activity − registered_at ≈ 4h 43m` of active session time. The 2 events present are both from a 6-minute window (20:04–20:09 UTC); everything before that is gone. |
| **IS NOT** | Not a `purge_worker` call (audit log clean, no admin action). Not file corruption (the JSON parses, schema is current). Not the `_persist` tmp-file race (lock serialises writes per worker; concurrency test passes). Not `purge_stale_workers` (the worker has been active continuously, far below the 24h cutoff). |
| **STARTED** | Hub container restarted at ~15:43 UTC (uptime 5h at 20:43 observation; work order #187 dispatched 14:30 UTC pre-restart). The 2 surviving events are from 20:04 and 20:09 — over 4 hours after the hub came back. |

## Tree

### Level 0 — observed effect

> **EFFECT:** quartermaster-tm.json on disk has only the most recent 2 events.

### Level 1 — proximate write

> **CLAIM:** Some write path overwrote the on-disk file with a state object that contained only those 2 events.  
> **EVIDENCE:** The JSON was last modified at 20:09 (matches the most recent event ts). `_persist()` is the only writer, and it serialises the in-memory `WorkerState.events` list.  
> **CONFIDENCE:** high — the file MUST have been written by `_persist`, which writes whatever `_workers[name].events` happens to be at that moment.

### Level 2 — what was the in-memory state when that write fired?

> **CAUSE A (action):** A code path called `_persist(worker)` while `_workers[worker]` held a state with only 2 events.  
> **CAUSE B (condition):** The in-memory `_workers[worker]` was a *fresh* `WorkerState`, not the one loaded from disk by `_load_all` at hub startup. Combined with A, this means the freshly-created state, with empty events at first, then accumulated only the 2 post-creation events, was the one persisted.

Both causes must hold. A alone is benign (overwrite with the same data). B alone is benign (in-memory drift is harmless if not persisted). Together: every write becomes a destructive overwrite.

### Level 3a — what code paths create fresh in-memory state when the worker isn't in `_workers`?

Five sites in `bridge/engine.py`, each with the same shape:

```python
ws = self._workers.get(worker)
if ws is None:
    ws = WorkerState(name=worker, registered_at=time.time())
    self._workers[worker] = ws
```

| Line | Function | Reachable via |
|---|---|---|
| 158 | `register_worker` | POST /tubemail/{worker}/register |
| 393 | `enqueue_inbound` | tm_send (orchestrator) |
| 418 | `record_outbound` | POST /tubemail/{worker}/outbound (Stop hook + reply tool) |
| 447 | `record_permission_request` | POST /tubemail/{worker}/permission-request |
| 518 | `send_interrupt` | tm_interrupt |

**Evidence:** `git grep -n "ws = WorkerState(name=worker"` in `engine.py`. None of these consult disk before creating fresh.

### Level 3b — under what condition would `_workers[worker]` be missing for a worker that has prior disk state?

Three plausible triggers, each ending with the same mechanism:

> **B1:** Hub container restart with a Claude session still active. `_load_all` runs at engine `__init__`. If a write fires BEFORE `_load_all` finishes (or if the worker's file was somehow not enumerated by the glob, e.g. a brief filesystem race during restart), the write's `if ws is None:` branch fires and fresh state replaces disk.  
> **EVIDENCE:** Hub uptime 5h, worker active throughout, events present only post-restart. Suggestive.  
> **CONFIDENCE:** medium.

> **B2:** A future `purge_stale_workers` change (or a manual `_workers.pop(worker)` in a debug session) evicts in-memory state but leaves disk file intact. Next write hits the auto-create branch.  
> **EVIDENCE:** No git evidence of recent eviction code, but the door is open.  
> **CONFIDENCE:** low for THIS incident; high as a future regression risk.

> **B3:** `register_worker` itself: when a worker re-registers (and `_workers[name]` is empty for any of the reasons above), the function builds a fresh `WorkerState` and persists it. The disk file's events get blasted away in the same atomic write that records the re-registration. This is the smoking gun for the post-restart case: the SSE reconnect loop in `hub_client.py` calls `register()` on every reconnect, and a brief gap between `_load_all` not yet running and the first re-register call would persist empty state.  
> **EVIDENCE:** `register_worker` line 158-164 hits this branch. Re-register is unconditional in `hub_client.stream()` (line 124). Hub restart resets `_workers`, the very next register from a still-running worker fires the branch.  
> **CONFIDENCE:** high — this is the one.

## Class-boundary stop

I CAN write the regression test for the class of failure: "any code path that mutates a worker's state must consult the on-disk file before creating fresh state." Test shape: pop from `_workers`, leave disk file with N events, fire a write, assert all N+1 events present after.

That's the actionable root, so I stop here.

## Fix

`bridge/engine.py` gains two helpers:

- `_load_from_disk(name) -> WorkerState | None` — read and validate the worker's file; returns None on missing/corrupt.
- `_get_or_create_worker(name, *, defaults=None) -> WorkerState` — prefer cache → disk → fresh, in that order. Inserts into `_workers` either way.

All five auto-create call sites switch to `_get_or_create_worker`. `register_worker` still does its post-resolve mutations (cwd, last_activity, forwarder_version) on the resolved state.

## Tests pinning the class

`tests/test_event_durability.py`:

1. 50 concurrent `record_outbound` for the same worker → all 50 events end up on disk (lock-serialisation regression).
2. Seed a worker, evict from `_workers`, fire `record_outbound` → prior events preserved (the actual #187 mode).
3. Same shape for `register_worker` with prior history.
4. Same shape for `enqueue_inbound`.
5. Brand-new worker (no memory, no disk) still works — auto-create path is fine when there's nothing to preserve.
6. Corrupt disk JSON → graceful fallback to fresh, no crash.
7. Audit tool flags a worker with 2 events / 5h active; healthy and freshly-started workers are not flagged.

## Belt-and-suspenders: audit tool

`python -m tubemail_hub.tools.audit_workers` scans `/data/tubemail/workers/*.json` and flags any worker whose events-per-active-hour rate falls below threshold (default 1.0, with a 5-minute grace window for fresh sessions). Exit code 0 = clean, non-zero = anomaly. Suitable for cron after hub restarts.

## Out of scope (intentionally)

- The `_persist` tmp-file collision concern from the work order's "suspected mechanism" turned out to be a non-issue: `_lock` serialises all per-worker writes, and the regression test confirms 50 concurrent `record_outbound` calls don't race.
- Did NOT add a "schema-version" check on `_load_from_disk` — the existing `WorkerState.model_validate` already enforces the contract; if it raises we log and fall back, which is correct.
- Did NOT change `purge_worker` semantics — that's an explicit admin tool and is correct.
