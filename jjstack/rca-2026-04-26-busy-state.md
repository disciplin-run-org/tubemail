# RCA — quartermaster-tm shows `busy` when actually idle

Date: 2026-04-26
Investigator: Claude (auto mode, /investigate)
Repo: tubemail
Branch: main

## PROBLEM SCOPE

- **IS:** `quartermaster-tm` displays as `busy` in tubemail's roster /
  `tm_status` even though the worker has no in-flight work — the user
  observes Claude sitting idle in that session. Same pattern flagged
  in code comments for `jjstack-tm` on 2026-04-25.
- **IS NOT:** Not a hung worker (the manager is alive, posting
  context-pct heartbeats — 28 in 30 min). Not the `waiting_permission`
  state (no pending permissions). Not "the worker hasn't been seen for
  hours" (last_activity is fresh).
- **STARTED:** Whenever a work order is delivered. The user notices
  it whenever they look at the roster between "work order arrived" and
  "10 minutes after work order arrived."

## What the algorithm does (answer to the user's first question)

`WorkerState.status_state()` in `src/tubemail_hub/bridge/models.py:102`.

```
if pending_permissions:           → "waiting_permission"
elif trailing event is `inbound`:
    if (now - inbound.ts) > BUSY_DECAY_S (600 s):  → "idle"
    else:                                          → "busy"
else:                             → "idle"
```

So: **the only signal for "busy" is "the most recent timeline event is
an inbound, and it's less than 10 minutes old."** No CPU check, no
"is the worker actively generating tokens" check. The hub literally
cannot tell apart "deep in tool call" from "completed and went home"
by event kind alone.

## FAILURE: false-positive busy after silent completion

Branch type: **C — intentional behavior + UX bug.** The algorithm is
working as designed; the design doesn't have enough signal.

**ACTION**
- claim: A work-order inbound arrived for `quartermaster-tm`. The
  worker processed it (writing code, running tests) without ever
  calling `mcp__tubemail-channel__reply` — most code-worker tasks
  finish that way.
- evidence: `tm_receive worker=quartermaster-tm` returned 5 events,
  all `kind=inbound`, zero `outbound`. Latest inbound at
  `ts=1777252960.32`. No outbounds in the entire timeline.
- confidence: verified.

**CONDITION**
- claim: The decay window (`BUSY_DECAY_S = 600 s`) is the worker's only
  way back to idle when no outbound is emitted, but the window is
  longer than the actual work duration. So the user sees `busy` for
  up to 10 minutes after the worker actually finished.
- evidence: `models.py:22` defines `BUSY_DECAY_S = 600.0` with a
  comment that explicitly names this trade-off. Live test:
  `now=1777253998 - inbound.ts=1777252960 = 1038 s` → past decay,
  state is now `idle`. Earlier in the window (e.g. 5 min after the
  inbound) it was `busy` even though the worker had stopped.
- confidence: verified.

**Why the simpler "look at last_activity" fix wasn't enough on its own**
- `last_activity` is bumped on every event AND on context-pct POSTs
  (`engine.update_context_pct` line 287). Workers running the
  post-2026-04 manager push context-pct whenever Claude burns tokens.
  Pre-fix, the algorithm ignored last_activity entirely; it used only
  the trailing event's timestamp.

## STOP CHECK — fix landed

Two layered windows in `status_state()`:

1. `BUSY_DECAY_S = 600 s` — absolute cap (unchanged).
2. `BUSY_QUIET_S = 60 s` — applies when the worker HAS produced
   post-inbound activity (`last_activity > inbound.ts`). When the
   activity stops for 60 s, decay early.

Workers running the legacy manager (no context-pct) keep the 10-min
cap. Workers running the new manager flip to idle within ~60 s of
truly going quiet — even when the original inbound is well inside the
old 10-min window.

### Regression test (lives in `tests/test_bridge_engine.py`)

Three new tests covering the class:

```python
test_status_state_decays_quiet_worker_with_post_inbound_activity
test_status_state_busy_while_post_inbound_activity_is_fresh
test_status_state_legacy_manager_no_post_inbound_signal
```

Plus the existing `test_status_state_decays_old_inbound_to_idle` still
passes — the 10-min cap is intact.

### Class boundary

The failure class is **"a worker's busy/idle classification cannot
distinguish 'still actively doing work' from 'finished silently'."**
Tests at this boundary cover three combinations:

- inbound + post-activity + post-activity is fresh → busy
- inbound + post-activity + post-activity is stale → idle (NEW)
- inbound + no post-activity + still inside 10-min cap → busy
- inbound + no post-activity + past 10-min cap → idle (existing)

If a future maintainer regresses any branch of this matrix, the test
suite catches it.

## Smallest user action (for THIS incident, no code change needed)

If you observed it again before the fix deploys: just wait. After
~10 minutes since the most recent inbound, the trailing-inbound rule
decays the state to idle on its own.

## Status

DONE — two passes.

### Pass 1 (heuristic)

`models.py`: BUSY_QUIET_S (60s) decay applied when last_activity has
advanced past inbound.ts. 3 regression tests. 144 hub tests pass.

### Pass 2 (authoritative manager-pushed signal — user request)

User pointed out: "I can see in the console the worker is idle, this
means the manager wrapper should be able to see that as well." Right.
The manager has direct line of sight to the pty; the hub does not.
Heuristics on event timestamps will always be wrong some of the time.

Added an end-to-end "manager observes claude's TUI and pushes is_active
to the hub" path:

- `channel/src/tubemail/manager.py` — new `_is_actively_processing(tail)`
  detector matches claude's spinner labels and the running-timer
  parenthetical. The existing `_context_pct_loop` now derives both
  signals from one screen scan and POSTs each independently when its
  value changes.
- `src/tubemail_hub/bridge/http.py` — new `POST /tubemail/<worker>/active`
  endpoint accepts `{"is_active": bool}`.
- `src/tubemail_hub/bridge/engine.py` — `update_active_state(name, bool)`
  records on `WorkerState.observed_active{,_at}`, fires a global
  `active_state` SSE event on flip.
- `src/tubemail_hub/bridge/models.py` — `WorkerState.observed_active`
  + `observed_active_at` fields. `OBSERVED_ACTIVE_FRESHNESS_S = 30s`.
  `status_state()` returns the observed value when fresh, falls back
  to BUSY_QUIET_S / BUSY_DECAY_S decay when the manager is silent
  longer than 30s (covers manager-disconnect / dead-manager / legacy-
  manager cases).

State precedence (highest to lowest):
1. `pending_permissions` → `waiting_permission`
2. fresh `observed_active` from manager → `busy` / `idle` (NEW)
3. trailing inbound + `last_activity` advanced + quiet > 60s → `idle`
4. trailing inbound + within 10 min → `busy`
5. trailing inbound + ≥10 min old → `idle`
6. trailing event ≠ inbound (or no events) → `idle`

5 new regression tests across hub + channel. 146 hub + 79 channel =
225 tests pass.
