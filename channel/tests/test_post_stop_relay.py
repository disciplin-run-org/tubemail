"""Tests for the Stop hook relay script.

The script reads a Claude Code Stop event JSON from stdin, extracts
the assistant's last message text, and POSTs to tubemail's
/{worker}/outbound endpoint. The relay is best-effort — the script
must NEVER block the stop, so it exits 0 even on failures.

Tests cover:
- inline `messages` array shape
- transcript_path on disk shape
- multi-block content (text + thinking)
- missing TM_WORKER_NAME → silent skip
- HTTP failure → still exits 0
"""

from __future__ import annotations

# Standard Libraries
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

# 3rd party
import pytest


HOOK_SCRIPT = Path(__file__).resolve().parents[1] / "hooks" / "post_stop_relay.py"


def _run_hook(stdin: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Invoke the hook script as a subprocess. Mirrors the way Claude Code
    fires hooks — stdin JSON, env vars from the parent session."""
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_silent_skip_when_no_worker_name(tmp_path: Path):
    """Outside a tubemail session — TM_WORKER_NAME unset — exit silently."""
    payload = json.dumps({"messages": [{"role": "assistant", "content": "hi"}]})
    proc = _run_hook(payload, {"PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0
    # No HTTP attempt → no error in stderr.
    assert "TUBEMAIL_SECRET" not in proc.stderr


def test_silent_skip_when_no_secret(tmp_path: Path):
    """TM_WORKER_NAME set but no TUBEMAIL_SECRET — log and exit 0."""
    payload = json.dumps({"messages": [{"role": "assistant", "content": "hi"}]})
    proc = _run_hook(
        payload,
        {"PATH": "/usr/bin:/bin", "TM_WORKER_NAME": "test-tm"},
    )
    assert proc.returncode == 0
    assert "TUBEMAIL_SECRET" in proc.stderr


def test_silent_skip_when_stdin_empty(tmp_path: Path):
    """Empty stdin → just exit 0 without doing anything."""
    proc = _run_hook(
        "",
        {
            "PATH": "/usr/bin:/bin",
            "TM_WORKER_NAME": "test-tm",
            "TUBEMAIL_SECRET": "secret123",
        },
    )
    assert proc.returncode == 0


def test_silent_skip_when_stdin_invalid_json(tmp_path: Path):
    proc = _run_hook(
        "not json at all",
        {
            "PATH": "/usr/bin:/bin",
            "TM_WORKER_NAME": "test-tm",
            "TUBEMAIL_SECRET": "secret123",
        },
    )
    assert proc.returncode == 0
    assert "stdin not JSON" in proc.stderr


def test_silent_skip_when_no_assistant_text(tmp_path: Path):
    """Payload has no extractable assistant message — log and exit 0."""
    payload = json.dumps({"messages": []})
    proc = _run_hook(
        payload,
        {
            "PATH": "/usr/bin:/bin",
            "TM_WORKER_NAME": "test-tm",
            "TUBEMAIL_SECRET": "secret123",
        },
    )
    assert proc.returncode == 0
    assert "no assistant text" in proc.stderr


# ── extraction unit tests ────────────────────────────────────────────────────


@pytest.fixture
def hook_module():
    """Import the script as a module so we can call _extract_last_assistant_text
    directly without spawning a subprocess."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("post_stop_relay", HOOK_SCRIPT)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_extract_inline_messages_string_content(hook_module):
    payload = {
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "the result"},
        ]
    }
    assert hook_module._extract_last_assistant_text(payload) == "the result"


def test_extract_inline_messages_block_content(hook_module):
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "let me think..."},
                    {"type": "text", "text": "Done.\n\n```qm-report\n{}\n```"},
                ],
            }
        ]
    }
    text = hook_module._extract_last_assistant_text(payload)
    assert text is not None
    # Both thinking and text are joined so the qm-report fence survives even
    # if the LLM tucked it into a non-canonical block.
    assert "let me think" in text
    assert "```qm-report" in text


