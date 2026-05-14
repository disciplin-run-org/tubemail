# TubeMail Tutorial

A step-by-step walkthrough that takes you from zero to driving one
Claude Code session from another, with the web UI open, in about 10
minutes.

If you only read one section, read [Five-minute quickstart](#five-minute-quickstart).
The rest is depth.

---

## What you'll have at the end

- The TubeMail hub running in Docker on `localhost:8001`.
- Two Claude Code sessions: an **orchestrator** (this one) and a
  **worker** in a separate terminal.
- The web UI open in your browser, showing both sessions live.
- A clean way to send work to the worker, watch its replies stream
  back, and approve permission prompts without leaving the orchestrator.
- Optional: session recording for after-the-fact debugging.

---

## Prerequisites

| You need | Why |
|---|---|
| Docker + Docker Compose | The hub runs as a container. |
| Python 3.10+ | The worker-side `tubemail-channel` package is a Python plugin. |
| Node.js 18+ (only for dev) | To build the web UI bundle. Skip this if you `pip install tubemail` and use the prebuilt frontend. |
| Claude Code | The thing being orchestrated. |
| ~30 MB of disk for the hub image | Bind-mounted state lives at `<repo>/data/`. |

You don't need: a GitHub account, an API key beyond what Claude Code
already uses, or anything cloud-hosted. Everything runs on your machine.

---

## Five-minute quickstart

Three commands to get the hub running, plus pasting one secret into the
browser.

```bash
# 1. Clone and configure
git clone git@github.com:disciplin-run-org/tubemail.git
cd tubemail
echo "TUBEMAIL_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" > .env

# 2. Start the hub
docker compose up -d tubemail

# 3. Verify
curl -s http://localhost:8001/health | python -m json.tool
```

You should see something like:

```json
{
  "status": "ok",
  "service": "tubemail",
  "version": "0.1.0",
  "workers_online": 0,
  "workers_total": 0,
  "pending_permissions": 0,
  "safe_to_restart": true
}
```

Open <http://localhost:8001> in your browser. You'll see an empty
roster: "No workers connected — Launch one from any project directory:
`claude-tm`."

Now go connect a worker.

---

## Connecting your first worker

The worker side is a Python package that plugs into Claude Code as a
"channel" — a small process that relays events between Claude and the
hub.

### Install the worker package

From the tubemail repo:

```bash
pip install -e channel/ --no-build-isolation
```

This installs the `tubemail` Python package and the
`tubemail-channel` Claude Code plugin. The `claude-tm` bash wrapper
lives at `scripts/claude-tm` in the repo; symlink it where your shell
will find it:

```bash
ln -s "$(pwd)/scripts/claude-tm" ~/.local/bin/claude-tm
```

Make sure `~/.local/bin` is on your `PATH`.

### Launch the worker

`cd` to any project directory and run `claude-tm`:

```bash
cd ~/Projects/my-project
claude-tm
```

What happens:

1. The bash wrapper loads `TUBEMAIL_SECRET` from your environment (or a
   `.env` file in the cwd, or in the tubemail repo).
2. It spawns `tubemail.manager`, a process that runs `claude` inside a
   pty.
3. The manager registers as `<dirname>-tm` (in this case
   `my-project-tm`) with the hub.
4. Claude Code starts as you'd expect, with the `tubemail-channel`
   plugin loaded.

Refresh the web UI. The worker is now in the roster, with a green
manager dot and an `idle` state badge.

If the dot is red or you see no row at all, the most common cause is
the `TUBEMAIL_SECRET` mismatch. Check `~/.local/bin/claude-tm` (it
sources `.env`) and make sure both sides have the same secret.

### What the worker sees

In the worker's terminal, `claude` is just `claude`. Type things, ask
it questions, run tools. The channel plugin sits silently in the
background. You can confirm it loaded by typing `/mcp` in the worker
terminal and looking for `tubemail-channel` in the list.

---

## Sending work from an orchestrator

The orchestrator is just another Claude Code session that has the
`tubemail` MCP server in its `.mcp.json`.

### Wire up the MCP server

Add to `.mcp.json` (or wherever your Claude Code reads MCP config):

```json
{
  "mcpServers": {
    "tubemail": {
      "type": "http",
      "url": "http://localhost:8001/mcp/",
      "headers": {
        "Authorization": "Bearer <your TUBEMAIL_SECRET>"
      }
    }
  }
}
```

Restart Claude Code so it picks up the MCP server. You should now see
`mcp__tubemail__tm_list_workers` and friends in your tool list.

### The basic loop

From the orchestrator's perspective, sending work to a worker is four
tool calls:

```python
# 1. See who's connected.
tm_list_workers()

# 2. Send a work order.
result = tm_send(
    worker="my-project-tm",
    message="What does this repo do? Reply with a one-paragraph summary."
)
event_id = result["event_id"]

# 3. Wait for the worker to do the work.
events = tm_wait_for_activity(worker="my-project-tm", since=event_id)

# 4. Read the reply.
for e in events:
    if e["kind"] == "outbound":
        print(e["content"])
```

What `tm_send` does: enqueues an event on the worker's inbox. The
channel plugin in the worker delivers it to Claude as a regular
message. Claude reads it, does the work, and the channel's `reply` tool
pushes a response back as an `outbound` event.

You can also drive harness commands:

```python
tm_send(worker="my-project-tm", message="/compact")  # auto-routed to manager
tm_send(worker="my-project-tm", message="/clear")    # auto-routed to manager
```

The `tm_send` tool detects built-in slash-commands and routes them
through the manager (which types them into the pty) instead of through
the channel.

---

## Watching the worker live

Two ways: the web UI, or the integrated browser terminal.

### Web UI roster

The Workers tab shows every connected session at a glance:

- **Green dot** under MGR means the manager process is up and
  subscribed to the hub.
- **State badge**: `idle`, `busy · 47s` (with elapsed time), or
  `waiting_permission`. After 10 minutes of trailing inbound with no
  reply, busy decays to idle — work orders that complete with code
  edits or commits don't always emit a channel reply, so this prevents
  workers from being stuck "busy" forever.
- **Project grouping**: workers in the same directory cluster under a
  folder header.
- **Sortable columns**: click any header to sort. Click again to
  reverse. A third click returns to the project-grouped layout.

### Integrated browser terminal

Click any row in the roster. A terminal pane opens, full xterm.js with
a WebSocket pty bridge to the worker's pty. You can:

- Watch the worker's output live as Claude streams it.
- Type into the pty directly (Shift+Enter inserts a hard newline that
  Claude reads as "still typing, don't submit yet").
- Copy a selection with Ctrl+C; if no selection, Ctrl+C sends SIGINT.
- Paste with Ctrl+V or Ctrl+Shift+V.
- Zoom with Ctrl+= / Ctrl+- / Ctrl+0 (font size persists per worker).
- Pop the terminal out into its own browser window for tile-multiple-
  workers-across-the-display layouts.

If the terminal connects but no bytes arrive within 4 seconds, you'll
see a "Worker silent" card with a "Roll manager + reconnect" button.
That handles the case where the worker's manager started before the
live-terminal feature shipped — rolling re-execs the manager, which
picks up the latest channel code without losing the conversation.

---

## Approving permission prompts remotely

When Claude wants to run a tool that needs approval (Bash, Edit, etc.),
the prompt forwards from the worker to the hub via Claude Code's
permission-relay capability. You see it two ways:

- The Permissions tab in the web UI shows every pending prompt across
  all workers in one inbox. Click allow or deny; the worker proceeds
  immediately.
- From an orchestrator session:

  ```python
  pending = tm_pending_permissions()
  # Returns [{worker, request_id, tool_name, description, input_preview}, ...]

  for p in pending:
      tm_respond_permission(
          worker=p["worker"],
          request_id=p["request_id"],
          behavior="allow",  # or "deny"
      )
  ```

The hub dedupes by `request_id`, so two operators clicking allow on the
same prompt is safe — the second click becomes a no-op.

---

## Recording sessions for later review

Recording is off by default. When on, the hub tees the worker's pty
output to two files per session:

- `<data>/recordings/<worker>/<ts>.cast` — full asciinema replay format.
  Open with `asciinema play <file>` to watch the session play back at
  original speed.
- `<data>/recordings/<worker>/<ts>.frames.jsonl` — ANSI-stripped text
  frames, one JSON object per pty chunk. This is what `tm_get_recording`
  reads. Optimized for grep and time-slicing.

### Turn recording on for one worker

In the web UI: Workers tab, click the circle in the `Rec` column for
the worker you want to record. Filled red = on; hollow gray = off.

From an orchestrator:

```python
tm_recording_toggle(worker="my-project-tm", enabled=True)
```

### Read what was recorded

```python
# Latest 100 frames
frames = tm_get_recording(worker="my-project-tm", limit=100)

# Slice by time
frames = tm_get_recording(
    worker="my-project-tm",
    since="2026-04-25T15:00:00Z",
    until="2026-04-25T15:30:00Z",
)

# Filter by content
frames = tm_get_recording(worker="my-project-tm", grep="permission")
```

Each frame is `{"t": ISO-8601, "delta": "<text>"}`. The `delta` is the
chunk of text that was added at that moment, with ANSI escape codes
stripped.

### Always-on by default

If you want every new worker to start with recording on, flip the
"Record new workers by default" toggle in the Settings tab. Existing
workers keep their per-worker setting; only first-register workers
inherit the new default.

Files rotate when the active `.cast` exceeds 5 MiB (default), and only
the 4 most recent files are kept per worker (default). Both knobs are
in Settings.

---

## Saved messages

Recurring work orders shouldn't be retyped. The Saved Messages tab
lets you save named templates that can be sent to any worker — by you
in the UI, or programmatically by an orchestrator.

In the UI:

1. Click "New flow" on the Saved Messages tab.
2. Name it something descriptive (e.g., `daily-status`).
3. Paste the message body.
4. Optionally set a default worker.
5. Save.

To run it:

- UI: pick a worker from the dropdown next to the flow, click Run.
- MCP:

  ```python
  result = tm_run_flow(name="daily-status", worker="iris-qa-tm")
  run_id = result["run_id"]
  log = tm_get_run_log(run_id=run_id)
  ```

Run logs persist on the hub; you can review what was sent and what
came back.

---

## Pop-out terminals for multi-worker layouts

Click the pop-out icon in the terminal pane's chrome (the
`ExternalLink` icon, top right). A new browser window opens with just
that worker's terminal — no sidebar, no chrome. Tile four of them
across a 4K monitor and you can drive a small fleet without ever
opening another iTerm tab.

