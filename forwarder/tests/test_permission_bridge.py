"""Tests for the PreToolUse permission bridge."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path

import pytest

from tubemail.permission_bridge import (
    ApprovalExchange,
    HookServer,
    RiskPolicy,
)


# ── ApprovalExchange ─────────────────────────────────────────────────────────

async def test_exchange_request_first_then_approval_returns_id():
    """Channel notification arrives first; hook approval later pairs up."""
    ex = ApprovalExchange()
    assert await ex.offer_request("r1", "Bash") is False  # no approval yet
    assert await ex.offer_approval("Bash") == "r1"        # pairs with banked


async def test_exchange_approval_first_then_request_returns_true():
    """Hook approval is banked; the later channel notification pairs."""
    ex = ApprovalExchange()
    assert await ex.offer_approval("Bash") is None   # no request yet, banked
    assert await ex.offer_request("r1", "Bash") is True


async def test_exchange_keys_by_tool_name():
    """Approvals and requests for different tools don't cross-pair."""
    ex = ApprovalExchange()
    await ex.offer_approval("Bash")
    # An Edit request should NOT pair with the Bash approval.
    assert await ex.offer_request("r1", "Edit") is False


async def test_exchange_fifo_pairing():
    ex = ApprovalExchange()
    await ex.offer_request("r1", "Bash")
    await ex.offer_request("r2", "Bash")
    assert await ex.offer_approval("Bash") == "r1"
    assert await ex.offer_approval("Bash") == "r2"
    assert await ex.offer_approval("Bash") is None


async def test_exchange_expires_stale_entries():
    ex = ApprovalExchange(ttl_s=0.05)
    await ex.offer_request("stale", "Bash")
    await asyncio.sleep(0.1)
    # After TTL, the stale request is reaped — a new approval shouldn't pair.
    assert await ex.offer_approval("Bash") is None
    # And an approval with no pair is banked.
    assert await ex.offer_request("new", "Bash") is True


async def test_exchange_ignores_empty_ids():
    ex = ApprovalExchange()
    assert await ex.offer_request("", "Bash") is False
    assert await ex.offer_request("r", "") is False
    # Those calls banked nothing — so a subsequent request pairs nothing.
    assert await ex.offer_approval("Bash") is None


# ── HookServer (unix socket) ─────────────────────────────────────────────────

class _FakeHub:
    def __init__(self):
        self.responses: list[tuple[str, str]] = []

    async def post_permission_response(self, request_id: str, behavior: str):
        self.responses.append((request_id, behavior))
        return {"ok": True}


class _FakePolicy:
    def __init__(self, decision: str):
        self._decision = decision
        self.calls: list[tuple[str, dict]] = []

    async def decide(self, tool_name: str, tool_input) -> str:
        self.calls.append((tool_name, tool_input))
        return self._decision


async def _send_to_socket(sock_path: Path, payload: dict) -> dict:
    """Mirror what the bash hook does: connect, send JSON, half-close, read reply."""
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write(json.dumps(payload).encode("utf-8"))
    await writer.drain()
    writer.write_eof()
    raw = await reader.read()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return json.loads(raw.decode("utf-8") or "{}")


@pytest.fixture
def tmp_worker_name(tmp_path, monkeypatch):
    # HookServer hard-codes /tmp/tubemail-hook-<worker>.sock. To avoid clobbering
    # a real running forwarder during tests, we patch the socket directory
    # by using a unique worker name per test and unlinking on teardown.
    name = f"test-{tmp_path.name}"
    yield name
    try:
        Path(f"/tmp/tubemail-hook-{name}.sock").unlink()
    except FileNotFoundError:
        pass


