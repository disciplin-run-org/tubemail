"""TubeMail MCP server — FastAPI + FastMCP + tubemail bridge."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastmcp import FastMCP
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from tubemail_hub.bridge.api import build_api_router, build_events_router, build_flows_router
from tubemail_hub.bridge.engine import BridgeEngine
from tubemail_hub.bridge.flows import FlowStore
from tubemail_hub.bridge.http import build_tubemail_router
from tubemail_hub.bridge.pty_registry import PtyBridgeRegistry
from tubemail_hub.bridge.tickets import TicketStore
from tubemail_hub.bridge.ws import build_ws_router
from tubemail_hub.config import load_config
from tubemail_hub.recorder import RecordingManager
from tubemail_hub.tools import workers as workers_tools
from tubemail_hub.tools.flows import register_flow_tools

logger = logging.getLogger(__name__)

SERVER_NAME = "tubemail"

SERVER_INSTRUCTIONS = (
    "TubeMail: orchestration hub for routing work between Claude Code sessions. "
    "Lets an orchestrator session drive other long-running Claude Code sessions "
    "('workers') via the tubemail channel plugin. Workers are native Claude Code "
    "processes launched with --dangerously-load-development-channels server:tubemail-channel. "
    "Each worker registers as <dirname>-tm (or TM_WORKER_NAME-tm if overridden).\n\n"
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


class _SPACacheHeaders(BaseHTTPMiddleware):
    """Cache policy for the SPA: index.html always revalidates, hashed
    /assets/* are immutable.

    After a container rebuild, browsers cached `index.html` from before
    the build and never see the new bundle until the user hard-refreshes.
    Vite emits content-hashed asset filenames under /assets/, so changed
    code gets a new URL — those are safe to cache forever; unchanged
    assets stay in the browser cache across rebuilds. Only the small
    `index.html` document needs revalidation.

    Mounted at the FastAPI level (not on the StaticFiles app) so it
    applies regardless of which router actually served the response.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith(".html"):
            response.headers["Cache-Control"] = (
                "no-cache, no-store, must-revalidate"
            )
        elif "/assets/" in path:
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
            )
        return response


class _SPAStaticFiles(StaticFiles):
    """StaticFiles variant that serves `index.html` for any 404 so
    client-side routes (e.g. `/workers/iris-qa-tm`) reload cleanly
    instead of returning the default 404 body.

    Only applies when `html=True`. Non-HTML asset 404s still 404 —
    we only redirect to `index.html` when the client asked for a
    document path (Accept includes text/html) or when the path has
    no file extension.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code != 404:
                raise
            # Only fall back for document requests, never for an asset
            # (e.g. `/assets/foo.js`) that actually is missing.
            accept = ""
            for k, v in scope.get("headers", []):
                if k == b"accept":
                    accept = v.decode("latin-1", "ignore")
                    break
            looks_like_doc = "text/html" in accept or "." not in path.rsplit("/", 1)[-1]
            if not looks_like_doc:
                raise
            return await super().get_response("index.html", scope)


async def _sweep_tickets(store: TicketStore) -> None:
    """Evict expired-but-unused tickets every 60s."""
    try:
        while True:
            await asyncio.sleep(60)
            n = await store.sweep()
            if n:
                logging.getLogger(__name__).debug(
                    "ticket sweeper evicted %d expired tickets", n
                )
    except asyncio.CancelledError:
        raise


async def _periodic_worker_purge(engine: BridgeEngine, max_age_s: float) -> None:
    """Drop stale offline workers every 10 minutes. Pairs with the
    startup purge so a long-running hub doesn't accumulate registry
    entries between restarts."""
    log = logging.getLogger(__name__)
    try:
        while True:
            await asyncio.sleep(600)  # 10 min
            try:
                purged = await engine.purge_stale_workers(max_age_s=max_age_s)
                if purged:
                    log.info(
                        "periodic purge: dropped %d stale workers (>%.0fh offline)",
                        len(purged), max_age_s / 3600,
                    )
            except Exception as e:
                log.warning("periodic purge failed: %s", e)
    except asyncio.CancelledError:
        raise


def _data_dir() -> Path:
    raw = os.environ.get("TUBEMAIL_DATA_DIR", "/data/tubemail")
    p = Path(raw)
    p.mkdir(parents=True, exist_ok=True)
    return p


def create_app() -> FastAPI:
    """Build the TubeMail FastAPI app with FastMCP mounted at /mcp."""
    version = _version()
    start_time = time.time()
    data_dir = _data_dir()
    hub_config = load_config(data_dir)
    recorder = RecordingManager(
        recordings_dir=data_dir / "recordings",
        max_bytes_per_file=hub_config.recording_max_bytes_per_file,
        keep_files=hub_config.recording_keep_files,
    )
    pty_bridges = PtyBridgeRegistry(recorder=recorder)
    engine = BridgeEngine(
        data_dir=data_dir,
        recorder=recorder,
        recording_default=hub_config.recording_enabled_by_default,
        pty_bridges=pty_bridges,
    )
    flow_store = FlowStore(data_dir=data_dir)
    ticket_store = TicketStore(ttl_s=30.0)

    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    workers_tools.register(mcp, engine)
    register_flow_tools(mcp, engine, flow_store)
    mcp_asgi = mcp.http_app(path="/", json_response=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # On startup: purge offline workers older than 1 hour
        # (configurable via TUBEMAIL_WORKER_PURGE_MAX_AGE_S). The
        # registry is a rolling record of who's ever been here —
        # without periodic purges it grows for the life of the data
        # volume. 1h is an aggressive default that matches the
        # reality of forwarder lifecycle: a manager that's been gone
        # for an hour with no pending work is almost certainly dead.
        # Operators who care about longer retention bump the env var.
        max_age = float(os.environ.get("TUBEMAIL_WORKER_PURGE_MAX_AGE_S", 3600))
        try:
            purged = await engine.purge_stale_workers(max_age_s=max_age)
            if purged:
                logger.info(
                    "startup purge: dropped %d stale workers (>%.0fh offline)",
                    len(purged), max_age / 3600,
                )
        except Exception as e:
            logger.warning("startup purge failed: %s", e)
        # Background tasks: ticket sweeper + periodic worker purge.
        sweep_task = asyncio.create_task(_sweep_tickets(ticket_store))
        purge_task = asyncio.create_task(
            _periodic_worker_purge(engine, max_age)
        )
        try:
            if mcp_asgi and getattr(mcp_asgi, "lifespan", None):
                async with mcp_asgi.lifespan(app):
                    yield
            else:
                yield
        finally:
            for t in (sweep_task, purge_task):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    app = FastAPI(
        title="TubeMail",
        description="Orchestration hub routing work between Claude Code sessions",
        version=version,
        lifespan=lifespan,
    )
    # Cache policy for the SPA. Must be added before any routes so the
    # response-rewriting wraps every handler (including StaticFiles).
    app.add_middleware(_SPACacheHeaders)

    # Health endpoint — live-state metrics only (stale on-disk entries
    # from dead workers do NOT count). See
    # jjstack/investigate/20260424-162835-health-reports-stale-state-as-live.md
    # for the RCA that drove this shape.
    from fastapi import Response
    from tubemail_hub.health import build_health_response

    async def _health() -> Response:
        workers_online = engine.count_online_workers()
        workers_total = len(engine.list_workers())
        pending_online = len(
            engine.list_pending_permissions(online_only=True)
        )
        base = await build_health_response(
            service="tubemail",
            version=version,
            start_time=start_time,
            # "busy" for the safe_to_restart signal = anyone online has
            # pending work. Stale zombies from dead sessions are ignored.
            is_busy=lambda: pending_online > 0,
        )
        base["workers_online"] = workers_online
        base["workers_total"] = workers_total
        base["pending_permissions"] = pending_online
        # Pretty-print the JSON so curl-ing /health from a shell is
        # readable without piping through jq. Adds ~20 bytes of
        # whitespace; negligible for a human-targeted endpoint.
        import json
        return Response(
            content=json.dumps(base, indent=2) + "\n",
            media_type="application/json",
        )

    app.get("/health")(_health)

    # Tubemail router (forwarder plumbing + new pty-out endpoint)
    app.include_router(build_tubemail_router(engine, pty_bridges=pty_bridges))

    # Web UI: JSON API + hub-wide SSE stream + flows REST + pty WS bridge
    app.include_router(build_api_router(
        engine, tickets=ticket_store, recorder=recorder, data_dir=data_dir,
    ))
    app.include_router(build_events_router(engine))
    app.include_router(build_flows_router(flow_store, engine))
    app.include_router(build_ws_router(engine, ticket_store, pty_bridges))

    # MCP at /mcp
    app.mount("/mcp", mcp_asgi)

    # Frontend SPA (built by `vite build` into frontend/dist). Mounted LAST
    # so all specific routes (/health, /tubemail/*, /api/*, /mcp, /ws)
    # win first. Uses a SPA-aware StaticFiles subclass: unknown paths
    # fall back to index.html so client-side routes like
    # `/workers/iris-qa-tm` reload cleanly instead of 404ing.
    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount(
            "/",
            _SPAStaticFiles(directory=str(frontend_dist), html=True),
            name="spa",
        )
        app.state.bridge_engine = engine
        app.state.mcp = mcp
        return app

    # Dev fallback path — frontend not built yet. Keep the legacy landing
    # page so /health and /mcp still work and the operator gets a useful
    # info page at /.
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
<code>--dangerously-load-development-channels server:tubemail-channel</code>.</p>
</body>
</html>"""

    # Store engine on app.state for tests that need to inspect it
    app.state.bridge_engine = engine
    app.state.mcp = mcp

    return app
