"""Origin-allowlist policy for the pty bridge WebSocket.

Same-origin must be auto-accepted so that operators reaching the hub on
a LAN IP, Tailscale hostname, or reverse-proxied domain are not blocked
by the default localhost-only allowlist. Cross-origin callers must
still be filtered through TUBEMAIL_ALLOWED_ORIGINS.

This pinned down the VPN regression where Tailscale-reached web
terminals failed with WS 1008 even though every other call worked.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tubemail_hub.bridge.engine import BridgeEngine
from tubemail_hub.bridge.pty_registry import PtyBridgeRegistry
from tubemail_hub.bridge.tickets import TicketStore
from tubemail_hub.bridge.ws import (
    _default_allowed_origins,
    _is_same_origin,
    build_ws_router,
)


# ── _is_same_origin unit tests ────────────────────────────────────────


def _fake_ws(host: str, scheme: str = "ws", xfp: str | None = None) -> Any:
    """Minimal stand-in matching the attributes _is_same_origin reads."""
    headers = {"host": host}
    if xfp is not None:
        headers["x-forwarded-proto"] = xfp
    return SimpleNamespace(headers=headers, url=SimpleNamespace(scheme=scheme))


def test_same_origin_localhost_match():
    ws = _fake_ws("localhost:8004")
    assert _is_same_origin(ws, "http://localhost:8004") is True


def test_same_origin_tailscale_hostname_match():
    # The shape that broke over VPN before this fix.
    ws = _fake_ws("my-machine.tail-scale.ts.net:8004")
    assert (
        _is_same_origin(ws, "http://my-machine.tail-scale.ts.net:8004") is True
    )


def test_same_origin_tailscale_cgnat_ip_match():
    ws = _fake_ws("100.64.7.42:8004")
    assert _is_same_origin(ws, "http://100.64.7.42:8004") is True


def test_same_origin_lan_ip_match():
    ws = _fake_ws("192.168.1.50:8004")
    assert _is_same_origin(ws, "http://192.168.1.50:8004") is True


def test_same_origin_case_insensitive_host():
    ws = _fake_ws("MyHost.Local:8004")
    assert _is_same_origin(ws, "http://myhost.local:8004") is True


def test_same_origin_scheme_mismatch_rejected():
    # Plaintext hub, but Origin claims https — block (not same-origin).
    ws = _fake_ws("localhost:8004", scheme="ws")
    assert _is_same_origin(ws, "https://localhost:8004") is False


def test_same_origin_host_mismatch_rejected():
    # Hub on :8004, malicious origin on :9999. Must reject.
    ws = _fake_ws("localhost:8004")
    assert _is_same_origin(ws, "http://localhost:9999") is False


def test_same_origin_forwarded_proto_https_accepted():
    # Reverse proxy terminates TLS, hub speaks ws (plaintext). Browser
    # opened wss:// to the proxy, so Origin is https://...
    ws = _fake_ws("tubemail.example.com", scheme="ws", xfp="https")
    assert _is_same_origin(ws, "https://tubemail.example.com") is True


def test_same_origin_forwarded_proto_chain_uses_first():
    ws = _fake_ws("tubemail.example.com", scheme="ws", xfp="https, http")
    assert _is_same_origin(ws, "https://tubemail.example.com") is True


def test_same_origin_garbage_origin_rejected():
    ws = _fake_ws("localhost:8004")
    assert _is_same_origin(ws, "not-a-url") is False


def test_same_origin_empty_host_rejected():
    ws = _fake_ws("")
    assert _is_same_origin(ws, "http://localhost:8004") is False


# ── _default_allowed_origins ──────────────────────────────────────────


def test_default_allowed_origins_includes_vite(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TUBEMAIL_ALLOWED_ORIGINS", raising=False)
    assert "http://localhost:5173" in _default_allowed_origins()


def test_default_allowed_origins_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "TUBEMAIL_ALLOWED_ORIGINS",
        "https://prod.example.com,https://staging.example.com",
    )
    got = _default_allowed_origins()
    assert got == {
        "https://prod.example.com",
        "https://staging.example.com",
    }


# ── End-to-end WS handler tests via TestClient ───────────────────────


@pytest.fixture
def secret() -> str:
    return "ws-origin-test-secret"


@pytest.fixture(autouse=True)
def set_secret(secret: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TUBEMAIL_SECRET", secret)
    # Force the deterministic default allowlist regardless of host env.
    monkeypatch.delenv("TUBEMAIL_ALLOWED_ORIGINS", raising=False)


@pytest.fixture
def engine(tmp_path: Path) -> BridgeEngine:
    return BridgeEngine(data_dir=tmp_path / "engine")


@pytest.fixture
def tickets() -> TicketStore:
    return TicketStore(ttl_s=30.0)


@pytest.fixture
def pty_bridges() -> PtyBridgeRegistry:
    return PtyBridgeRegistry()


@pytest.fixture
def app(
    engine: BridgeEngine,
    tickets: TicketStore,
    pty_bridges: PtyBridgeRegistry,
) -> FastAPI:
    fastapi_app = FastAPI()
    fastapi_app.include_router(build_ws_router(engine, tickets, pty_bridges))
    return fastapi_app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _mint_ticket(tickets: TicketStore, worker: str) -> str:
    """Issue a ticket synchronously by spinning a one-shot event loop.
    Using `get_event_loop` here breaks when other suites in the same
    pytest run have already torn the default loop down — pytest-asyncio
    auto-mode leaves the main-thread loop in an undefined state between
    tests. A fresh loop sidesteps that entirely."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(tickets.issue(worker))
    finally:
        loop.close()


def test_ws_accepts_same_origin_tailscale_host(
    client: TestClient, tickets: TicketStore
):
    """The bug we are fixing: Tailscale-reached UI must connect."""
    ticket = _mint_ticket(tickets, "demo-tm")
    # The TestClient default base_url is http://testserver. We construct
    # the WS URL with that same host so the request's Host header matches
    # the Origin we send. This mirrors a same-origin browser request.
    with client.websocket_connect(
        f"/ws/pty/demo-tm?ticket={ticket}",
        headers={"origin": "http://testserver"},
    ) as ws:
        # Reached accept(); the handler is now in its pump loops. Closing
        # the client side exits cleanly.
        ws.close()


def test_ws_rejects_foreign_origin(
    client: TestClient, tickets: TicketStore
):
    ticket = _mint_ticket(tickets, "demo-tm")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/ws/pty/demo-tm?ticket={ticket}",
            headers={"origin": "http://evil.example.com"},
        ):
            pass
    assert exc.value.code == 1008


def test_ws_accepts_vite_dev_origin(
    client: TestClient, tickets: TicketStore
):
    """Cross-origin from the vite dev proxy must still work via the
    explicit allowlist entry — that's why we kept it."""
    ticket = _mint_ticket(tickets, "demo-tm")
    with client.websocket_connect(
        f"/ws/pty/demo-tm?ticket={ticket}",
        headers={"origin": "http://localhost:5173"},
    ) as ws:
        ws.close()


def test_ws_rejects_bad_ticket_even_when_same_origin(
    client: TestClient,
):
    """Origin policy is necessary but not sufficient — bad ticket still
    closes 1008, so a same-origin XSS cannot bypass ticket auth."""
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/ws/pty/demo-tm?ticket=not-a-real-ticket",
            headers={"origin": "http://testserver"},
        ):
            pass
    assert exc.value.code == 1008