The popout window auto-resizes itself to match the worker's pty grid,
so you don't have to manually drag it tall enough to see the prompt.

---

## When things go wrong

### The roster shows the worker as red ("offline")

The worker's manager hasn't kept its SSE subscription open. Likely
causes:

- The hub container restarted while the worker was running. Fix: from
  the orchestrator, `tm_update_manager(worker="my-project-tm")` re-execs
  the manager so it re-subscribes. Or restart `claude-tm` in the worker
  terminal.
- Network blip. Wait 5-10 seconds; the manager retries SSE with
  exponential backoff.

### A worker is "busy" forever

If the trailing inbound event is more than 10 minutes old, it
auto-decays to `idle`. Before that, it's amber-busy with a counter.

If a worker is genuinely stuck (Claude is generating but not making
progress), `tm_health(worker="...")` reports CPU + memory. If CPU is
high, work is happening; just wait. If CPU is zero for minutes, the
session may need a `tm_interrupt` or `tm_restart`.

### `tm_send` returns success but the worker doesn't respond

Check `tm_status(worker="...")`. If it's `waiting_permission`, the
worker needs you to allow or deny something — visit the Permissions
tab.

If it's idle, look at the timeline:

```python
events = tm_receive(worker="my-project-tm", limit=10)
```

The latest inbound and outbound events tell you whether the message
landed and whether anything came back.

