"""Tests for the tubemail HTTP router."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubemail_hub.bridge.engine import BridgeEngine
from tubemail_hub.bridge.http import build_tubemail_router


@pytest.fixture
def secret() -> str:
    return "test-secret-abc123"


@pytest.fixture(autouse=True)
def set_secret(secret: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TUBEMAIL_SECRET", secret)


@pytest.fixture
def app_and_engine(tmp_path: Path) -> tuple[FastAPI, BridgeEngine]:
    engine = BridgeEngine(data_dir=tmp_path)
    app = FastAPI()
    app.include_router(build_tubemail_router(engine))
    return app, engine


@pytest.fixture
def client(app_and_engine) -> TestClient:
    app, _ = app_and_engine
    return TestClient(app)


@pytest.fixture
def auth_headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def test_register_requires_auth(client: TestClient):
    resp = client.post("/tubemail/w/register", json={"cwd": "/tmp"})
    assert resp.status_code == 401


def test_register_with_bad_token(client: TestClient):
    resp = client.post(
        "/tubemail/w/register",
        json={"cwd": "/tmp"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_register_success(client: TestClient, auth_headers):
    resp = client.post(
        "/tubemail/leanspecs/register",
        json={"cwd": "/src/leanspecs"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["worker"] == "leanspecs"
    assert body["cursor"] == ""


def test_unregister(client: TestClient, auth_headers):
    client.post(
        "/tubemail/w/register",
        json={"cwd": "/tmp"},
        headers=auth_headers,
    )
    resp = client.post("/tubemail/w/unregister", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_outbound_records_event(client: TestClient, auth_headers, app_and_engine):
    _, engine = app_and_engine
    client.post(
        "/tubemail/w/register",
        json={"cwd": "/tmp"},
        headers=auth_headers,
    )
    resp = client.post(
        "/tubemail/w/outbound",
        json={"text": "hello from worker", "meta": {"progress": 50}},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    events = engine.events_since("w")
    assert len(events) == 1
    assert events[0].kind == "outbound"
    assert events[0].content == "hello from worker"
    assert events[0].meta == {"progress": 50}


def test_permission_request_recorded(client: TestClient, auth_headers, app_and_engine):
    _, engine = app_and_engine
    client.post(
        "/tubemail/w/register",
        json={"cwd": "/tmp"},
        headers=auth_headers,
    )
    resp = client.post(
        "/tubemail/w/permission-request",
        json={
            "request_id": "abcde",
            "tool_name": "Bash",
            "description": "run pytest",
            "input_preview": "pytest tests/",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    pending = engine.list_pending_permissions()
    assert len(pending) == 1
    assert pending[0]["request_id"] == "abcde"
    assert pending[0]["tool_name"] == "Bash"


def test_missing_secret_returns_503(client: TestClient, monkeypatch):
    monkeypatch.delenv("TUBEMAIL_SECRET", raising=False)
    resp = client.post(
        "/tubemail/w/register",
        json={"cwd": "/tmp"},
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 503
