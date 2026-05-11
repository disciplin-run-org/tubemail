"""Tests for the orchestrator MCP tool layer."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import FastMCP

from tubemail_hub.bridge.engine import BridgeEngine, _new_event_id
from tubemail_hub.bridge.models import PermissionRequestPayload, WorkerEvent
from tubemail_hub.tools import workers as workers_tools


def _append_outbound(engine: BridgeEngine, worker: str, content: str = "moved on") -> None:
    """Append a synthetic outbound event without going through
    `record_outbound`. Tests of the explicit sweep admin tool use this
    to avoid `record_outbound`'s auto-sweep claiming the drop first.
    The auto-sweep behavior is pinned by tests in test_bridge_engine.py.
    """
    import time as _t
    ws = engine._workers[worker]
    ws.events.append(WorkerEvent(
        event_id=_new_event_id(),
        ts=_t.time(),
        kind="outbound",
        content=content,
        meta={},
    ))
    ws.last_activity = ws.events[-1].ts


@pytest.fixture
async def mcp_and_engine(tmp_path: Path):
    engine = BridgeEngine(data_dir=tmp_path)
    mcp = FastMCP("tubemail-test")
    workers_tools.register(mcp, engine)
    return mcp, engine


async def _call(mcp: FastMCP, name: str, **kwargs):
    """Call a registered tool by name and return its ToolResult."""
    return await mcp.call_tool(name, kwargs)


async def test_list_workers_shows_ecosystem(mcp_and_engine):
    """Even with no connected workers, ecosystem projects always appear."""
    mcp, _ = mcp_and_engine
    result = await _call(mcp, "tm_list_workers")
    text = result.structured_content["result"]
    for project in ("leanspecs", "iris-qa", "architrix", "actuatrix", "tubemail"):
        assert project in text
    assert "not started" in text


async def test_list_workers_groups_roles_under_project(mcp_and_engine):
    """Two role-scoped workers in the same project render as a grouped tree."""
    mcp, engine = mcp_and_engine
    await engine.register_worker(
        "leanspecs-code-tm", "/home/jesper/PycharmProjects/ai-agents/leanspecs"
    )
    await engine.register_worker(
        "leanspecs-spec-tm", "/home/jesper/PycharmProjects/ai-agents/leanspecs"
    )
    result = await _call(mcp, "tm_list_workers")
    text = result.structured_content["result"]
    # Project header with role count
    assert "leanspecs" in text
    assert "2 roles" in text
    # Both roles rendered with tree branches
    assert "├─ " in text
    assert "└─ " in text
    assert "leanspecs-code-tm" in text
    assert "leanspecs-spec-tm" in text


async def test_list_workers_single_worker_not_grouped(mcp_and_engine):
    """A project with one worker renders as a normal row, not a grouped
    tree."""
    mcp, engine = mcp_and_engine
    await engine.register_worker(
        "leanspecs-tm", "/home/jesper/PycharmProjects/ai-agents/leanspecs"
    )
    result = await _call(mcp, "tm_list_workers")
    text = result.structured_content["result"]
    assert "leanspecs-tm" in text
    assert "2 roles" not in text
    assert "├─" not in text


async def test_send_and_receive(mcp_and_engine):
    mcp, engine = mcp_and_engine
    await engine.register_worker("w", "/tmp")
    send_result = await _call(mcp, "tm_send", worker="w", message="ping")
    event_id = send_result.structured_content["event_id"]
    assert event_id
    recv_result = await _call(mcp, "tm_receive", worker="w")
    events = recv_result.structured_content["result"]
    assert len(events) == 1
    assert events[0]["content"] == "ping"
    assert events[0]["kind"] == "inbound"


async def test_receive_with_cursor(mcp_and_engine):
    mcp, engine = mcp_and_engine
    await engine.register_worker("w", "/tmp")
    first = await _call(mcp, "tm_send", worker="w", message="a")
    cursor = first.structured_content["event_id"]
    await _call(mcp, "tm_send", worker="w", message="b")
    await _call(mcp, "tm_send", worker="w", message="c")
    recv = await _call(mcp, "tm_receive", worker="w", since=cursor)
    events = recv.structured_content["result"]
    assert [e["content"] for e in events] == ["b", "c"]


async def test_status_unknown_worker(mcp_and_engine):
    mcp, _ = mcp_and_engine
    result = await _call(mcp, "tm_status", worker="nonexistent")
    assert result.structured_content["state"] == "unknown"


async def test_status_idle(mcp_and_engine):
    mcp, engine = mcp_and_engine
    await engine.register_worker("w", "/tmp")
    result = await _call(mcp, "tm_status", worker="w")
    assert result.structured_content["state"] == "idle"
    assert result.structured_content["pending_count"] == 0


async def test_status_waiting_permission(mcp_and_engine):
    mcp, engine = mcp_and_engine
    await engine.register_worker("w", "/tmp")
    await engine.record_permission_request(
        "w",
        PermissionRequestPayload(request_id="abcde", tool_name="Bash"),
    )
    result = await _call(mcp, "tm_status", worker="w")
    assert result.structured_content["state"] == "waiting_permission"
    assert result.structured_content["pending_count"] == 1


async def test_pending_and_respond_permission(mcp_and_engine):
    mcp, engine = mcp_and_engine
    await engine.register_worker("w", "/tmp")
    await engine.record_permission_request(
        "w",
        PermissionRequestPayload(
            request_id="abcde",
            tool_name="Bash",
            description="run tests",
            input_preview="pytest",
        ),
    )
    pending = await _call(mcp, "tm_pending_permissions")
    entries = pending.structured_content["result"]
    assert len(entries) == 1
    assert entries[0]["request_id"] == "abcde"

    resp = await _call(
        mcp, "tm_respond_permission", worker="w", request_id="abcde", behavior="allow"
    )
    assert resp.structured_content["ok"] is True

    pending2 = await _call(mcp, "tm_pending_permissions")
    assert pending2.structured_content["result"] == []


async def test_respond_permission_invalid_behavior_rejected_by_schema(mcp_and_engine):
    """Literal type on `behavior` must reject unknown values at the schema
    layer."""
    mcp, _ = mcp_and_engine
    with pytest.raises(Exception):
        await _call(
            mcp,
            "tm_respond_permission",
            worker="w",
            request_id="abcde",
            behavior="maybe",
        )


async def test_interrupt(mcp_and_engine):
    mcp, engine = mcp_and_engine
    await engine.register_worker("w", "/tmp")
    result = await _call(mcp, "tm_interrupt", worker="w")
    assert result.structured_content["ok"] is True
    events = engine.events_since("w")
    assert len(events) == 1
    assert events[0].kind == "interrupt"


async def test_get_instructions_returns_server_instructions(mcp_and_engine):
    """Meta-tool: AI can re-read the server's workflow doc after compaction."""
    mcp, _ = mcp_and_engine
    result = await _call(mcp, "get_instructions")
    text = result.structured_content["result"]
    assert "TubeMail" in text
    assert "tm_send" in text
    assert "tm_list_workers" in text


