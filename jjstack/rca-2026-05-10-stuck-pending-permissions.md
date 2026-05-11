# RCA — Stuck `pending_permissions` entries for active workers

**Date:** 2026-05-10
**Status:** Fixed (hub side). Hook-side gap surfaced for jjstack.
**Reported by:** Jesper via /investigate
**Related ADR:** `adr-2026-05-09-permission-resolution-durability.md`

---

## PROBLEM SCOPE

### IS
- `tm_pending_permissions(worker="tubemail-tm")` returns an entry the
  worker has long since resolved locally — the tool ran successfully,
  the LLM moved on, but the hub still reports `state="waiting_permission"`.
- Verified concrete case (2026-05-10): request_id `iaptp`,
  tool=`Bash`, command=`curl -s -o /dev/null -w "%{http_code}"
  http://localhost:8004/health`. Persisted ~7 minutes despite the
  worker producing a `stop_relay` outbound 94s after the request.
- Reproduces on every TM worker that goes through the local-auto-approve
  path (`auto-approve-safe.sh` returning `allow` without round-tripping
  a `permission` notification through the channel).
- Hub event timeline contains the `permission_request` event but NO
  matching `permission_response` event — so the resolution truly
  never reached the hub.

### IS NOT
- Not a hub sweeper heuristic bug: `tm_sweep_stale_permissions(worker)`
  cleared the entry instantly. The "any outbound after the request
  proves resolution" rule is correct.
- Not the channel durability path that ADR 2026-05-09 fixed: that path
  retries + spools when the channel sends a `permission_response`. In
  this scenario the channel never tries to send one.
- Not the same shape as the `leanspecs-code-tm` 34-hour incident
  (which was a hub blip swallowing a real POST). This one is the
  resolution never being POSTed at all.
- Not specific to the VPN/Tailscale work — orthogonal. Confirmed by
  reproducing on a localhost-only setup before any VPN involvement.

### STARTED
- Discoverable since the auto-approve hook went TM-aware. The
  `tubemail-hook-<worker>.sock` infrastructure landed earlier
  (per `channel/src/tubemail/permission_bridge.py` docstring) but
  the corresponding hook-script dispatch branch was never added.
- The most recent reproduction in this session: 2026-05-11 04:35:48 UTC
  (event_id `3fstk9dhn3` on worker `tubemail-tm`).

---

## FAILURE

`pending_permissions[tubemail-tm]` retains an entry indefinitely after
the local resolution.

### Branch A — Action

Claude Code in a TM worker session needs to run a `Bash` tool
(`curl -s -o /dev/null -w "%{http_code}" http://localhost:8004/health`)
that isn't in `settings.json` `permissions.allow`. The harness fires
the `PermissionRequest` hook (`~/.claude/hooks/auto-approve-safe.sh`,
symlinked to `~/.claude/skills/jjstack/hooks/auto-approve-safe.sh`).

- **claim:** the hook executes locally and decides "allow" via Haiku LOW
  rating, returning the hook-format `{"hookSpecificOutput": {...,
  decision: {behavior: "allow"}}}`. Claude Code consumes that decision,
  allows the tool, and runs the curl.
- **evidence:** the curl returned exit 0 with body `200`; no permission
  prompt appeared at the worker's terminal; the worker emitted a
  `stop_relay` outbound 94s later (event_id `06ue8jjjfy`).
- **confidence:** high.

### Branch B — Condition

The auto-approve hook script has a QM dispatch branch (delegates to
`/tmp/qm-hook-${QM_WORKER_NAME}.sock`) but **no symmetric TM dispatch
branch**. The channel's `HookServer` listens on
`/tmp/tubemail-hook-${TM_WORKER_NAME}.sock` and is the only piece of
code that knows the channel's pending `request_id` for a tool call.
Without a script call to that socket, the local "allow" decision is
never paired with the `request_id` and no `permission_response` POST
is ever sent to the hub.

- **claim:** `~/.claude/skills/jjstack/hooks/auto-approve-safe.sh` has
  only the `QM_WORKER_NAME` dispatch block (lines 31-95 in the
  inspected file), no `TM_WORKER_NAME` equivalent. The TM hook socket
  exists on disk (`/tmp/tubemail-hook-tubemail-tm.sock`,
  owner=jesper, mode=srw-------) but nothing writes to it.
- **evidence:**
  - `grep -rn '/tmp/tubemail-hook' /home/jesper/.claude/{hooks,skills}` —
    only the channel's own permission_bridge.py docstring matches; no
    hook script references this path.
  - `grep -n 'TM_WORKER_NAME\\|tubemail-hook-' /home/jesper/.claude/hooks/auto-approve-safe.sh`
    returns no matches.
  - The channel's `HookServer.start()` creates the socket but is dead
    code from the caller side.
- **confidence:** high.

### Branch C — Hub-side defense-in-depth gap (AND-branch)

Even with the upstream hook gap, the hub could have healed itself.
The sweeper (`engine._sweep_stale_for_worker`) already encodes "an
outbound after the request proves resolution" and is invoked at
engine startup and on demand via `tm_sweep_stale_permissions`. But:

- **claim:** the sweeper has no continuous / event-driven trigger.
  Stuck entries persist until either a hub restart or a manual admin
  call. The web UI keeps showing a stale `pending_permissions` row
  for the full gap window.
- **evidence:** `grep -n '_sweep_stale_for_worker\\|sweep_stale_permissions'
  src/tubemail_hub/bridge/engine.py` — only two trigger sites:
  `_sweep_stale_permissions_on_load` (called from `__init__`) and the
  `async sweep_stale_permissions{,_all}` wrappers (called by the MCP
  tool). No inline trigger inside any `record_*` method.
- **confidence:** high.

---

## STOP CHECK

### Class of failure

"A locally-resolved permission whose resolution never round-trips to
the hub leaves a `pending_permissions` entry visible for the full
window between hub restarts." This class covers:

- The TM hook-script gap (this incident).
- Any future hook integration that approves a tool without notifying
  the channel.
- Any transport failure that drops the `permission` notification AFTER
  the channel's durable POST already succeeded but before the hub
  records it.
- Any third-party plugin that resolves a permission locally.

The defining shape: the hub sees a `permission_request` event, then
later sees a worker outbound past the grace window, but the
`pending_permissions` list still contains the request.

### Regression test (written and shipped)

`tests/test_bridge_engine.py::test_outbound_auto_sweeps_proven_resolved_pending`:

```python
async def test_outbound_auto_sweeps_proven_resolved_pending(engine):
    """Record an outbound that postdates a stuck request by more than
    the grace window. Pending must clear with no admin call."""
    await engine.register_worker("w", "/")
    payload = PermissionRequestPayload(request_id="stuck", tool_name="Bash")
    await engine.record_permission_request("w", payload)
    ws = engine._workers["w"]
    for ev in ws.events:
        if ev.kind == "permission_request":
            ev.ts -= 3600
    assert len(ws.pending_permissions) == 1

    await engine.record_outbound("w", "i moved on")

    assert ws.pending_permissions == []
```

Plus three companion tests pinning the grace window, the no-op path,
and disk persistence — see `test_outbound_auto_sweep_respects_grace_window`,
`test_outbound_auto_sweep_no_op_when_no_pending`, and
`test_outbound_auto_sweep_persists_to_disk` in the same file.

If a future change disables auto-sweep on outbound, the first test
turns red regardless of which transport-layer mechanism failed —
exactly the class boundary we want.

---

## Fix shipped

**Hub side (this repo, commit pending):**

`src/tubemail_hub/bridge/engine.py` — `record_outbound` now calls
`_sweep_stale_for_worker(worker, now=event.ts, persist=False)` when
`pending_permissions` is non-empty. The new `persist=False` parameter
on `_sweep_stale_for_worker` avoids double-persisting the worker file
since `record_outbound` is about to persist anyway. The grace window
(`_SWEEP_GRACE_S = 60.0`) still protects fresh requests from being
evicted by a parallel reply.

Effect: any worker that emits any outbound event past the grace
window heals its own stuck `pending_permissions` entries in O(events)
without operator action. The web UI reflects reality within one
outbound event of the worker resuming activity.

**Hook side (jjstack, surfaced for follow-up):**

`~/.claude/skills/jjstack/hooks/auto-approve-safe.sh` needs a TM
dispatch branch mirroring the existing QM block (script lines 31-95).
Suggested shape:

```bash
if [ -n "$TM_WORKER_NAME" ]; then
  TM_HOOK_SOCK="/tmp/tubemail-hook-${TM_WORKER_NAME}.sock"
  if [ -S "$TM_HOOK_SOCK" ]; then
    # read-only short-circuit, then python socket dispatch — identical
    # to the QM block but pointing at $TM_HOOK_SOCK.
    ...
  fi
fi
```

This makes auto-approval atomic with the hub-side resolve. With it,
the hub-side sweeper is purely defensive — the system stays clean
without ever firing.

Not landed in this RCA because the hook lives outside the tubemail
repo. Tracked as a follow-up for the jjstack repo.

---

## How to verify after fix

- Reproduce the original symptom: any TM worker runs an auto-approved
  Bash command whose pattern isn't in `settings.json`. Confirm
  `tm_pending_permissions(worker)` initially shows an entry.
- Wait 60s, then trigger any outbound from the worker (any reply or
  Stop relay). Confirm `tm_pending_permissions(worker)` is empty
  WITHOUT calling `tm_sweep_stale_permissions`.
- Confirm `tm_status(worker)` returns `state=idle` / `state=busy`,
  not `waiting_permission`.

---

## Verified contributing factors recap

| Node | Claim | Evidence | Confidence |
|---|---|---|---|
| Action | `auto-approve-safe.sh` returns "allow" locally for the curl | curl ran, no prompt, outbound followed | high |
| Condition | Hook script has no TM dispatch branch | `grep TM_WORKER_NAME` in script: empty | high |
| Hub gap (AND) | Sweeper never triggers on incoming events | `grep _sweep_stale_for_worker`: only `__init__` + admin tool | high |

All three are needed for the failure mode to be observable: drop any
one and the symptom goes away. The hub-side fix invalidates the third;
the jjstack fix invalidates the second. Either alone closes the class
boundary.
