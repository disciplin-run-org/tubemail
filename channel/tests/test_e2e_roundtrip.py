"""End-to-end round-trip test against a running TubeMail hub.

Spawns `tubemail` as a subprocess with pipes, simulates the MCP messages
Claude Code would send, and verifies the hub receives and acts on them.

Requires the tubemail (hub) container (port 8004) to be running with
TUBEMAIL_SECRET set in the environment. Skipped automatically if either
condition is unmet, so `pytest tests/` can run cleanly even without Docker.

Note on the hub's MCP transport: FastMCP serves stateful streamable-HTTP,
so every `tools/call` MUST go through a session created by `initialize`.
The `_MCPClient` helper below handles that handshake — earlier versions
of these tests POSTed `tools/call` directly and got `400 Missing
session ID` (RCA: jjstack/qa-reports/20260424-190244-…).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
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
    reason="tubemail hub not reachable on localhost:8004 or TUBEMAIL_SECRET unset",
)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SECRET}"}


class _MCPClient:
    """Minimal MCP-over-HTTP client for tests.

    Handles the FastMCP session protocol: `initialize` returns an
    `Mcp-Session-Id` header that must accompany every subsequent
    `tools/call`. Skip this and the server returns 400. The earlier
    revision of this file POSTed without sessions and never worked.
    """

    def __init__(self, url: str):
        self._url = url
        self._client = httpx.Client(timeout=10.0)
        self._session_id: str | None = None
        self._next_id = 1

    def initialize(self) -> dict[str, Any]:
        r = self._client.post(
            self._url,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": self._gen_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "e2e-test", "version": "1.0"},
                },
            },
        )
        r.raise_for_status()
        sid = r.headers.get("mcp-session-id")
        assert sid, f"hub did not return Mcp-Session-Id header: {r.headers}"
        self._session_id = sid
        # Server expects the initialized notification before tools/call.
        self._client.post(
            self._url,
            headers=self._headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        return r.json()["result"]

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Returns the `result` block of the JSON-RPC response.
        For FastMCP, the structured payload is at
        `result["structuredContent"]["result"]`."""
        r = self._client.post(
            self._url,
            headers=self._headers(),
            json={
                "jsonrpc": "2.0",
                "id": self._gen_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
        )
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"MCP error from {name}: {body['error']}")
        return body["result"]

    def structured(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Convenience wrapper: returns just the structured payload."""
        return self.call_tool(name, arguments)["structuredContent"]["result"]

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _gen_id(self) -> int:
        v = self._next_id
        self._next_id += 1
        return v

    def close(self) -> None:
        self._client.close()


@pytest.fixture
def mcp():
    """Initialized MCP-over-HTTP client. One session per test, torn down at end."""
    c = _MCPClient(f"{HUB_URL}/mcp/")
    c.initialize()
    yield c
    c.close()


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
        env["TUBEMAIL_LOG"] = "INFO"
        # Stream stderr to a per-test log file so we can inspect when a
        # test fails. Test code can read this back via `harness.stderr_log`.
        import tempfile
        self.stderr_log = tempfile.NamedTemporaryFile(
            mode="w+", suffix=f"-{self.worker_name}.log", delete=False
        )
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tubemail",
            cwd=self.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr_log.file,
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
        # Purge our worker (and its manager, if any) from the hub
        # registry. The forwarder's `unregister` POST only marks it
        # offline — without this the registry accumulates one zombie
        # per e2e test run. See jj-qa Rule 1: tests clean up after
        # themselves.
        try:
            with httpx.Client(timeout=2.0) as client:
                for name in (self.worker_name, f"{self.worker_name}-manager"):
                    client.delete(
                        f"{HUB_URL}/api/workers/{name}",
                        headers=_auth_headers(),
                    )
        except Exception:
            # Hub gone or rejecting auth — non-fatal for the test
            # itself, just leaves the entry behind to be swept up by
            # the periodic purge later.
            pass

        # Cleanup the temp stderr log unless the test left it (kept on
        # failure for diagnosis — see read_stderr).
        try:
            self.stderr_log.close()
            os.unlink(self.stderr_log.name)
        except Exception:
            pass

    def read_stderr(self) -> str:
        try:
            self.stderr_log.flush()
            with open(self.stderr_log.name) as f:
                return f.read()
        except Exception:
            return ""

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
    # Test worker names follow the ecosystem `<project>-tm` convention so
    # tm_list_workers includes them (line 134 of tools/workers.py: workers
    # whose names don't end in -tm are hidden by default behind
    # include_stale=True).
    name = f"e2e-{secrets.token_hex(4)}-tm"
    h = ForwarderHarness(worker_name=name)
    await h.start()
    yield h
    await h.stop()


async def test_initialize_and_register(harness: ForwarderHarness, mcp: _MCPClient):
    await _initialize_handshake(harness)
    # Registration is async — the channel POSTs /register only after its
    # SSE pump task starts. Poll for up to 5s instead of guessing a
    # single sleep. tm_list_workers returns a formatted table as a
    # single string; substring-check our worker's name.
    deadline = asyncio.get_event_loop().time() + 5.0
    table = ""
    while asyncio.get_event_loop().time() < deadline:
        table = mcp.structured("tm_list_workers")
        assert isinstance(table, str), f"unexpected shape: {type(table).__name__}"
        if harness.worker_name in table:
            return
        await asyncio.sleep(0.2)
    pytest.fail(
        f"{harness.worker_name!r} did not register within 5s.\n"
        f"--- subprocess stderr ---\n{harness.read_stderr()}\n"
        f"--- roster ---\n{table}"
    )


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


async def test_reply_tool_delivers_to_hub_outbox(
    harness: ForwarderHarness, mcp: _MCPClient
):
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

    events = mcp.structured("tm_receive", {"worker": harness.worker_name})
    outbound = [e for e in events if e["kind"] == "outbound"]
    assert len(outbound) == 1, f"expected 1 outbound, got: {events}"
    assert outbound[0]["content"] == "progress 50%"
    assert outbound[0]["meta"]["step"] == "compile"


async def test_permission_request_surfaces_via_mcp(
    harness: ForwarderHarness, mcp: _MCPClient
):
    await _initialize_handshake(harness)
    await asyncio.sleep(0.2)
    # Use a per-test unique request_id so we don't collide with stale
    # entries from previous runs.
    request_id = f"req-{secrets.token_hex(4)}"
    await harness.send({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel/permission_request",
        "params": {
            "request_id": request_id,
            "tool_name": "Bash",
            "description": "run tests",
            "input_preview": "pytest tests/",
        },
    })
    await asyncio.sleep(0.2)

    pending = mcp.structured(
        "tm_pending_permissions", {"worker": harness.worker_name}
    )
    assert any(
        p["request_id"] == request_id and p["worker"] == harness.worker_name
        for p in pending
    ), f"{request_id} not in pending list: {pending}"

    # Cleanup: resolve so the permission doesn't outlive the test as a
    # zombie if the worker crashes before the SSE-close cleanup runs.
    mcp.call_tool("tm_respond_permission", {
        "worker": harness.worker_name,
        "request_id": request_id,
        "behavior": "deny",
    })


async def test_inbound_message_becomes_channel_notification(
    harness: ForwarderHarness, mcp: _MCPClient
):
    """Orchestrator sends a message; forwarder should emit a notifications/claude/channel."""
    await _initialize_handshake(harness)
    await asyncio.sleep(0.3)  # SSE subscription must be established

    mcp.call_tool("tm_send", {
        "worker": harness.worker_name,
        "message": "please review 4.1.1",
    })

    # The forwarder should now emit a notifications/claude/channel on stdout.
    for _ in range(10):
        msg = await harness.read(timeout=2.0)
        if msg.get("method") == "notifications/claude/channel":
            assert msg["params"]["content"] == "please review 4.1.1"
            assert msg["params"]["meta"]["source"] == "tubemail"
            return
    pytest.fail("did not receive notifications/claude/channel on stdout")


async def test_respond_permission_pushes_notification_to_forwarder(
    harness: ForwarderHarness, mcp: _MCPClient
):
    """Orchestrator resolves a pending permission; forwarder emits
    notifications/claude/channel/permission."""
    await _initialize_handshake(harness)
    await asyncio.sleep(0.3)

    request_id = f"req-{secrets.token_hex(4)}"
    await harness.send({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel/permission_request",
        "params": {"request_id": request_id, "tool_name": "Bash"},
    })
    await asyncio.sleep(0.2)

    mcp.call_tool("tm_respond_permission", {
        "worker": harness.worker_name,
        "request_id": request_id,
        "behavior": "allow",
    })

    # Forwarder should emit the permission notification on stdout.
    for _ in range(10):
        msg = await harness.read(timeout=2.0)
        if msg.get("method") == "notifications/claude/channel/permission":
            assert msg["params"]["request_id"] == request_id
            assert msg["params"]["behavior"] == "allow"
            return
    pytest.fail("did not receive notifications/claude/channel/permission on stdout")