async def test_all_tools_have_distinct_prefixed_names(mcp_and_engine):
    """Tool-selection test: every orchestration tool is tm_-prefixed so it
    doesn't collide with tools from other MCP servers loaded alongside us.
    Meta-tools (get_instructions, refresh_tools) are the two allowed exceptions.

    The exact count isn't the point — the invariant is "every domain tool
    starts with tm_" and "the meta-tools are present". A lower bound catches
    accidental deletion of the entire tool layer."""
    mcp, _ = mcp_and_engine
    tools = await mcp.list_tools()
    names = sorted(t.name for t in tools)
    meta_tools = {"get_instructions", "refresh_tools"}
    domain_tools = [n for n in names if n not in meta_tools]
    assert len(domain_tools) >= 12, f"fewer than 12 domain tools: {domain_tools}"
    for name in domain_tools:
        assert name.startswith("tm_"), f"domain tool {name!r} missing tm_ prefix"
    # Ensure meta-tools are present
    assert meta_tools.issubset(
        set(names)
    ), f"missing meta-tools: {meta_tools - set(names)}"


async def test_tool_descriptions_distinguish_send_from_receive(mcp_and_engine):
    """Tool-selection sanity: descriptions make tm_send vs tm_receive
    obviously distinct so an agent picks correctly."""
    mcp, _ = mcp_and_engine
    tools = {t.name: t for t in await mcp.list_tools()}
    send_desc = (tools["tm_send"].description or "").lower()
    recv_desc = (tools["tm_receive"].description or "").lower()
    assert "send" in send_desc or "deliver" in send_desc
    assert "read" in recv_desc or "poll" in recv_desc
    # Each should name the verb the other lacks
    assert (
        "read" not in send_desc.split(".")[0]
    )  # first sentence of send isn't about reading


