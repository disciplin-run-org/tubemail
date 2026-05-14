# ADR — Permission-resolution durability and stuck-state self-healing

**Date:** 2026-05-09
**Status:** Accepted, implemented in commits `0a485ca` (hub) + `788cbcd` (channel).
**Supersedes:** the previous fire-and-forget `post_permission_response` path.
**Related:**
- `rca-2026-05-07-missing-events.md` — the worker-history-loss bug; same shape (in-memory state diverging from disk after a hub blip).
- Stop-hook durability under QM #205 — same pattern at a different layer.

This document captures invariants the system MUST preserve, even after a
full rewrite. If you rebuild TubeMail from scratch and want it to be
production-trustworthy, every section under "Invariants" below has to
land in the new build — the heuristics and the file layout can change,
but the guarantees cannot.

---

## 1. Problem this solves

When a worker's Claude Code session asks to use a tool that requires
approval, the channel plugin emits a `permission_request` event the hub
records under `WorkerState.pending_permissions`. The expected lifecycle:

1. Worker's LLM calls a tool that needs approval.
2. Channel posts `permission_request` to the hub. Hub records it under
   `pending_permissions[worker]` and emits an SSE event for the UI.
3. Either the orchestrator calls `tm_respond_permission`, or the user
   answers at the worker's terminal, or an auto-approve hook fires.
4. The resolution travels back to the hub as a `permission_response`
   event. Hub removes the entry from `pending_permissions[worker]`.

**The bug:** step 4 is fragile. The path that ships the local resolution
back to the hub (`HubClient.post_permission_response`) was a single
fire-and-forget POST with `try/except: log; drop`. If the hub blipped
at exactly that moment, the resolution vanished. The LLM proceeded —
the worker kept producing outbound events — but the hub kept reporting
`state="waiting_permission"` indefinitely.

**Observed in the wild (2026-05-09):**
`leanspecs-code-tm` had two pending entries (`hcnes`, `vnniv`) stuck
for 34 hours despite the worker actively posting `stop_relay` outbounds
the entire time. Both requests landed within 21 seconds of each other,
right after a different request (`zrcne`) was successfully resolved —
suggestive of a brief hub blip that swallowed only the two trailing
resolutions.

**Why this is worse than a missed event in general:** the hub state
drives operator decisions. A worker reporting `waiting_permission`
forever:
- Confuses the orchestrator's dispatch loop (Quartermaster won't
  dispatch new work to a worker it thinks is blocked).
- Misleads humans triaging via `tm_status` / the web UI roster.
- Triggers spurious "this worker is hung" investigations.
- The worker itself has no way to fix this — it doesn't know the hub's
  view of its state.

---

## 2. Two-layer fix

A single layer was rejected because each fix has independent failure
modes:

- **Channel-side durability alone** (retry+spool) prevents the bug going
  forward but does nothing about state already stuck on the hub from
  pre-fix sessions. A worker that ran for two days under the old code
  would still need its disk state cleaned up.
- **Hub-side sweeper alone** cleans up after the fact but burns
  state-divergence cycles every time — the LLM thinks it answered, the
  UI shows pending until the next sweep tick. Operators see the lie in
  between.

So both layers ship. They are independent and each one alone reduces the
incident rate; together they make the failure class non-observable.

```
worker LLM resolves permission
         │
         ▼
  channel.notification handler
         │
         ▼
  post_permission_response_durable
   ├─► hub POST: try → retry x3 with 0.5/2/5s backoff
   │         └─► success: hub clears pending entry
   │
   └─► all retries failed
            ▼
       spool to disk: ~/.claude/tubemail-spool/<worker>/permission-<ts>-<rid>.json
            ▼
       drain on next channel startup OR next successful POST
            ▼
       hub clears pending entry (eventually)

independently, on the hub:
   engine.__init__ → _load_all → _sweep_stale_permissions_on_load
                                      │
                                      ▼
                          for each worker, drop pending entries
                          where any subsequent worker outbound
                          event proves the resolution happened
```

---

## 3. Invariants — must preserve in any rebuild

### 3.1 The hub's `pending_permissions` MUST self-heal from divergent state

Premise: any transport between the worker and the hub can drop a
permission_response. The hub MUST be able to detect "this entry is
stuck" from purely local information (its own event log + the
pending_permissions list) and drop it.

The detection rule that works:

> A pending_permission entry whose corresponding `permission_request`
> event in the timeline is followed by **any worker outbound event with
> kind in `{outbound, permission_response}`** is provably resolved.

Reasoning: the worker's LLM cannot produce output past a permission
gate while still blocked on it. Any outbound event observed AFTER the
request's timestamp proves the LLM passed the gate, which means the
permission was resolved locally.

`interrupt` events do NOT qualify — interrupts go TO the worker (to
break it out of a gate), not from it (proof of progress past one).

### 3.2 The sweep MUST respect a grace window

A request received microseconds before a `stop_relay` outbound from a
parallel turn could be wrongly evicted by a too-eager sweep. Every
sweep run checks that the request is older than `_SWEEP_GRACE_S = 60s`
before considering it sweepable. The current value is conservative —
permission prompts that genuinely need an answer sit pending for many
minutes.