def test_extract_picks_latest_assistant(hook_module):
    payload = {
        "messages": [
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "more"},
            {"role": "assistant", "content": "newest"},
        ]
    }
    assert hook_module._extract_last_assistant_text(payload) == "newest"


def test_extract_from_transcript_jsonl(hook_module, tmp_path: Path):
    """Transcript path schema: each line is a JSON event with `message`."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {"type": "user", "message": {"role": "user", "content": "go"}}
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "first attempt"}
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": "the final answer",
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    payload = {"transcript_path": str(transcript)}
    assert (
        hook_module._extract_last_assistant_text(payload) == "the final answer"
    )


def test_extract_returns_none_when_only_user_messages(hook_module):
    payload = {"messages": [{"role": "user", "content": "go"}]}
    assert hook_module._extract_last_assistant_text(payload) is None


# ── HTTP behavior ────────────────────────────────────────────────────────────


def test_posts_to_outbound_endpoint(hook_module, monkeypatch):
    """Mock urlopen and assert the script POSTs to the right URL with bearer auth."""
    captured: dict[str, Any] = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["data"] = req.data
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp()

    monkeypatch.setattr(hook_module.urllib.request, "urlopen", fake_urlopen)

    hook_module._post_outbound(
        hub_url="http://localhost:8001",
        worker="leanspecs-tm",
        secret="abc123",
        text="my reply",
    )

    assert captured["url"] == (
        "http://localhost:8001/tubemail/leanspecs-tm/outbound"
    )
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer abc123"
    body = json.loads(captured["data"].decode("utf-8"))
    assert body["text"] == "my reply"
    assert body["meta"] == {"kind": "stop_relay"}


def test_post_failure_does_not_raise(hook_module, monkeypatch):
    """HTTP failures must NOT propagate. The hook escalates to spool/exit2
    in main(), but `_post_outbound` itself returns False rather than
    raising — main() needs the boolean to drive the spool decision."""
    import urllib.error

    def boom(*a, **kw):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(hook_module.urllib.request, "urlopen", boom)

    # Must not raise. retries=1 + no-op sleep keeps the test fast.
    result = hook_module._post_outbound(
        hub_url="http://localhost:8001",
        worker="leanspecs-tm",
        secret="abc123",
        text="my reply",
        retries=1,
        sleep_fn=lambda _s: None,
    )
    assert result is False


def test_empty_hub_url_env_falls_back_to_default(tmp_path: Path):
    """Some claude-tm launches export TUBEMAIL_HUB_URL='' (empty) instead
    of leaving it unset. os.environ.get(name, default) returns the empty
    string in that case, NOT the default. Without the explicit `or
    HUB_URL_DEFAULT` guard, the hook builds a relative URL
    ('/tubemail/.../outbound') and urllib raises 'unknown url type',
    making the hook exit non-zero on every Stop. This test pins the
    fix so a future refactor can't reintroduce the regression.
    """
    payload = json.dumps({"messages": [{"role": "assistant", "content": "x"}]})
    proc = _run_hook(
        payload,
        {
            "PATH": "/usr/bin:/bin",
            "TM_WORKER_NAME": "test-tm",
            "TUBEMAIL_SECRET": "secret123",
            "TUBEMAIL_HUB_URL": "",  # explicitly empty — the regression case
            # Keep retries low + sandbox the spool so this test stays fast
            # and doesn't write to the developer's real ~/.claude/.
            "TUBEMAIL_STOP_HOOK_RETRIES": "1",
            "TUBEMAIL_STOP_HOOK_SPOOL_DIR": str(tmp_path / "spool"),
        },
    )
    # Hook must not abort with the urllib 'unknown url type' error.
    assert "unknown url type" not in proc.stderr
    # POST will fail (no real hub at localhost:8001 in tests) but the spool
    # write succeeds, so the hook exits 0 — not 2. Exit 2 only fires when
    # spool ALSO fails (covered by the durability test).
    assert proc.returncode == 0