async def test_update_manager_refuses_when_not_idle(mcp_and_engine):
    """tm_update_manager must not fire /exit at a worker mid-permission-
    prompt."""
    mcp, engine = mcp_and_engine
    await engine.register_worker("w", "/")
    await engine.record_permission_request(
        "w",
        PermissionRequestPayload(request_id="abcde", tool_name="Bash"),
    )
    result = await _call(mcp, "tm_update_manager", worker="w")
    data = result.structured_content
    assert data.get("state") == "waiting_permission"
    assert "error" in data


async def test_update_manager_force_bypasses_idle_check(mcp_and_engine):
    """Force=True must proceed even when the worker is non-idle."""
    mcp, engine = mcp_and_engine
    await engine.register_worker("w", "/")
    await engine.record_permission_request(
        "w",
        PermissionRequestPayload(request_id="abcde", tool_name="Bash"),
    )
    result = await _call(mcp, "tm_update_manager", worker="w", force=True)
    data = result.structured_content
    assert "event_id" in data
    assert data.get("forced") is True


async def test_update_manager_refuses_unknown_worker(mcp_and_engine):
    mcp, _ = mcp_and_engine
    result = await _call(mcp, "tm_update_manager", worker="never-existed")
    data = result.structured_content
    assert data.get("state") == "unknown"
    assert "error" in data


async def test_clear_and_send_routes_both_events(mcp_and_engine):
    """Clear goes to <worker>-manager; message goes to <worker> timeline."""
    mcp, engine = mcp_and_engine
    await engine.register_worker("w", "/")
    result = await _call(
        mcp,
        "tm_clear_and_send",
        worker="w",
        message="new task",
        delay_s=0.01,
    )
    data = result.structured_content
    assert "clear_event_id" in data
    assert "message_event_id" in data
    assert data["routed_to"] == {"clear": "w-manager", "message": "w"}
    # Message lands on the worker's own timeline (not the manager's).
    worker_events = engine.events_since("w")
    assert any(e.content == "new task" for e in worker_events)
    # Clear lands on the manager timeline with kind=clear.
    manager_events = engine.events_since("w-manager")
    assert any(e.meta.get("kind") == "clear" for e in manager_events)


async def test_clear_and_send_refuses_when_not_idle(mcp_and_engine):
    """Same safety gate as tm_update_manager — don't /clear mid-permission."""
    mcp, engine = mcp_and_engine
    await engine.register_worker("w", "/")
    await engine.record_permission_request(
        "w",
        PermissionRequestPayload(request_id="abcde", tool_name="Bash"),
    )
    result = await _call(
        mcp,
        "tm_clear_and_send",
        worker="w",
        message="new task",
        delay_s=0.01,
    )
    data = result.structured_content
    assert data.get("state") == "waiting_permission"
    assert "error" in data


async def test_my_inbox_resolves_self_from_env(mcp_and_engine, monkeypatch):
    """tm_my_inbox reads TM_WORKER_NAME and returns the caller's timeline."""
    mcp, engine = mcp_and_engine
    await engine.register_worker("iris-qa-tm", "/")
    await engine.enqueue_inbound("iris-qa-tm", "work order A", {})
    await engine.enqueue_inbound("iris-qa-tm", "work order B", {})
    monkeypatch.setenv("TM_WORKER_NAME", "iris-qa-tm")
    result = await _call(mcp, "tm_my_inbox", limit=10)
    data = result.structured_content
    assert data["worker"] == "iris-qa-tm"
    contents = [e["content"] for e in data["events"] if e["kind"] == "inbound"]
    assert contents == ["work order A", "work order B"]


