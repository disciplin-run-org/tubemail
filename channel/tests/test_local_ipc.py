"""Unit tests for the local Unix-domain socket bridge between the channel
plugin and the manager.

The manager-side server runs on threads; the channel-side client is
asyncio. Tests exercise both halves end-to-end against real Unix sockets
in a tmp_path so we never accidentally pass thanks to a mock.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

from tubemail.local_ipc import (
    SOCK_ENV,
    LocalIPCError,
    LocalIPCServer,
    default_sock_path,
    request,
)

# ── default_sock_path ────────────────────────────────────────────────────


def test_default_sock_path_uses_session_name():
    assert default_sock_path("leanspecs-tm") == "/tmp/tubemail-leanspecs-tm.sock"


# ── LocalIPCServer ──────────────────────────────────────────────────────


@pytest.fixture
def sock_path(tmp_path) -> str:
    # Sockets in tmp_path keep tests parallel-safe and self-cleaning.
    return str(tmp_path / "ipc.sock")


def _start_server(sock_path: str, handler) -> LocalIPCServer:
    s = LocalIPCServer(sock_path, handler)
    s.start()
    # Give the accept thread a moment to enter its loop on slow CI.
    time.sleep(0.05)
    return s


def test_server_socket_file_has_owner_only_mode(sock_path):
    server = _start_server(sock_path, lambda req: {"ok": True})
    try:
        st = os.stat(sock_path)
        assert st.st_mode & 0o777 == 0o600
    finally:
        server.stop()


def test_server_unlinks_stale_socket_on_start(tmp_path):
    sock_path = str(tmp_path / "stale.sock")
    # Drop a regular file at the path — simulates a previous manager
    # crashing before it could clean up.
    open(sock_path, "wb").close()
    server = _start_server(sock_path, lambda req: {"ok": True})
    try:
        # If the start path didn't unlink the stale file, the bind would
        # have raised — assert it's now a real socket by connecting.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(sock_path)
    finally:
        server.stop()


def test_server_stop_unlinks_socket_file(sock_path):
    server = _start_server(sock_path, lambda req: {"ok": True})
    assert os.path.exists(sock_path)
    server.stop()
    # Best-effort unlink — should be gone now.
    assert not os.path.exists(sock_path)


def _sync_request(sock_path: str, payload: dict) -> dict:
    """Synchronous client used by manager-side tests; mirrors the wire protocol
    the async client uses so we can verify the server in isolation from
    asyncio."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
        c.settimeout(5.0)
        c.connect(sock_path)
        c.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while True:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in chunk:
                break
        return json.loads(buf.split(b"\n", 1)[0])


def test_server_dispatches_request_to_handler(sock_path):
    received = []

    def handler(req: dict) -> dict:
        received.append(req)
        return {"ok": True, "echo": req["data"]}

    server = _start_server(sock_path, handler)
    try:
        resp = _sync_request(sock_path, {"action": "x", "data": 42})
        assert resp == {"ok": True, "echo": 42}
        assert received == [{"action": "x", "data": 42}]
    finally:
        server.stop()


def test_server_returns_error_on_invalid_json(sock_path):
    server = _start_server(sock_path, lambda req: {"ok": True})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(5.0)
            c.connect(sock_path)
            c.sendall(b"not json at all\n")
            line = c.recv(4096)
        resp = json.loads(line.decode())
        assert resp["ok"] is False
        assert "invalid json" in resp["error"]
    finally:
        server.stop()


def test_server_catches_handler_exception(sock_path):
    def handler(req: dict) -> dict:
        raise RuntimeError("oops")

    server = _start_server(sock_path, handler)
    try:
        resp = _sync_request(sock_path, {"action": "x"})
        assert resp["ok"] is False
        assert "handler exception" in resp["error"]
    finally:
        server.stop()


def test_server_handles_concurrent_clients(sock_path):
    """Each accepted connection runs in its own thread so a slow handler does
    not block the next client.

    Five parallel clients must all get served within a short window.
    """
    barrier = threading.Barrier(5)

    def handler(req: dict) -> dict:
        # Serialise everyone at the barrier so we only succeed if the
        # accept loop is dispatching to per-connection threads.
        barrier.wait(timeout=2.0)
        return {"ok": True, "id": req["id"]}

    server = _start_server(sock_path, handler)
    results: list[dict] = []
    lock = threading.Lock()

    def client(idx: int) -> None:
        resp = _sync_request(sock_path, {"action": "x", "id": idx})
        with lock:
            results.append(resp)

    try:
        threads = [
            threading.Thread(target=client, args=(i,), daemon=True) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)
        assert len(results) == 5
        assert {r["id"] for r in results} == {0, 1, 2, 3, 4}
    finally:
        server.stop()


# ── Async client ────────────────────────────────────────────────────────


async def test_request_round_trips_against_real_server(sock_path):
    def handler(req: dict) -> dict:
        return {"ok": True, "got": req["server"], "request_id": req["request_id"]}

    server = _start_server(sock_path, handler)
    try:
        resp = await request(
            sock_path,
            {"action": "reconnect_mcp", "server": "leanspecs", "request_id": "rid"},
        )
        assert resp["ok"] is True
        assert resp["got"] == "leanspecs"
        assert resp["request_id"] == "rid"
    finally:
        server.stop()


async def test_request_raises_when_socket_missing(tmp_path):
    sock_path = str(tmp_path / "nope.sock")
    with pytest.raises(LocalIPCError, match="socket file missing"):
        await request(sock_path, {"action": "x"})


async def test_request_times_out_when_server_never_replies(sock_path):
    """If a buggy handler never replies, the client must give up at timeout_s
    and raise — the channel falls back to the hub on LocalIPCError."""

    def handler(req: dict) -> dict:
        time.sleep(2.0)  # Longer than the test's timeout below.
        return {"ok": True}

    server = _start_server(sock_path, handler)
    try:
        with pytest.raises(LocalIPCError, match="did not reply"):
            await request(sock_path, {"action": "x"}, timeout_s=0.3)
    finally:
        server.stop()


# ── Env-var contract ───────────────────────────────────────────────────


def test_sock_env_constant_matches_documented_name():
    # Manager exports this and channel reads it; both must use the same
    # string. Pin it so a typo on either side fails this test.
    assert SOCK_ENV == "TUBEMAIL_LOCAL_SOCK"
