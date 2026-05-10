# ADR — MCP reconnect architecture (worker self-rescue, hub bypass)

**Date:** 2026-05-09
**Status:** Accepted, implemented in commits `b63d78d` (hub
self-reconnect tool), `de70107` (customer slash command), `9247ab2`
(local Unix socket bypass), `a26b3b5` (VERSION resolver fix).
**Related:**
- `~/.claude/skills/jjstack/skills/reconnect-mcp/SKILL.md` — the LLM-facing
  decision skill that selects between the two tools below.
- The MCP-categories standard at `jjstack/20260428-mcp-server-categories.md`
  (constrains where each new tool may live).

This ADR captures the contract between the worker, the manager, and
the hub for **MCP reconnection** — what tool to call when, why two
tools exist, and what a rebuild MUST preserve.

---

## 1. Problem

A worker's Claude Code session loads multiple MCP servers (leanspecs,
iris-qa, quartermaster, tubemail, ...). Any one of them can drop:
network blip, container restart, server bug. When that happens, the
LLM stops being able to call tools from that server until /mcp's
"Reconnect" action runs against it.

Before this ADR, the only reliable reconnect path was a human at the
worker's terminal driving the /mcp dialog. Two failure modes plagued
us:

**Problem A:** the LLM didn't know to ask. The orchestrator-facing
`tm_reconnect_mcp(worker, server)` tool existed, but its name read
like an "operate on someone else's worker" verb. Worker sessions
forgot it could be self-invoked, and human operators kept getting
paged to drive `/mcp` manually.

**Problem B:** when the failed MCP was tubemail itself, the worker
couldn't use any `tm_*` tool to recover — the tool surface was gone.
The session became unrescuable from inside, and the tubemail
`tm_reconnect_mcp` was useless for its own outage.

---

## 2. Two tools, two transports

The fix is two reconnect tools that share the same return shape but
take different transport paths to the manager. The LLM picks based on
which MCP failed.

| Failed server | Tool | Transport |
|---|---|---|
| any non-tubemail (`leanspecs`, `iris-qa`, etc.) | `mcp__tubemail__tm_self_reconnect_mcp(server)` | Worker → tubemail hub HTTP → `<worker>-manager` SSE |
| `tubemail` itself | `mcp__tubemail-channel__reconnect_mcp(server)` | Worker → channel plugin → Unix socket → manager (no hub round-trip) |

Both tools return `{ok: bool, server: str, detail: str}`. Both end at
the same `_PtyChild.reconnect_mcp(server)` in the manager that drives
`/mcp` deterministically (numbered selection, no `enter` shortcuts that
could trigger Remote Control view).

The /mcp dialog driver itself is a separate concern — see
`channel/src/tubemail/manager.py:reconnect_mcp` — and is unchanged by
this ADR. What this ADR captures is the dispatch and transport layer.

---

## 3. Invariants — must preserve in any rebuild

### 3.1 Worker self-reconnect MUST be a parameterless choice

The LLM should be able to think "MCP X is dead, reconnect it" without
also having to thread "and my own worker name is Y." Without this,
the most common case (worker reconnects its own MCP) has the highest
chance of bug-by-fumbling: workers paste the wrong worker name, paste
their manager's name, paste a placeholder, etc.

`tm_self_reconnect_mcp(server)` resolves the worker identity from
`TM_WORKER_NAME` in the hub-process env (the same channel as
`tm_my_inbox`). The LLM passes only the server name. The original
`tm_reconnect_mcp(worker, server)` stays for orchestrator-on-other-
worker calls.

### 3.2 The reconnect path MUST work when tubemail itself is the failed MCP

Any reconnect path that depends on tubemail to function will fail
exactly when it's needed most. The channel plugin therefore must have
an out-of-band path to the manager that does not traverse the hub.

The chosen mechanism is a Unix-domain socket the manager opens at
startup. The path is exported via `TUBEMAIL_LOCAL_SOCK` env var and
inherited by the channel plugin through the pty child.

A different IPC (named pipe, shared memory, signal-based) would
satisfy this invariant. What MUST NOT happen is removing the
out-of-band path on the grounds that "the channel can usually reach
the hub anyway."

### 3.3 The local socket MUST be owner-only

`/tmp/tubemail-<session>.sock` is in a world-readable directory. The
socket file mode MUST be `0600` immediately after bind so a hostile
local user cannot connect. Mirrors how `_pidfile_path` and
`_logfile_path` already work in `manager.py`.

### 3.4 The wire protocol MUST be self-bounded

A malformed or hostile peer must not be able to exhaust the manager's
memory by streaming an unterminated request. Every read is capped at
`_MAX_REQUEST_BYTES = 64 KB`; reads beyond that drop the connection
with a logged warning.

### 3.5 The local socket MUST be a feature flag, not a hard requirement

Older managers don't have the socket. Channel-side code MUST detect a
missing/unreachable socket and return a structured `{ok: false, ...}`
pointing the LLM at `tm_self_reconnect_mcp` instead of raising. Failing
loudly here would break the worker even when the hub-routed path
would have succeeded.

### 3.6 The hub-routed reconnect MUST run as a `task=True` MCP tool

The dialog driver does up to ~25s of screen polling. A regular MCP
call would race Claude Code's 60s tool timeout if hub latency added
even a few seconds. `task=True` returns a task ID immediately, polls
in the background, and surfaces the final result via the standard
MCP task protocol.

### 3.7 The reconnect tool MUST NOT auto-fire on tool failures

