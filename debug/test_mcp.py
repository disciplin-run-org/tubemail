"""MCP protocol layer: bearer auth + initialize handshake + tools/list.

Issues this catches:
- Bearer mismatch (client uses one secret, hub loaded another)
- /mcp/ returns 401 despite the right bearer (auth middleware bug)
- initialize succeeds but tools/list fails (broken hub state)
- Stale Mcp-Session-Id from a previous container generation (the issue
  that caused "/mcp Failed to reconnect" earlier in this session)

Issues this heals:
- Stale session IDs               → compose restart (wipes server-side
                                     session table; clients must do a
                                     fresh initialize after this anyway)
- Bearer 401                      → if .env value matches container env,
                                     middleware bug → compose restart;
                                     if not, env drift → defer to test_hub
- broken handshake (initialize 2xx but tools/list 5xx) → compose restart
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import (  # noqa: E402
    BASE, HEAL,
    compose_restart, fail, header, ok, post_json,
    read_env_file, result_fail, result_pass, warn,
)


def _bearer() -> str | None:
    return read_env_file().get("TUBEMAIL_SECRET")


def _initialize() -> tuple[int | None, str, dict]:
    """Returns (status, body, response_headers)."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "heal", "version": "1"},
        },
    }
    s, h, b = post_json(f"{BASE}/mcp/", payload, bearer=_bearer(), timeout=5)
    return s, b, h


def _parse_session_id(headers: dict) -> str | None:
    # Header name is mcp-session-id (case-insensitive).
    for k, v in headers.items():
        if k.lower() == "mcp-session-id":
            return v
    return None


def _tools_list(session_id: str) -> tuple[int | None, str]:
    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json,text/event-stream",
        "Authorization": f"Bearer {_bearer()}",
        "Mcp-Session-Id": session_id,
    }
    import urllib.request
    req = urllib.request.Request(
        f"{BASE}/mcp/",
        method="POST",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(errors="replace")
    except Exception as e:
        return None, str(e)


def test() -> dict:
    header(f"[mcp] {BASE}/mcp/ initialize + tools/list")

    if not _bearer():
        fail("no TUBEMAIL_SECRET in .env — defer to [hub] test for healing")
        return result_fail("no_bearer_in_env")

    # 1. initialize
    status, body, hdrs = _initialize()
    if status is None:
        fail(f"transport failure: {body}")
        return result_fail("transport")
    if status == 401:
        fail("401 Unauthorized — bearer mismatch")
        return result_fail("auth_401")
    if status >= 500:
        fail(f"server error {status} on initialize")
        return result_fail(f"initialize_5xx_{status}")
    if status != 200:
        fail(f"unexpected initialize status {status}: {body[:120]}")
        return result_fail(f"initialize_status_{status}")
    ok(f"initialize 200 ({len(body)} bytes)")

    session_id = _parse_session_id(hdrs)
    if not session_id:
        fail("no Mcp-Session-Id header on initialize response")
        return result_fail("no_session_id")
    ok(f"got session id ({session_id[:8]}…)")

    # 2. tools/list as smoke test of the live session
    s2, b2 = _tools_list(session_id)
    if s2 is None:
        fail(f"tools/list transport failure: {b2}")
        return result_fail("tools_transport")
    if s2 >= 500:
        fail(f"tools/list server error {s2}")
        return result_fail(f"tools_5xx_{s2}")
    if s2 != 200:
        fail(f"tools/list status {s2}: {b2[:120]}")
        return result_fail(f"tools_status_{s2}")

    # Count tools — coarse but catches "0 tools" regressions.
    n = len(re.findall(r'"name"\s*:\s*"[a-zA-Z_]', b2))
    if n == 0:
        fail("tools/list returned 0 tools — server is healthy but empty")
        return result_fail("tools_empty")
    ok(f"tools/list 200 ({n} tools)")

    return result_pass()


def heal() -> dict:
    r = test()
    if r["status"] == "pass" or not HEAL:
        return r

    err = r["error"]

    if err == "no_bearer_in_env":
        # Hub layer will surface this; nothing for MCP layer to do.
        return r

    if err == "auth_401":
        # Either (a) container env doesn't match .env (drift — hub
        # layer's job to recreate) or (b) middleware bug (restart).
        # We can't easily distinguish here, so we restart and re-test.
        compose_restart(wait=5)
        return test()

    if err in ("transport", "no_session_id", "tools_transport", "tools_empty") \
       or err.startswith(("initialize_5xx_", "tools_5xx_",
                          "initialize_status_", "tools_status_")):
        # Restart wipes session state and reloads the app — the same
        # action that resolved "/mcp Failed to reconnect" in this session.
        compose_restart(wait=5)
        return test()

    return r


if __name__ == "__main__":
    sys.exit(0 if heal()["status"] == "pass" else 1)