async def test_my_inbox_errors_when_env_missing(mcp_and_engine, monkeypatch):
    mcp, _ = mcp_and_engine
    monkeypatch.delenv("TM_WORKER_NAME", raising=False)
    result = await _call(mcp, "tm_my_inbox")
    data = result.structured_content
    assert "error" in data
    assert "TM_WORKER_NAME" in data["error"]


async def test_my_inbox_honors_limit(mcp_and_engine, monkeypatch):
    mcp, engine = mcp_and_engine
    await engine.register_worker("w", "/")
    for i in range(10):
        await engine.enqueue_inbound("w", f"msg {i}", {})
    monkeypatch.setenv("TM_WORKER_NAME", "w")
    result = await _call(mcp, "tm_my_inbox", limit=3)
    data = result.structured_content
    assert len(data["events"]) == 3
    # Latest 3 — "msg 7", "msg 8", "msg 9"
    assert [e["content"] for e in data["events"]] == ["msg 7", "msg 8", "msg 9"]


# ── tm_self_reconnect_mcp ────────────────────────────────────────────────


async def test_self_reconnect_mcp_errors_when_env_missing(
    mcp_and_engine,
    monkeypatch,
):
    """Without TM_WORKER_NAME the wrapper can't pick a worker — return
    error."""
    mcp, _ = mcp_and_engine
    monkeypatch.delenv("TM_WORKER_NAME", raising=False)
    result = await _call(mcp, "tm_self_reconnect_mcp", server="leanspecs")
    data = result.structured_content
    assert "error" in data
    assert "TM_WORKER_NAME" in data["error"]


async def test_sweep_stale_permissions_tool_reports_dropped_workers(
    mcp_and_engine,
):
    """The admin tool surfaces only workers where something was dropped,
    plus a `total` count. A clean fleet returns swept={}, total=0."""
    mcp, engine = mcp_and_engine
    await engine.register_worker("clean-tm", "/")
    await engine.register_worker("stuck-tm", "/")
    await engine.record_permission_request(
        "stuck-tm", PermissionRequestPayload(request_id="x", tool_name="Bash"),
    )
    for ev in engine._workers["stuck-tm"].events:
        if ev.kind == "permission_request":
            ev.ts -= 3600
    _append_outbound(engine, "stuck-tm")

    result = await _call(mcp, "tm_sweep_stale_permissions")
    data = result.structured_content
    assert data == {"swept": {"stuck-tm": 1}, "total": 1}


async def test_sweep_stale_permissions_tool_scopes_to_one_worker(
    mcp_and_engine,
):
    mcp, engine = mcp_and_engine
    await engine.register_worker("a", "/")
    await engine.register_worker("b", "/")
    for w in ("a", "b"):
        await engine.record_permission_request(
            w, PermissionRequestPayload(request_id=f"{w}-x", tool_name="Bash"),
        )
        for ev in engine._workers[w].events:
            if ev.kind == "permission_request":
                ev.ts -= 3600
        _append_outbound(engine, w)

    result = await _call(mcp, "tm_sweep_stale_permissions", worker="a")
    data = result.structured_content
    assert data == {"swept": {"a": 1}, "total": 1}
    # b was untouched.
    assert len(engine._workers["b"].pending_permissions) == 1


async def test_self_reconnect_mcp_routes_to_caller_manager(
    mcp_and_engine,
    monkeypatch,
):
    """tm_self_reconnect_mcp(server) must enqueue inbound on
    `<TM_WORKER_NAME>-manager` and return the manager's reply payload."""
    import asyncio
    import json

    mcp, engine = mcp_and_engine
    monkeypatch.setenv("TM_WORKER_NAME", "leanspecs-tm")
    await engine.register_worker("leanspecs-tm-manager", "/")

    async def post_manager_reply() -> None:
        # Wait briefly for the wrapper's inbound to be enqueued, then post
        # the manager's reconnect_mcp_result the way _run_reconnect_mcp would.
        await asyncio.sleep(0.05)
        await engine.record_outbound(
            "leanspecs-tm-manager",
            json.dumps({"ok": True, "server": "leanspecs", "detail": "reconnected"}),
            {"kind": "reconnect_mcp_result", "ok": True, "server": "leanspecs"},
        )

    asyncio.create_task(post_manager_reply())
    result = await _call(mcp, "tm_self_reconnect_mcp", server="leanspecs")
    data = result.structured_content
    assert data == {"ok": True, "server": "leanspecs", "detail": "reconnected"}

    # Inbound must have landed on the manager's timeline with the right meta.
    manager_events = engine.events_since("leanspecs-tm-manager")
    assert any(
        e.kind == "inbound"
        and e.meta.get("kind") == "reconnect_mcp:leanspecs"
        and e.content == "reconnect_mcp:leanspecs"
        for e in manager_events
    ), [{"kind": e.kind, "content": e.content, "meta": e.meta} for e in manager_events]