### The web UI shows "no bearer" or asks for a token

You're not on localhost. The auto-bootstrap that hands you the bearer
only works over loopback. Either:

- Run the browser on the same machine as the hub.
- Or paste the `TUBEMAIL_SECRET` value into the auth gate manually.

For non-localhost deployments, you should also enable HTTPS by dropping
`server.crt` + `server.key` into the hub's data volume.

### Recording is on but no bytes are captured

Known issue when no browser tab is also open on that worker; the
manager's pty stream loop only kicks on for browser-attached
subscribers in some edge cases. Workaround: open the worker's terminal
pane in the web UI while recording. Tracked in
`jjstack/jesper-main-design-20260425-092030.md` "Open work" section.

---

## Where to go next

- **Read the source.** It's small. The hub is ~3000 lines of Python in
  `src/tubemail_hub/`; the worker channel is ~1500 lines in
  `channel/src/tubemail/`. The web UI is ~2000 lines of React.
- **Read the planning docs.** `jjstack/jesper-main-design-20260425-092030.md`
  is the as-built design rationale — why each piece exists. The four
  review docs in `jjstack/ceo-plans/` capture honest critique of the
  shipped state.
- **Build something on top.** TubeMail is transport plus an operator
  surface; orchestration policy (failure routing, retries, scheduling,
  scoring) lives in whatever you build on top. The included
  `quartermaster` placeholder is where that future SaaS orchestrator
  backend lives in this ecosystem; build your own or wait for that.
- **File an issue or send a PR.** GitHub:
  <https://github.com/disciplin-run-org/tubemail>.
