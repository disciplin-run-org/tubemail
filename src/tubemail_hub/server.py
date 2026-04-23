"""TubeMail MCP server — FastAPI + FastMCP + tubemail bridge."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastmcp import FastMCP

from tubemail_hub.bridge.engine import BridgeEngine
from tubemail_hub.bridge.http import build_tubemail_router
from tubemail_hub.tools import workers as workers_tools

logger = logging.getLogger(__name__)

SERVER_NAME = "tubemail"

SERVER_INSTRUCTIONS = (
    "TubeMail: orchestration hub for routing work between Claude Code sessions. "
    "Lets an orchestrator session drive other long-running Claude Code sessions "
    "('workers') via the tubemail channel plugin. Workers are native Claude Code "
    "processes launched with --dangerously-load-development-channels server:tubemail. "
    "Each worker registers as <dirname>-qm (or TM_WORKER_NAME-qm if overridden).\n\n"
    "Typical workflow:\n"
    "1. tm_list_workers() to see who is connected.\n"
    "2. tm_send(worker, message) to deliver a work order — returns an event_id.\n"
    "3. tm_wait_for_activity(worker, since=<event_id>) to block until the reply arrives.\n"
    "4. tm_receive(worker, since=...) to read the full event timeline.\n"
    "5. When a worker hits a tool permission prompt, it surfaces here as a "
    "pending permission — tm_pending_permissions() / tm_respond_permission() let "
    "the orchestrator approve or deny remotely without walking back to the worker terminal.\n\n"
    "Scope: v1 is the transport layer only. Routing logic, loop policy, and "
    "scorecard aggregation live in the orchestrator's prompt and follow-on code.\n\n"
    "Call get_instructions() to re-read these instructions at any time. "
    "Call refresh_tools() after the server has been rebuilt to pick up changes."
)

# Backward-compat alias
_INSTRUCTIONS = SERVER_INSTRUCTIONS


def _version() -> str:
    version_file = Path(__file__).parents[2] / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "dev"


def _data_dir() -> Path:
    raw = os.environ.get("TUBEMAIL_DATA_DIR", "/data/tubemail")
    p = Path(raw)
    p.mkdir(parents=True, exist_ok=True)
    return p


def create_app() -> FastAPI:
    """Build the TubeMail FastAPI app with FastMCP mounted at /mcp."""
    version = _version()
    start_time = time.time()
    engine = BridgeEngine(data_dir=_data_dir())

    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    workers_tools.register(mcp, engine)
    mcp_asgi = mcp.http_app(path="/", json_response=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if mcp_asgi and getattr(mcp_asgi, "lifespan", None):
            async with mcp_asgi.lifespan(app):
                yield
        else:
            yield

    app = FastAPI(
        title="TubeMail",
        description="Orchestration hub routing work between Claude Code sessions",
        version=version,
        lifespan=lifespan,
    )

    # Health endpoint
    from tubemail_hub.health import build_health_response

    async def _health() -> dict[str, Any]:
        worker_count = len(engine.list_workers())
        pending = len(engine.list_pending_permissions())
        base = await build_health_response(
            service="tubemail",
            version=version,
            start_time=start_time,
            is_busy=lambda: pending > 0,
        )
        base["worker_count"] = worker_count
        base["pending_permissions"] = pending
        return base

    app.get("/health")(_health)

    # Tubemail router (forwarder plumbing)
    app.include_router(build_tubemail_router(engine))

    # MCP at /mcp
    app.mount("/mcp", mcp_asgi)

    # Landing page — HTML with MCP connection instructions
    @app.get("/", response_class=HTMLResponse)
    async def landing() -> str:
        port = os.environ.get("MCP_PORT", "8004")
        scheme = "https" if Path("/data/server.crt").exists() else "http"
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TubeMail MCP — orchestration hub</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 720px;
         margin: 40px auto; padding: 0 20px; color: #222; line-height: 1.5; }}
  h1 {{ margin-bottom: 4px; }}
  .sub {{ color: #666; margin-top: 0; }}
  code {{ background: #f4f4f4; padding: 1px 6px; border-radius: 3px; }}
  pre {{ background: #f4f4f4; padding: 12px; border-radius: 4px; overflow: auto; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #eee; }}
  th {{ font-size: 13px; color: #666; font-weight: 600; }}
  .ver {{ color: #999; font-size: 13px; }}
</style>
</head>
<body>
<h1>TubeMail <span class="ver">v{version}</span></h1>
<p class="sub">Orchestration hub routing work between Claude Code sessions over tubemail.</p>

<h2>Connect from Claude Code</h2>
<p>Add to <code>.mcp.json</code>:</p>
<pre>{{
  "mcpServers": {{
    "tubemail": {{
      "type": "http",
      "url": "{scheme}://localhost:{port}/mcp/"
    }}
  }}
}}</pre>

<h2>Endpoints</h2>
<table>
<tr><th>Path</th><th>Purpose</th></tr>
<tr><td><code>/mcp/</code></td><td>Orchestrator MCP tools (streamable HTTP)</td></tr>
<tr><td><code>/tubemail/&lt;worker&gt;/*</code></td><td>Forwarder plumbing (bearer-auth'd)</td></tr>
<tr><td><code>/health</code></td><td>Health check — status, uptime, worker count</td></tr>
</table>

<h2>Starting a worker</h2>
<pre>cd /path/to/project-dir
claude-tm</pre>
<p>The worker auto-names itself after the directory it's launched in. The
<code>claude-tm</code> wrapper script sources <code>.env</code> for
<code>TUBEMAIL_SECRET</code> and starts <code>claude</code> with
<code>--dangerously-load-development-channels server:tubemail</code>.</p>
</body>
</html>"""

    # Store engine on app.state for tests that need to inspect it
    app.state.bridge_engine = engine
    app.state.mcp = mcp

    return app
