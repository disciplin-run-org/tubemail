"""End-to-end round-trip test against a running TubeMail hub.

Spawns `tubemail` as a subprocess with pipes, simulates the MCP messages
Claude Code would send, and verifies the hub receives and acts on them.

Requires the tubemail-hub container (port 8004) to be running with
TUBEMAIL_SECRET set in the environment. Skipped automatically if either
condition is unmet, so `pytest tests/` can run cleanly even without Docker.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
from typing import Any

import httpx
import pytest

HUB_URL = os.environ.get("TUBEMAIL_HUB_URL", "http://localhost:8004")
SECRET = os.environ.get("TUBEMAIL_SECRET")


def _hub_reachable() -> bool:
    try:
        r = httpx.get(f"{HUB_URL}/health", timeout=1.0)
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not SECRET or not _hub_reachable(),
    reason="tubemail-hub not reachable on localhost:8004 or TUBEMAIL_SECRET unset",
)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SECRET}"}


class ForwarderHarness:
    """Spawns tubemail and gives tests a clean async stdin/stdout handle."""

    def __init__(self, worker_name: str, cwd: str = "/tmp"):
        self.worker_name = worker_name
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 0

    async def start(self) -> None:
        env = os.environ.copy()
        env["TUBEMAIL_SECRET"] = SECRET or ""
        env["TM_WORKER_NAME"] = self.worker_name
        env["TUBEMAIL_HUB_URL"] = HUB_URL
        env["TUBEMAIL_LOG"] = "WARNING"
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tubemail",
            cwd=self.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()

    async def send(self, msg: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        line = (json.dumps(msg) + "\n").encode()
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

    async def read(self, timeout: float = 2.0) -> dict[str, Any]:
        assert self.proc and self.proc.stdout
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=timeout)
        if not line:
            raise RuntimeError("forwarder stdout EOF")
        return json.loads(line.decode())

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id


async def _initialize_handshake(h: ForwarderHarness) -> dict[str, Any]:
    await h.send({
        "jsonrpc": "2.0",
        "id": h.next_id(),
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "e2e-test", "version": "1.0"},
        },
    })
    resp = await h.read()
    assert resp["id"] == 1, resp
    caps = resp["result"]["capabilities"]
    assert "claude/channel" in caps["experimental"]
    assert "claude/channel/permission" in caps["experimental"]
    await h.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return resp["result"]


@pytest.fixture
async def harness():
    name = f"e2e-{secrets.token_hex(4)}"
    h = ForwarderHarness(worker_name=name)
    await h.start()
    yield h
    await h.stop()


async def test_initialize_and_register(harness: ForwarderHarness):
    await _initialize_handshake(harness)
    # Give the forwarder a beat to POST its register call
    await asyncio.sleep(0.3)
    r = httpx.get(f"{HUB_URL}/mcp/", headers=_auth_headers(), timeout=2.0)
    # We can't directly enumerate — list_workers goes through MCP. Call it.
    list_resp = httpx.post(
        f"{HUB_URL}/mcp/",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 99, "method": "tools/call",
              "params": {"name": "tm_list_workers", "arguments": {}}},
        timeout=5.0,
    )
    assert list_resp.status_code == 200
    body = list_resp.json()
    worker_names = [
        w["name"]
        for w in body["result"]["structuredContent"]["result"]
    ]
    assert harness.worker_name in worker_names


async def test_tools_list_returns_reply_and_ack(harness: ForwarderHarness):
    await _initialize_handshake(harness)
    await harness.send({
        "jsonrpc": "2.0",
        "id": harness.next_id(),
        "method": "tools/list",
    })
    resp = await harness.read()
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "reply" in names
    assert "ack" in names


async def test_reply_tool_delivers_to_hub_outbox(harness: ForwarderHarness):
    await _initialize_handshake(harness)
    await asyncio.sleep(0.2)  # let register settle
    await harness.send({
        "jsonrpc": "2.0",
        "id": harness.next_id(),
        "method": "tools/call",
        "params": {
            "name": "reply",
            "arguments": {"text": "progress 50%", "meta": {"step": "compile"}},
        },
    })
    resp = await harness.read()
    assert "error" not in resp

    # Pull the event back via the orchestrator MCP tool
    recv_resp = httpx.post(
        f"{HUB_URL}/mcp/",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "tm_receive", "arguments": {"worker": harness.worker_name}},
        },
        timeout=5.0,
    )
    body = recv_resp.json()
    events = body["result"]["structuredContent"]["result"]
    outbound = [e for e in events if e["kind"] == "outbound"]
    assert len(outbound) == 1
    assert outbound[0]["content"] == "progress 50%"
    assert outbound[0]["meta"]["step"] == "compile"


async def test_permission_request_surfaces_via_mcp(harness: ForwarderHarness):
    await _initialize_handshake(harness)
    await asyncio.sleep(0.2)
    await harness.send({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel/permission_request",
        "params": {
            "request_id": "abcde",
            "tool_name": "Bash",
            "description": "run tests",
            "input_preview": "pytest tests/",
        },
    })
    await asyncio.sleep(0.2)

    pending_resp = httpx.post(
        f"{HUB_URL}/mcp/",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "tm_pending_permissions",
                "arguments": {"worker": harness.worker_name},
            },
        },
        timeout=5.0,
    )
    pending = pending_resp.json()["result"]["structuredContent"]["result"]
    assert any(
        p["request_id"] == "abcde" and p["worker"] == harness.worker_name
        for p in pending
    )


async def test_inbound_message_becomes_channel_notification(harness: ForwarderHarness):
    """Orchestrator sends a message; forwarder should emit a notifications/claude/channel."""
    await _initialize_handshake(harness)
    await asyncio.sleep(0.3)  # SSE subscription must be established

    # Orchestrator: send a message to this worker via MCP
    send_resp = httpx.post(
        f"{HUB_URL}/mcp/",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "tm_send",
                "arguments": {
                    "worker": harness.worker_name,
                    "message": "please review 4.1.1",
                },
            },
        },
        timeout=5.0,
    )
    assert send_resp.status_code == 200

    # The forwarder should now emit a notifications/claude/channel on stdout
    for _ in range(10):
        msg = await harness.read(timeout=2.0)
        if msg.get("method") == "notifications/claude/channel":
            assert msg["params"]["content"] == "please review 4.1.1"
            assert msg["params"]["meta"]["source"] == "tubemail"
            return
    pytest.fail("did not receive notifications/claude/channel on stdout")


async def test_respond_permission_pushes_notification_to_forwarder(harness: ForwarderHarness):
    """Orchestrator resolves a pending permission; forwarder emits
    notifications/claude/channel/permission."""
    await _initialize_handshake(harness)
    await asyncio.sleep(0.3)

    # Queue a permission request as if the worker had sent it
    await harness.send({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel/permission_request",
        "params": {"request_id": "fghij", "tool_name": "Bash"},
    })
    await asyncio.sleep(0.2)

    # Orchestrator: respond allow
    resp = httpx.post(
        f"{HUB_URL}/mcp/",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "tm_respond_permission",
                "arguments": {
                    "worker": harness.worker_name,
                    "request_id": "fghij",
                    "behavior": "allow",
                },
            },
        },
        timeout=5.0,
    )
    assert resp.status_code == 200

    # Forwarder should emit the permission notification on stdout
    for _ in range(10):
        msg = await harness.read(timeout=2.0)
        if msg.get("method") == "notifications/claude/channel/permission":
            assert msg["params"]["request_id"] == "fghij"
            assert msg["params"]["behavior"] == "allow"
            return
    pytest.fail("did not receive notifications/claude/channel/permission on stdout")
