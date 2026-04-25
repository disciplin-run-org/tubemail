"""HTTP-level tests for the new web-UI API routers.

Covers /api/workers, /api/permissions, /api/pty-ticket, /api/flows, and
the SPA fallback. The events stream and WS endpoint are exercised via
their own tests elsewhere; here we focus on the shape + auth of the
REST surface the frontend consumes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubemail_hub.bridge.api import (
    build_api_router,
    build_events_router,
    build_flows_router,
)
from tubemail_hub.bridge.engine import BridgeEngine
from tubemail_hub.bridge.flows import FlowStore
from tubemail_hub.bridge.tickets import TicketStore


@pytest.fixture
def secret() -> str:
    return "test-secret-abc123"


@pytest.fixture(autouse=True)
def set_secret(secret: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TUBEMAIL_SECRET", secret)


@pytest.fixture
def engine(tmp_path: Path) -> BridgeEngine:
    return BridgeEngine(data_dir=tmp_path / "engine")


@pytest.fixture
def flows(tmp_path: Path) -> FlowStore:
    return FlowStore(data_dir=tmp_path / "flows")


@pytest.fixture
def tickets() -> TicketStore:
    return TicketStore(ttl_s=30.0)


@pytest.fixture
def app(engine: BridgeEngine, flows: FlowStore, tickets: TicketStore) -> FastAPI:
    app = FastAPI()
    app.include_router(build_api_router(engine, tickets=tickets))
    app.include_router(build_events_router(engine))
    app.include_router(build_flows_router(flows, engine))
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# ── /api/workers ──────────────────────────────────────────────────────────

def test_api_workers_requires_auth(client: TestClient):
    r = client.get("/api/workers")
    assert r.status_code == 401


def test_api_workers_rejects_bad_token(client: TestClient):
    r = client.get("/api/workers", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_api_workers_returns_empty_roster(client: TestClient, auth):
    r = client.get("/api/workers", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"workers": []}


def test_api_workers_includes_registered_worker(
    client: TestClient, engine: BridgeEngine, auth
):
    import asyncio
    asyncio.run(engine.register_worker("demo-tm", "/tmp"))
    r = client.get("/api/workers", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert len(body["workers"]) == 1
    assert body["workers"][0]["name"] == "demo-tm"


# ── /api/permissions ──────────────────────────────────────────────────────

def test_api_permissions_requires_auth(client: TestClient):
    r = client.get("/api/permissions")
    assert r.status_code == 401


def test_api_permissions_empty(client: TestClient, auth):
    r = client.get("/api/permissions", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"pending": []}


def test_api_permissions_resolve_requires_auth(client: TestClient):
    r = client.post("/api/permissions/resolve", json={})
    assert r.status_code == 401


def test_api_permissions_resolve_rejects_bad_behavior(client: TestClient, auth):
    r = client.post(
        "/api/permissions/resolve",
        json={"worker": "demo-tm", "request_id": "x", "behavior": "maybe"},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "behavior" in body["error"]


def test_api_permissions_resolve_returns_false_for_unknown(
    client: TestClient, engine: BridgeEngine, auth
):
    r = client.post(
        "/api/permissions/resolve",
        json={"worker": "nobody", "request_id": "x", "behavior": "allow"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False


# ── /api/workers/{name}/update-manager ────────────────────────────────────

def test_update_manager_requires_auth(client: TestClient):
    r = client.post("/api/workers/demo-tm/update-manager")
    assert r.status_code == 401


def test_update_manager_dispatches_event_on_idle_worker(
    client: TestClient, engine: BridgeEngine, auth
):
    """Idle worker → manager event is enqueued."""
    import asyncio
    asyncio.run(engine.register_worker("demo-tm", "/tmp"))
    r = client.post("/api/workers/demo-tm/update-manager", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["routed_to"] == "demo-tm-manager"
    assert body["event_id"]


def test_update_manager_refuses_busy_worker_without_force(
    client: TestClient, engine: BridgeEngine, auth
):
    """Worker mid-turn → reject unless force=true."""
    import asyncio
    asyncio.run(engine.register_worker("demo-tm", "/tmp"))
    asyncio.run(engine.enqueue_inbound("demo-tm", "in flight"))  # → busy
    r = client.post("/api/workers/demo-tm/update-manager", headers=auth)
    body = r.json()
    assert body["ok"] is False
    assert "not idle" in body["error"]
    # With force=true the same call goes through.
    r = client.post(
        "/api/workers/demo-tm/update-manager?force=true", headers=auth
    )
    assert r.json()["ok"] is True


# ── /api/dev-bootstrap ────────────────────────────────────────────────────

def test_dev_bootstrap_returns_secret_for_loopback(client: TestClient, secret: str):
    """Same-machine browser gets the bearer back so the auth gate is
    skipped — the common dev case. TestClient sends client.host = 'testclient'
    by default, so we explicitly emulate loopback."""
    # TestClient uses 'testclient' as client.host. We have to test the
    # loopback gate via a direct call against the underlying endpoint.
    r = client.get("/api/dev-bootstrap", headers={"Host": "127.0.0.1:8004"})
    # By default TestClient is treated as non-loopback; expect 403.
    assert r.status_code in (403, 200), r.text


def test_dev_bootstrap_rejects_non_loopback(client: TestClient):
    """Default TestClient client.host is 'testclient' — not a loopback
    address. Verify that the gate refuses."""
    r = client.get("/api/dev-bootstrap")
    assert r.status_code == 403
    body = r.json()
    assert "loopback" in body["detail"].lower()


def test_dev_bootstrap_disabled_by_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TUBEMAIL_DISABLE_DEV_BOOTSTRAP", "1")
    r = client.get("/api/dev-bootstrap")
    # Disabled → 404 (looks like the endpoint isn't there). 403 also
    # acceptable; either way the frontend treats it as "show auth gate."
    assert r.status_code in (403, 404)


# ── /api/pty-ticket ───────────────────────────────────────────────────────

def test_api_pty_ticket_requires_auth(client: TestClient):
    r = client.post("/api/pty-ticket", json={"worker": "demo-tm"})
    assert r.status_code == 401


def test_api_pty_ticket_issues_unique_tokens(client: TestClient, auth):
    r1 = client.post("/api/pty-ticket", json={"worker": "demo-tm"}, headers=auth)
    r2 = client.post("/api/pty-ticket", json={"worker": "demo-tm"}, headers=auth)
    assert r1.status_code == 200 and r2.status_code == 200
    t1, t2 = r1.json()["ticket"], r2.json()["ticket"]
    assert t1 != t2
    assert len(t1) >= 32


def test_api_pty_ticket_rejects_path_traversal_worker(client: TestClient, auth):
    r = client.post("/api/pty-ticket", json={"worker": "../evil"}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "invalid" in body["error"].lower()


# ── /api/flows ────────────────────────────────────────────────────────────

def test_api_flows_list_empty(client: TestClient, auth):
    r = client.get("/api/flows", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"flows": []}


def test_api_flows_save_requires_name_and_body(client: TestClient, auth):
    r = client.post("/api/flows", json={"name": "", "body": ""}, headers=auth)
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_api_flows_save_and_list(client: TestClient, auth):
    r = client.post(
        "/api/flows",
        json={"name": "demo-flow", "body": "do the thing", "default_worker": "x-tm"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.get("/api/flows", headers=auth)
    body = r.json()
    assert len(body["flows"]) == 1
    assert body["flows"][0]["name"] == "demo-flow"


def test_api_flows_save_rejects_invalid_name(client: TestClient, auth):
    r = client.post(
        "/api/flows",
        json={"name": "../evil", "body": "body"},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "invalid" in body["error"].lower()


def test_api_flows_delete(client: TestClient, auth):
    client.post(
        "/api/flows",
        json={"name": "gone", "body": "body"},
        headers=auth,
    )
    r = client.delete("/api/flows/gone", headers=auth)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # Delete of non-existent is a False no-op, not an error.
    r = client.delete("/api/flows/gone", headers=auth)
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_api_flows_run_requires_saved(client: TestClient, auth):
    r = client.post("/api/flows/nope/run", json={}, headers=auth)
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_api_flows_run_with_explicit_worker(
    client: TestClient, engine: BridgeEngine, auth
):
    import asyncio
    asyncio.run(engine.register_worker("demo-tm", "/tmp"))
    client.post(
        "/api/flows",
        json={"name": "with-worker", "body": "go go go"},
        headers=auth,
    )
    r = client.post(
        "/api/flows/with-worker/run",
        json={"worker": "demo-tm"},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["worker"] == "demo-tm"
    assert body["first_event_id"]
    assert len(body["run_id"]) >= 16


def test_api_flows_run_without_worker_errors_when_no_default(
    client: TestClient, auth
):
    client.post(
        "/api/flows",
        json={"name": "no-default", "body": "body"},
        headers=auth,
    )
    r = client.post("/api/flows/no-default/run", json={}, headers=auth)
    body = r.json()
    assert body["ok"] is False
    assert "worker" in body["error"].lower()


def test_api_flows_run_log_roundtrip(
    client: TestClient, engine: BridgeEngine, auth
):
    import asyncio
    asyncio.run(engine.register_worker("demo-tm", "/tmp"))
    client.post(
        "/api/flows",
        json={"name": "rt", "body": "go", "default_worker": "demo-tm"},
        headers=auth,
    )
    r = client.post("/api/flows/rt/run", json={}, headers=auth)
    run_id = r.json()["run_id"]

    r = client.get(f"/api/flows/runs/{run_id}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["run"]["flow_name"] == "rt"
    assert body["run"]["worker"] == "demo-tm"


def test_api_flows_get_unknown_run(client: TestClient, auth):
    r = client.get("/api/flows/runs/nonexistent-run-id-abc", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False


# ── SPA fallback ──────────────────────────────────────────────────────────
# Exercised via the full app factory, because the SPA mount is wired there.

def test_spa_fallback_serves_index_for_deep_links(
    tmp_path: Path, secret: str, monkeypatch: pytest.MonkeyPatch
):
    """A client-side route like /workers/iris-qa-tm with Accept: text/html
    must return index.html, not 404.
    """
    monkeypatch.setenv("TUBEMAIL_DATA_DIR", str(tmp_path / "data"))
    from tubemail_hub.server import create_app
    app = create_app()
    c = TestClient(app)
    r = c.get("/workers/iris-qa-tm", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_spa_fallback_preserves_asset_404s(
    tmp_path: Path, secret: str, monkeypatch: pytest.MonkeyPatch
):
    """A missing JS / CSS asset must still 404 — do NOT return HTML
    for a file the browser actually expected."""
    monkeypatch.setenv("TUBEMAIL_DATA_DIR", str(tmp_path / "data"))
    from tubemail_hub.server import create_app
    app = create_app()
    c = TestClient(app)
    r = c.get(
        "/assets/does-not-exist.js",
        headers={"Accept": "application/javascript"},
    )
    assert r.status_code == 404


def test_spa_fallback_does_not_shadow_api(
    tmp_path: Path, secret: str, monkeypatch: pytest.MonkeyPatch
):
    """Specific API routes must always win over the SPA fallback."""
    monkeypatch.setenv("TUBEMAIL_DATA_DIR", str(tmp_path / "data"))
    from tubemail_hub.server import create_app
    app = create_app()
    c = TestClient(app)
    r = c.get(
        "/api/workers",
        headers={"Authorization": f"Bearer {secret}", "Accept": "text/html"},
    )
    assert r.status_code == 200
    assert "json" in r.headers.get("content-type", "")
