# TubeMail for Claude Code

Claude Code workers on a wire.

TubeMail lets one Claude Code session drive another. Start a worker in any directory, and any other Claude Code session (or MCP-aware agent) can send it messages, receive replies, approve permission prompts remotely, and restart it — all over HTTP/SSE.

## What this is

- A **hub** service (FastAPI + FastMCP) that brokers events between workers.
- A **forwarder** that wraps `claude` so a session registers as a named worker.
- A **CLI wrapper** (`claude-tm`) that starts a worker and keeps it alive across restarts.

## What this is not

- Not an orchestrator. TubeMail is the plumbing. Routing logic, failure classification, and work policy live in whatever orchestrator you build on top. TubeMail ships the send/receive/restart primitives, nothing more.
- Not a replacement for Claude Code's native tools. Workers are vanilla `claude` processes.

## Architecture

```
┌─────────────────────┐        HTTP/SSE         ┌──────────────────┐
│  Orchestrator       │ ───────────────────────▶│  TubeMail hub    │
│  (any MCP client)   │ ◀───────────────────────│  (FastMCP :8004) │
│  tm_send / tm_recv  │                         └────────┬─────────┘
└─────────────────────┘                                  │
                                                         │  HTTP/SSE
                                                         ▼
                                            ┌────────────────────────┐
                                            │  Worker session        │
                                            │  claude-tm (PID file)  │
                                            │  ├─ forwarder (pty)    │
                                            │  └─ claude --name ...  │
                                            └────────────────────────┘
```

## Install

Two packages — one for each side.

| Side | Install | Binary / tools |
|------|---------|----------------|
| Worker | `pip install tubemail` | `claude-tm` wrapper |
| Hub | `pip install tubemail-hub` | MCP server at `:8004`; `tm_*` tools |

For local development:

```bash
git clone git@github.com:jesperjurcenoks/tubemail.git
cd tubemail
pip install -e forwarder/ --no-build-isolation
pip install -e .[dev] --no-build-isolation
```

## Quickstart

1. **Start the hub:**
   ```
   echo 'TUBEMAIL_SECRET=change-me' > .env
   docker compose up -d tubemail-hub
   ```

2. **Start a worker** in any project directory:
   ```
   ln -s $(pwd)/scripts/claude-tm ~/.local/bin/claude-tm
   cd /path/to/your/project && claude-tm
   ```
   Registers as `<dirname>-tm`.

3. **From another Claude Code session** (with the `tubemail` MCP server added to `.mcp.json`):
   ```python
   tm_list_workers()
   tm_send(worker="your-project-tm", message="what's in this repo?")
   tm_wait_for_activity(worker="your-project-tm", since=<event_id>)
   tm_receive(worker="your-project-tm", since=<event_id>)
   ```

## Tool surface (hub MCP)

| Tool | What |
|------|------|
| `tm_list_workers` | Who is connected |
| `tm_send` | Deliver a message to a worker |
| `tm_receive` | Read a worker's event timeline |
| `tm_wait_for_activity` | Block until the worker produces an event |
| `tm_status` | Quick check: idle / busy / waiting_permission |
| `tm_my_inbox` | Worker-facing: what messages arrived while I was offline |
| `tm_pending_permissions` | List tool-approval prompts across workers |
| `tm_respond_permission` | Allow / deny a pending permission |
| `tm_interrupt` | Pause a worker |
| `tm_restart` | Clean restart via `/exit` + `--continue` |
| `tm_stop` | Kill a worker |
| `tm_keystroke` | Send raw keystrokes to a worker's pty |
| `tm_health` | CPU / memory / uptime — is the worker actually working? |
| `tm_screenshot` | Read recent stdout from a worker |
| `tm_update_wrapper` | Tell the wrapper to re-exec (pick up new forwarder code) |
| `tm_clear_and_send` | Atomic: `/clear` then send, avoids race on permission prompt |
| `tm_reconnect_mcp` | Drive the worker's `/mcp` UI to reconnect a failed server |

## Environment

| Var | Required | Purpose |
|-----|----------|---------|
| `TUBEMAIL_SECRET` | yes | Shared bearer secret between hub and forwarders |
| `TUBEMAIL_HUB_URL` | no (default `http://localhost:8004`) | Where forwarders connect |
| `TM_WORKER_NAME` | no | Override the auto-derived worker name |
| `TUBEMAIL_LOG` | no (default `WARNING`) | Forwarder log level |
| `TUBEMAIL_LOG_FILE` | no | Path to forwarder log file |

## License

MIT — see `LICENSE`.