# ── Recording tools ──────────────────────────────────────────────────────


@pytest.fixture
async def mcp_engine_with_recorder(tmp_path: Path):
    """Engine with a real RecordingManager wired in."""
    from tubemail_hub.recorder import RecordingManager

    rec = RecordingManager(tmp_path / "rec")
    engine = BridgeEngine(data_dir=tmp_path / "engine", recorder=rec)
    mcp = FastMCP("tubemail-test")
    workers_tools.register(mcp, engine)
    return mcp, engine, rec


async def test_recording_toggle_unknown_worker(mcp_engine_with_recorder):
    mcp, _, _ = mcp_engine_with_recorder
    result = await _call(mcp, "tm_recording_toggle", worker="ghost", enabled=True)
    data = result.structured_content
    assert data["ok"] is False
    assert "unknown worker" in data["error"]


async def test_recording_toggle_starts_and_stops(mcp_engine_with_recorder):
    mcp, engine, rec = mcp_engine_with_recorder
    await engine.register_worker("w", "/")

    on = await _call(mcp, "tm_recording_toggle", worker="w", enabled=True)
    assert on.structured_content["enabled"] is True
    assert rec.is_recording("w") is True

    off = await _call(mcp, "tm_recording_toggle", worker="w", enabled=False)
    assert off.structured_content["enabled"] is False
    assert rec.is_recording("w") is False


async def test_recording_status_shows_no_files_until_write(mcp_engine_with_recorder):
    mcp, engine, _ = mcp_engine_with_recorder
    await engine.register_worker("w", "/")
    await _call(mcp, "tm_recording_toggle", worker="w", enabled=True)
    result = await _call(mcp, "tm_recording_status", worker="w")
    data = result.structured_content
    assert data["enabled"] is True
    # Active file is open but no writes yet.
    assert data["active_file"] is not None
    assert len(data["files"]) == 1


async def test_get_recording_returns_frames(mcp_engine_with_recorder):
    mcp, engine, rec = mcp_engine_with_recorder
    await engine.register_worker("w", "/")
    await _call(mcp, "tm_recording_toggle", worker="w", enabled=True)
    rec.write("w", b"hello world\n")
    rec.write("w", b"second line\n")

    result = await _call(mcp, "tm_get_recording", worker="w", limit=10)
    data = result.structured_content
    assert data["truncated"] is False
    deltas = [f["delta"] for f in data["frames"]]
    assert any("hello world" in d for d in deltas)
    assert any("second line" in d for d in deltas)


async def test_get_recording_grep_filters(mcp_engine_with_recorder):
    mcp, engine, rec = mcp_engine_with_recorder
    await engine.register_worker("w", "/")
    await _call(mcp, "tm_recording_toggle", worker="w", enabled=True)
    rec.write("w", b"permission requested: Bash\n")
    rec.write("w", b"streaming token foo\n")
    rec.write("w", b"another permission requested: Edit\n")

    result = await _call(mcp, "tm_get_recording", worker="w", grep="permission")
    data = result.structured_content
    assert len(data["frames"]) == 2
    assert all("permission" in f["delta"] for f in data["frames"])


async def test_get_recording_truncated_flag(mcp_engine_with_recorder):
    mcp, engine, rec = mcp_engine_with_recorder
    await engine.register_worker("w", "/")
    await _call(mcp, "tm_recording_toggle", worker="w", enabled=True)
    for i in range(10):
        rec.write("w", f"line {i}\n".encode())
    result = await _call(mcp, "tm_get_recording", worker="w", limit=3)
    data = result.structured_content
    assert data["truncated"] is True
    assert len(data["frames"]) == 3