### 3.3 Orphan entries (no matching event) MUST be droppable

A pending_permission whose `permission_request` event is missing from
the timeline (state corruption, an edited JSON, schema drift) becomes
unanswerable: nothing in the hub's event stream can match it, so even
a real `tm_respond_permission` call would fail to find it. Orphans get
dropped after the same grace window since `last_activity` — fresh
enough that a race might still resolve them, otherwise unanswerable
forever.

### 3.4 The sweeper MUST run at hub startup

Without this, every hub restart leaves the existing stuck entries in
place until something else triggers cleanup. The first `tm_status`
after a restart MUST already be clean.

### 3.5 The sweeper MUST be exposed as an admin tool

For when a blocked operator needs to force a sweep without restarting
the hub. Current name: `tm_sweep_stale_permissions(worker?)`. Must
return `{swept: {worker: count}, total: int}` so the caller can verify
something actually changed.

### 3.6 The channel MUST retry permission_response POSTs

Single-attempt fire-and-forget is the proximate cause of the entire
class. Minimum: 3 retries with backoff. Current schedule mirrors the
Stop hook (0.5s, 2s, 5s) so operators only have to remember one timing
table.

### 3.7 The channel MUST persist resolutions to a local spool on
final retry failure

The retry chain bounds the recovery window at ~7.5s. A hub outage
longer than that (manager-to-hub network down, hub container being
upgraded) means the retry chain runs out and we lose the resolution
without spooling. Spool to:
`~/.claude/tubemail-spool/<worker>/permission-<ts>-<rid>.json`

Same root as the Stop hook spool. Mode `0600`. Atomic via tmp+rename.
JSON shape:
```json
{"request_id": "<rid>", "behavior": "allow|deny", "spooled_at": "<ts>"}
```

### 3.8 The spool MUST be drained on channel startup AND on next
successful POST

A hub outage that outlives the worker's claude session must still
recover the moment the worker's next session starts. Drain order is
oldest-first so resolutions land in the order the user actually
answered them.

`drain_spool` MUST stop on the first failure mid-drain. Otherwise a
long outage burns the full retry chain on every entry, and the spool
cap kicks in and starts dropping older entries. Stop early; come back
later.

### 3.9 The spool MUST be capped per worker

A multi-day outage with many resolutions could otherwise fill the disk.
Current cap: 200 entries. Drop oldest with a logged WARNING when full.

### 3.10 Programmer-error exceptions MUST propagate

The retry+spool wrapper catches `httpx.HTTPError` and `OSError` only.
A `TypeError` or `AttributeError` is a bug in the call path and must
remain loud — silencing it would hide the kind of error that matters
most.

---

## 4. Components and where they live

| Concern | File | Symbol |
|---|---|---|
| Hub sweep heuristic | `src/tubemail_hub/bridge/engine.py` | `BridgeEngine._sweep_stale_for_worker`, `_sweep_stale_permissions_on_load`, `sweep_stale_permissions`, `sweep_stale_permissions_all` |
| Constants | same | `_PROOF_OF_RESUMED_KINDS = {"outbound", "permission_response"}`, `_SWEEP_GRACE_S = 60.0` |
| Admin tool | `src/tubemail_hub/tools/workers.py` | `tm_sweep_stale_permissions` |
| Wired into startup | `src/tubemail_hub/bridge/engine.py` | last line of `BridgeEngine.__init__` |
| Channel spool | `channel/src/tubemail/permission_durability.py` | `PermissionResponseSpool` |
| Channel durable wrapper | same | `post_permission_response_durable`, `drain_spool` |
| Channel call sites | `channel/src/tubemail/channel.py` | `_handle_unknown_notification` (both `permission_request` auto-resolve and `permission` user-resolve paths) |
| Drain at startup | `channel/src/tubemail/channel.py` | `_handle_initialized` |
| Spool root env | `channel/src/tubemail/permission_durability.py` | `SPOOL_ROOT_ENV = "TUBEMAIL_PERMISSION_SPOOL_DIR"` |

The spool layout (`~/.claude/tubemail-spool/<worker>/`) is shared with
the Stop hook by intent. One spool root, two file prefixes:
- `<ts>-<sha>.json` — Stop hook payloads
- `permission-<ts>-<rid>.json` — permission_response payloads

A future writer that wants to spool a third event class should follow
the same prefix-discriminated convention.

---

## 5. Test pinning — what each test guards

### Hub-side, in `tests/test_bridge_engine.py`:

| Test | Invariant guarded |
|---|---|
| `test_sweep_drops_proven_resolved_pending` | §3.1 |
| `test_sweep_keeps_genuinely_pending` | sweeper does NOT drop entries with no proof of resolution |
| `test_sweep_respects_grace_window` | §3.2 |
| `test_sweep_drops_orphan_after_grace` | §3.3 |
| `test_sweep_keeps_orphan_within_grace` | grace window protects new orphans from racing eviction |
| `test_sweep_all_only_reports_workers_that_changed` | response stays readable on a large fleet |
| `test_sweeper_runs_at_engine_construction` | §3.4 — the on-disk file is updated, not just memory |

