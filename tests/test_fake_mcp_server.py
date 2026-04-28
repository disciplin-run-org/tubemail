"""Tests for the fake MCP server fixture used to exercise /mcp reconnect."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# scripts/ isn't a package; add it to sys.path so we can import the module.
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from fake_mcp_server import create_app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    # TestClient runs lifespan on enter, which builds the FastMCP app.
    with TestClient(create_app(name="fake-mcp-test")) as c:
        yield c


class TestControlEndpoints:
    def test_status_starts_healthy(self, client: TestClient):
        r = client.get("/control/status")
        assert r.status_code == 200
        body = r.json()
        assert body == {
            "name": "fake-mcp-test",
            "broken": False,
            "break_count": 0,
            "heal_count": 0,
        }

    def test_break_then_heal_increments_counters(self, client: TestClient):
        client.post("/control/break")
        client.post("/control/break")
        client.post("/control/heal")
        body = client.get("/control/status").json()
        assert body["broken"] is False
        assert body["break_count"] == 2
        assert body["heal_count"] == 1

    def test_reset_clears_counters_and_heals(self, client: TestClient):
        client.post("/control/break")
        client.post("/control/reset")
        body = client.get("/control/status").json()
        assert body == {
            "name": "fake-mcp-test",
            "broken": False,
            "break_count": 0,
            "heal_count": 0,
        }


class TestMcpBreakerGate:
    def test_mcp_returns_503_only_when_broken(self, client: TestClient):
        # Healthy: anything under /mcp is handled by FastMCP and is NOT
        # the breaker's 503. We don't speak full MCP protocol here, so
        # we accept any non-503 status as proof the gate is open.
        r = client.get("/mcp/")
        assert r.status_code != 503

        client.post("/control/break")
        r = client.get("/mcp/")
        assert r.status_code == 503
        body = r.json()
        assert "broken state" in body["error"]
        assert "heal" in body["hint"]

        client.post("/control/heal")
        r = client.get("/mcp/")
        assert r.status_code != 503

    def test_break_does_not_affect_control_endpoints(self, client: TestClient):
        client.post("/control/break")
        # Control plane must remain reachable so the test loop can heal.
        assert client.get("/control/status").status_code == 200
        assert client.post("/control/heal").status_code == 200
