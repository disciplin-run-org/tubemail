"""Entry point for the tubemail channel plugin.

Spawned by `claude --dangerously-load-development-channels server:tubemail`
as a stdio subprocess of a Claude Code worker session.

Environment variables:
    TUBEMAIL_HUB_URL       — TubeMail base URL (default: http://localhost:8004)
    TUBEMAIL_SECRET — required, shared bearer secret with the hub
    TM_WORKER_NAME   — override default worker name (basename of cwd)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx

from .channel import Channel
from .hub_client import HubClient
from .permission_bridge import ApprovalExchange, HookServer, RiskPolicy


def _configure_logging() -> None:
    # Log to stderr only — stdout is the JSON-RPC transport and must not
    # contain anything other than JSON-RPC messages.
    level = os.environ.get("TUBEMAIL_LOG", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )


def _resolve_worker_name() -> str:
    override = os.environ.get("TM_WORKER_NAME")
    if override:
        return override
    return Path.cwd().name or "worker"


async def _run() -> int:
    _configure_logging()
    log = logging.getLogger("tubemail")

    hub_url = os.environ.get("TUBEMAIL_HUB_URL", "http://localhost:8004")
    secret = os.environ.get("TUBEMAIL_SECRET")
    if not secret:
        log.error("TUBEMAIL_SECRET not set — refusing to start")
        return 2

    worker = _resolve_worker_name()
    cwd = str(Path.cwd())
    log.info("starting tubemail worker=%s cwd=%s hub=%s", worker, cwd, hub_url)

    hub = HubClient(base_url=hub_url, worker=worker, secret=secret, cwd=cwd)

    # Permission bridge: local unix socket the PreToolUse hook talks to.
    # Shares an ApprovalExchange with Channel so hook-side approvals and
    # channel-side permission_request notifications can pair up regardless
    # of which one fires first (Claude can fire the hook before emitting
    # the notification, so both sides must look for a pending counterpart).
    exchange = ApprovalExchange()
    policy_client = httpx.AsyncClient()
    policy = RiskPolicy(client=policy_client)
    hook_server = HookServer(
        worker=worker, exchange=exchange, policy=policy, hub=hub
    )
    try:
        await hook_server.start()
    except Exception:
        log.exception("failed to start hook socket server — continuing without it")

    channel = Channel(
        hub=hub, worker_name=worker, cwd=cwd, pending_buffer=exchange
    )
    try:
        await channel.run()
    finally:
        await hook_server.stop()
        await policy_client.aclose()
    return 0


def main() -> None:
    try:
        code = asyncio.run(_run())
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