We considered making the channel auto-reconnect when an MCP tool call
returned the canonical "server disconnected" error. Rejected:
auto-firing from a layer the LLM can't see introduces a hidden control
loop. Every other tool in the channel is explicit-on-LLM-action; the
reconnect tools follow that pattern.

If you want fire-and-forget reconnect later, surface it as a separate
opt-in setting, not a default.

### 3.8 Customer onboarding MUST ship a discoverable slash command

The Anthropic plugin spec auto-loads `commands/*.md` as slash
commands. The channel plugin ships `/reconnect-mcp` so any customer
who installs the plugin gets the LLM trained on when to call which
tool, without needing to edit their CLAUDE.md.

If a future plugin runtime drops the auto-load convention, the
equivalent hook must exist or every fresh install loses this
discoverability win.

---

## 4. Components and where they live

| Concern | File | Symbol |
|---|---|---|
| Hub self-reconnect tool | `src/tubemail_hub/tools/workers.py` | `tm_self_reconnect_mcp` |
| Hub orchestrator reconnect | same | `tm_reconnect_mcp` (pre-existing, unchanged) |
| Channel reconnect tool | `channel/src/tubemail/channel.py` | `reconnect_mcp` in `_handle_tools_list` + `_handle_tools_call` + `_do_reconnect_mcp` |
| Local IPC primitives | `channel/src/tubemail/local_ipc.py` | `LocalIPCServer`, async `request()`, `LocalIPCError`, `default_sock_path`, `SOCK_ENV` |
| Manager IPC wiring | `channel/src/tubemail/manager.py` | `_set_ipc_child`, `_handle_local_ipc`, IPC-server lifecycle in `run()` |
| Dialog driver | `channel/src/tubemail/manager.py` | `_PtyChild.reconnect_mcp`, `_run_reconnect_mcp` |
| Customer slash command | `channel/commands/reconnect-mcp.md` | (whole file) |
| README doc | `channel/README.md` | "Slash commands" section |

---

## 5. Test pinning

### Hub-side, in `tests/test_mcp_tools.py`:
- `test_self_reconnect_mcp_errors_when_env_missing` — §3.1 graceful failure
- `test_self_reconnect_mcp_routes_to_caller_manager` — happy path

### Local IPC, in `channel/tests/test_local_ipc.py` (12 tests):
- mode 0600 on socket file — §3.3
- stale-socket recovery on startup
- request-line size cap — §3.4
- concurrent clients
- missing-socket → `LocalIPCError`
- handler timeout

### Channel reconnect tool, in `channel/tests/test_channel.py`:
- `test_tools_list_advertises_reconnect_mcp` — schema published
- `test_reconnect_mcp_returns_helpful_error_when_env_unset` — §3.5
- `test_reconnect_mcp_falls_back_to_hub_when_socket_unreachable` — §3.5
- `test_reconnect_mcp_routes_through_local_socket` — happy path
- `test_reconnect_mcp_rejects_missing_server_arg` — schema enforcement

---

## 6. Operational notes

### When you see "MCP X is unavailable" in a worker

The LLM should run the `/reconnect-mcp` slash command (which loads the
shipped skill), which dispatches:

```
# Common case: any MCP except tubemail
mcp__tubemail__tm_self_reconnect_mcp(server="leanspecs")

# Tubemail itself died
mcp__tubemail-channel__reconnect_mcp(server="tubemail")
```

### When neither tool works

- `tm_self_reconnect_mcp` failing means tubemail is also down. Try the
  channel-side tool.
- The channel-side tool failing with `TUBEMAIL_LOCAL_SOCK not set`
  means the manager is older than this feature. Restart the worker's
  claude-tm wrapper (manager bounce) to pick up the IPC server.
- The channel-side tool failing with `local IPC failed` means the
  manager is up but the socket bind failed. Check `/tmp/tubemail-*.sock`
  perms and look at `/tmp/claude-tm-<session>.log` for the manager's
  error.

### When a reconnect "succeeds" but the tool surface is still broken

The reconnect path tells the MCP client to re-handshake. If the failure
was upstream (server's container is down, OAuth handshake broken, the
server isn't in `.mcp.json` at all), the reconnect succeeds at the
client layer but the tool surface stays empty. Fix the upstream cause
first.

---

## 7. What can safely change in a rebuild

- Switch the channel↔manager IPC from Unix socket to named pipe / TCP
  on localhost / shared-memory queue. The contract is "out-of-band
  from the hub, owner-only, bounded reads."
- Change the env-var name (`TUBEMAIL_LOCAL_SOCK` is just a convention).
  Any name works as long as the channel and the manager agree.
- Replace the `task=True` polling with a different long-running-tool
  shape if the MCP protocol evolves.
- Rename the slash command (`/reconnect-mcp` → whatever) — the LLM
  reads the front-matter description, not the filename.

## 8. What MUST NOT change

- The two-tool split (one for hub-down, one for everything else) — see
  §3.2 and the failure mode it solves.
- Worker self-reconnect being a parameterless call — see §3.1.
- Owner-only socket file mode — §3.3.
- Bounded reads on the IPC wire — §3.4.
- Explicit-only invocation — §3.7. Do not add auto-reconnect on tool
  errors without surfacing it as a clearly-labeled opt-in.

A rebuilder who collapses the two tools into one ("just always go via
the hub") loses the tubemail-itself-down case. A rebuilder who drops
the socket and routes everything through the hub for "simplicity" has
reintroduced Problem B.