async def test_hook_allow_matches_prebanked_request(tmp_worker_name):
    """Request arrived first (banked) → hook allow pairs and posts resolve."""
    ex = ApprovalExchange()
    hub = _FakeHub()
    policy = _FakePolicy(decision="allow")
    server = HookServer(
        worker=tmp_worker_name, exchange=ex, policy=policy, hub=hub
    )
    await server.start()
    try:
        await ex.offer_request("rid-abc", "Bash")
        reply = await _send_to_socket(
            server.socket_path,
            {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
        )
    finally:
        await server.stop()

    assert reply["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert hub.responses == [("rid-abc", "allow")]


async def test_hook_allow_banks_when_no_request_yet(tmp_worker_name):
    """Hook fires before channel notification → approval banked for later."""
    ex = ApprovalExchange()
    hub = _FakeHub()
    policy = _FakePolicy(decision="allow")
    server = HookServer(
        worker=tmp_worker_name, exchange=ex, policy=policy, hub=hub
    )
    await server.start()
    try:
        reply = await _send_to_socket(
            server.socket_path,
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        )
    finally:
        await server.stop()

    # Hook still approves locally so Claude proceeds.
    assert reply["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    # Nothing posted to the hub YET — the channel handler will post when
    # the permission_request notification arrives and pairs.
    assert hub.responses == []
    # The approval is banked: the next offer_request for Bash should pair.
    assert await ex.offer_request("rid-late", "Bash") is True


async def test_hook_defer_does_not_bank_or_post(tmp_worker_name):
    ex = ApprovalExchange()
    hub = _FakeHub()
    policy = _FakePolicy(decision="defer")
    server = HookServer(
        worker=tmp_worker_name, exchange=ex, policy=policy, hub=hub
    )
    await server.start()
    try:
        await ex.offer_request("rid-xyz", "Bash")
        reply = await _send_to_socket(
            server.socket_path,
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
        )
    finally:
        await server.stop()

    # Empty response — Claude falls through to normal prompt flow.
    assert reply == {}
    # Hub gets nothing; the prior request stays banked for orchestrator
    # decision (hub already has it in pending_permissions from the channel).
    assert hub.responses == []
    # The banked request is still available (hook didn't consume it).
    assert await ex.offer_approval("Bash") == "rid-xyz"


async def test_hook_server_cleans_up_stale_socket(tmp_worker_name):
    """A leftover socket from a prior crashed forwarder should be replaced."""
    sock_path = Path(f"/tmp/tubemail-hook-{tmp_worker_name}.sock")
    sock_path.write_text("stale")
    assert sock_path.exists()

    ex = ApprovalExchange()
    hub = _FakeHub()
    policy = _FakePolicy(decision="allow")
    server = HookServer(
        worker=tmp_worker_name, exchange=ex, policy=policy, hub=hub
    )
    await server.start()
    try:
        await ex.offer_request("r", "Bash")
        reply = await _send_to_socket(
            server.socket_path,
            {"tool_name": "Bash", "tool_input": {}},
        )
        assert reply["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    finally:
        await server.stop()


# ── RiskPolicy (pure logic, no network) ──────────────────────────────────────

async def test_risk_policy_read_only_tools_always_allow():
    policy = RiskPolicy(client=None)  # client unused on this branch
    assert await policy.decide("Read", {}) == "allow"
    assert await policy.decide("Grep", {}) == "allow"
    assert await policy.decide("WebFetch", {}) == "allow"


async def test_risk_policy_non_bash_defers():
    policy = RiskPolicy(client=None)
    assert await policy.decide("Edit", {"file_path": "/etc/passwd"}) == "defer"
    assert await policy.decide("Write", {"file_path": "/tmp/x"}) == "defer"


async def test_risk_policy_empty_bash_defers():
    policy = RiskPolicy(client=None)
    assert await policy.decide("Bash", {}) == "defer"
    assert await policy.decide("Bash", {"command": "   "}) == "defer"


async def test_risk_policy_fallback_heuristic_allows_safe_readonly(monkeypatch):
    # Force the no-API-key branch
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/nonexistent")))
    policy = RiskPolicy(client=None)
    assert await policy.decide("Bash", {"command": "ls -la"}) == "allow"
    assert await policy.decide("Bash", {"command": "git status"}) == "allow"


async def test_risk_policy_fallback_heuristic_defers_piped_and_substituted(
    monkeypatch,
):
    # Mirrors auto-approve-safe.sh's fallback patterns verbatim: pipes and
    # command substitution `$(..)` defer. (Semicolon-chained non-safe
    # commands are NOT caught by either pattern — intentionally preserving
    # the original hook's behavior.)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/nonexistent")))
    policy = RiskPolicy(client=None)
    assert await policy.decide("Bash", {"command": "ls | grep foo"}) == "defer"
    assert await policy.decide("Bash", {"command": "echo $(whoami)"}) == "defer"
    # Non-safe command not starting with a safe-readonly prefix → defer
    assert await policy.decide("Bash", {"command": "rm -rf /tmp/x"}) == "defer"
