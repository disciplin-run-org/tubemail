#!/usr/bin/env python3
"""Fake MCP server for testing /mcp reconnect.

Single-process FastAPI+FastMCP fixture with toggleable health. While
healthy, the MCP endpoint behaves normally and Claude shows it as
``✔ connected``. After ``POST /control/break`` every ``/mcp/*`` request
returns 503 — Claude's MCP client surfaces the error and the next ``/mcp``
dialog renders ``✘ failed``. ``POST /control/heal`` flips it back so
``tm_reconnect_mcp`` (or a manual /mcp Reconnect) succeeds.

Run it standalone:

    python scripts/fake_mcp_server.py            # 127.0.0.1:8099
    python scripts/fake_mcp_server.py --port 8123 --name brokenmail

Register in the project's ``.mcp.json`` so a claude-tm worker picks it up::

    {
      "mcpServers": {
        "fake-mcp": {"type": "http", "url": "http://localhost:8099/mcp/"}
      }
    }

End-to-end loop you can replay as many times as you like:

    1. Start fake_mcp_server.py and a claude-tm worker pointed at it.
       Worker's /mcp dialog shows ``fake-mcp · ✔ connected``.
    2. curl -fsS -X POST http://localhost:8099/control/break
    3. From the worker session ask Claude to call ``mcp__fake-mcp__ping``.
       The call fails with 503; /mcp dialog now shows ``✘ failed``.
    4. curl -fsS -X POST http://localhost:8099/control/heal
    5. From the orchestrator: ``tm_reconnect_mcp("<worker>", "fake-mcp")``.
    6. /mcp dialog should flip back to ``✔ connected`` and ping works.

Reset the failure counter between runs with ``POST /control/reset``.
"""
from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastmcp import FastMCP

logger = logging.getLogger("fake_mcp_server")


@dataclass
class _State:
    broken: bool = False
    break_count: int = 0
    heal_count: int = 0


def _build_mcp(name: str, state: _State) -> FastMCP:
    mcp = FastMCP(
        name,
        instructions=(
            f"Fake MCP server '{name}' for /mcp reconnect testing. "
            "Toggle health via POST /control/break and /control/heal."
        ),
    )

    @mcp.tool
    def ping() -> str:
        """Return 'pong'. Use to verify the MCP server is reachable."""
        return "pong"

    @mcp.tool
    def whoami() -> dict:
        """Return the server name and current control state."""
        return {
            "name": name,
            "broken": state.broken,
            "break_count": state.break_count,
            "heal_count": state.heal_count,
        }

    return mcp


def create_app(name: str = "fake-mcp") -> FastAPI:
    state = _State()
    mcp = _build_mcp(name, state)
    mcp_asgi = mcp.http_app(path="/", json_response=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if mcp_asgi is not None and getattr(mcp_asgi, "lifespan", None):
            async with mcp_asgi.lifespan(app):
                yield
        else:
            yield

    app = FastAPI(title=f"{name} (fake MCP)", lifespan=lifespan)
    app.state.fake = state

    @app.middleware("http")
    async def break_mcp_when_flagged(request: Request, call_next):
        if state.broken and request.url.path.startswith("/mcp"):
            return JSONResponse(
                status_code=503,
                content={
                    "error": f"{name} is in broken state",
                    "hint": "POST /control/heal to recover",
                },
            )
        return await call_next(request)

    @app.get("/control/status")
    async def status() -> dict:
        return {
            "name": name,
            "broken": state.broken,
            "break_count": state.break_count,
            "heal_count": state.heal_count,
        }

    @app.post("/control/break")
    async def break_now() -> dict:
        state.broken = True
        state.break_count += 1
        logger.warning("BREAK — /mcp/* requests will return 503")
        return {
            "name": name,
            "broken": True,
            "break_count": state.break_count,
        }

    @app.post("/control/heal")
    async def heal() -> dict:
        state.broken = False
        state.heal_count += 1
        logger.warning("HEAL — /mcp/* requests will succeed again")
        return {
            "name": name,
            "broken": False,
            "heal_count": state.heal_count,
        }

    @app.post("/control/reset")
    async def reset() -> dict:
        state.broken = False
        state.break_count = 0
        state.heal_count = 0
        logger.info("RESET — counters cleared, state healthy")
        return {"name": name, "broken": False, "break_count": 0, "heal_count": 0}

    app.mount("/mcp", mcp_asgi)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Toggleable fake MCP server for reconnect testing.",
    )
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--name", default="fake-mcp",
        help="Server name reported by FastMCP and shown in /control/status.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    import uvicorn
    uvicorn.run(
        create_app(name=args.name),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
