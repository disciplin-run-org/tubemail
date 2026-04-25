# Investigation — leanspecs-spec-tm "never heard back" from iris-qa-tm

**Date:** 2026-04-23 21:05
**Reporter:** leanspecs-spec-tm orchestrator
**Repo:** tubemail (transport layer)

## Ticket

> leanspecs-spec-tm sent a work order to iris-qa-tm via `tm_send`, waited
> three 55-second `tm_wait_for_activity` cycles (~2:45 total), all empty.
> `tm_status` on iris-qa-tm reported `state: idle`, `event_count: 1`,
> `last_activity` matching the inbound. No pending permissions.
>
> Meanwhile iris-qa-tm claims it ran the work order and replied with a full
> target_bug/setup_bug classification.

## Evidence collected

`tm_receive(worker="iris-qa-tm", limit=50)` run from the tubemail
diagnostic session, 2026-04-23 21:03:

| # | event_id     | ts                       | kind     | note                         |
|---|--------------|--------------------------|----------|------------------------------|
| 1 | xt8zbeq4vr   | 2026-04-23 20:30:13.478  | inbound  | work order from orchestrator |
| 2 | r6ebzfg5g7   | 2026-04-23 20:33:38.890  | outbound | iris-qa-tm's reply (full classification) |

**Delta: 205.4 seconds** between work-order arrival and reply emission.

`tm_status(worker="iris-qa-tm")` at 21:03:

```
state: idle
event_count: 2
last_activity: 1777001618.89008   # matches r6ebzfg5g7 ts
pending_count: 0
```

The outbound reply **did** land on iris-qa-tm's timeline. The transport
layer is not dropping messages.

## Two whys

**Why did leanspecs-spec-tm report `empty`?**
Because its three 55 s waits ended at t≈165 s. The reply arrived at t≈205 s.
The orchestrator stopped polling 40 s too early.

**Why did the orchestrator stop polling?**
Because `tm_status` reported `state: idle` and `event_count: 1`. That
matches "worker has nothing in flight" — so giving up was a reasonable
read of the signal. The orchestrator's mental model was correct; the
signal lied.

**Root cause (third why):** `WorkerState.status_state()` in
`src/tubemail_hub/bridge/models.py:74-78` never returns `"busy"`. It
returns `"waiting_permission"` if there is a pending permission, else
`"idle"` — always. The tool docstring (`tm_status`, line 267) and the
engine's state model advertise `busy` as a valid state, but no code
path emits it. The author even left a comment on the method: *"Busy
if last event was outbound within a short idle window — simplistic
heuristic"* — acknowledging the logic is incomplete.

Effect: while iris-qa-tm was spending 205 s running `iris_qa_run`,
`tm_status` reported `idle`, actively misleading orchestrators into
thinking no work was in flight.

## Classification

- **Primary**: tubemail bug — `status_state()` does not detect the
  obvious "inbound waiting for outbound reply" condition. The contract
  promised by the tool docstring (`busy`/`idle`/`waiting_permission`) is
  not met.
- **Secondary**: orchestrator policy — 3×55 s waits is insufficient for
  iris-qa runs (typical duration 2–5 min). Orchestrators should either
  wait longer, check `tm_health` (manager-level CPU), or loop on
  `tm_wait_for_activity` until a meaningful signal arrives.

The primary is worth fixing in tubemail because a correct `busy`
signal makes the orchestrator's job trivial: keep waiting while busy,
give up on idle.

## Proposed fix (minimal, deterministic)

In `src/tubemail_hub/bridge/models.py`:

```python
def status_state(self) -> str:
    if self.pending_permissions:
        return "waiting_permission"
    # Busy when the most recent timeline event is an unanswered inbound:
    # orchestrator handed the worker work and no reply has come back yet.
    if self.events and self.events[-1].kind == "inbound":
        return "busy"
    return "idle"
```

This is deterministic (no clock-window heuristic), matches the mental
model every orchestrator already has, and removes the failure mode that
triggered this investigation.

### Test coverage to add

In `tests/test_bridge_engine.py`:

1. After `enqueue_inbound`, assert `status_state() == "busy"`.
2. After `record_outbound` that follows an inbound, assert `status_state() == "idle"`.
3. `waiting_permission` continues to beat `busy` when both conditions hold.

## Recovery for the current stuck orchestrator

leanspecs-spec-tm can recover without any code change:

```
tm_receive(worker="iris-qa-tm", since="xt8zbeq4vr")
```

will return the outbound reply `r6ebzfg5g7` — the full classification
iris-qa-tm produced. No need to resend the work order.

## Not a bug

- The channel-plugin reply path is working (reply event is on the
  timeline).
- `events_since` / `wait_for_activity` work correctly (the reply would be
  returned by any call issued after t=205 s).
- `tm_send` correctly returns the inbound `event_id` for `since`-cursor
  use.

## Heal framework integration

This investigation should translate into three things that live in
the repo so we don't re-litigate:

1. **Fix** `status_state` as above.
2. **Tests** listed above.
3. **Heal check** (when a heal framework exists for tubemail): a
   smoke test that posts a synthetic inbound with no reply, then asserts
   `state == "busy"`, then posts the outbound and asserts `state == "idle"`.
   That guards the contract the tool docstring promises.