### Hub admin tool, in `tests/test_mcp_tools.py`:

| Test | Invariant guarded |
|---|---|
| `test_sweep_stale_permissions_tool_reports_dropped_workers` | §3.5 — fleet-wide call shape |
| `test_sweep_stale_permissions_tool_scopes_to_one_worker` | per-worker call shape |

### Channel-side, in `channel/tests/test_permission_durability.py`:

| Test | Invariant guarded |
|---|---|
| `test_spool_root_honors_env_var` | §3.7 — env override for testing |
| `test_spool_root_isolates_per_worker` | per-worker spools cannot cross-contaminate |
| `test_spool_write_persists_payload_with_secure_mode` | §3.7 — mode 0600, payload shape |
| `test_spool_write_sanitizes_request_id_in_filename` | filesystem-traversal defense for malicious request_ids |
| `test_list_pending_returns_oldest_first` | §3.8 — drain order |
| `test_durable_post_returns_true_on_first_try` | happy path doesn't accidentally spool |
| `test_durable_post_retries_on_transient_failure` | §3.6 — retry count is pinned |
| `test_durable_post_spools_when_all_retries_fail` | §3.7 — spool fidelity |
| `test_durable_post_does_not_spool_unrelated_exceptions` | §3.10 |
| `test_drain_spool_forwards_in_order` | §3.8 — drain order on success |
| `test_drain_spool_stops_on_first_failure` | §3.8 — back-off discipline mid-drain |
| `test_drain_spool_skips_malformed_entries` | bad spool entries don't poison the drain |
| `test_drain_spool_skips_entries_with_invalid_behavior` | defense in depth on payload schema |

A rebuilder who passes equivalent tests under different names has met
the spec. A rebuilder who skips any of these is leaving a known sharp
edge on the floor.

---

## 6. Operational notes

### How to recover a stuck worker without code changes

If you discover a worker stuck in `waiting_permission` and the channel
or hub doesn't have the durability code yet (e.g. an old build), use:

```
mcp__tubemail__tm_sweep_stale_permissions(worker="<worker-name>")
```

(Once the new tool ships.) Or: edit the worker's JSON file directly —
e.g. `docker exec disciplin-run-tubemail-1 python -c "..."` — to drop the
stuck entry, then restart the hub container so in-memory state reloads
from disk.

### How to recover when the spool itself is wedged

Inspect `~/.claude/tubemail-spool/<worker>/permission-*.json` on the
host. Each file is human-readable JSON. To force a drain attempt,
restart the worker's claude-tm — the channel drains on init.

To abandon a wedged spool entry (e.g. the request_id is no longer
recognised by the hub because the worker re-registered and cleared its
pending list), `rm` the file. The channel will skip it on next drain.

### How to verify the system is healthy

```bash
# Across the fleet — total should be 0 unless a real human is waiting.
docker exec disciplin-run-tubemail-1 python -c "
import json, glob
total = 0
for path in glob.glob('/data/workers/*.json'):
    d = json.load(open(path))
    n = len(d.get('pending_permissions', []))
    if n: print(path.split('/')[-1], n)
    total += n
print('total:', total)
"
```

```bash
# Per-worker, via the new admin tool:
mcp__tubemail__tm_pending_permissions(worker="<name>")
mcp__tubemail__tm_status(worker="<name>")
```

If a worker shows `pending` but no human has asked for approval in the
last few minutes, the sweeper missed something — file an RCA. The
heuristic in §3.1 should catch every real case.

---

## 7. What can safely change in a rebuild

- The exact backoff schedule (currently 0.5/2/5). Anything in the same
  order of magnitude that retries at least three times is fine.
- The grace window (currently 60s). Should be at least 30s to absorb
  event-append races; longer is fine but delays recovery.
- The spool cap (currently 200). Lower bound: enough to absorb a
  realistic burst of resolutions during a believable outage. The Stop
  hook uses 1000; permission resolutions are rarer so 200 is
  comfortable.
- The spool path layout. Sharing one root with the Stop hook is
  intentional but not required.
- The exact `_PROOF_OF_RESUMED_KINDS` set, IFF the rebuilder also
  changes what kinds of events workers can produce. The semantic is
  "any event that proves the LLM ran past the gate" — preserve that.

## 8. What MUST NOT change

- The premise that the hub's view of a worker can be wrong and self-
  heals from local information.
- The premise that permission_response must be durable transport.
- The dual-layer architecture: removing either layer reintroduces the
  observed failure mode.
- The `try/except: log; drop` anti-pattern at the
  `post_permission_response` call site — this is exactly what got us
  here.

If a future maintainer suggests "let's simplify by removing the
sweeper now that we have the spool" or "let's drop the spool now that
we have the sweeper" — point them at this document. They have
independent failure modes (3.7's note covers the spool-side failure
that only the sweeper handles; the channel-side durability is what
prevents the bug from recurring per worker per outage).
