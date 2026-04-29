#!/usr/bin/env python3
"""Claude Code Stop hook: relay the assistant's last message to tubemail.

Runs when Claude Code fires a `Stop` event (assistant turn finished).
Reads the hook payload from stdin, extracts the most recent assistant
message text, and POSTs it to the tubemail hub's
`/{worker}/outbound` endpoint so it lands as a `kind=outbound` event
on the worker's timeline.

Why this exists:
    The tubemail-channel plugin exposes a `reply` MCP tool that the
    LLM can call to relay its response. The LLM doesn't always call
    it (it's optional, not auto-fired). When the LLM emits prose +
    a qm-report fence as a normal chat reply, no `reply` invocation
    happens and the response never reaches tubemail's bridge.
    Quartermaster's auto-extract reads from that bridge, so the
    fence becomes invisible despite being syntactically perfect.

    This hook closes that gap: every Stop event POSTs the assistant's
    last message text, regardless of whether the LLM remembered to
    call `reply`. The relay is best-effort — any failure exits 0 so
    we never block the stop.

Skip-on-double-fire:
    If the LLM DID call `reply` during this turn, both paths fire
    and we'd double-record. The hook tags its event with
    `meta.kind="stop_relay"` so consumers can dedup if needed; the
    bridge engine itself doesn't enforce uniqueness because that
    requires per-turn correlation that lives outside this hook's
    visibility.

Activation:
    Only runs when `TM_WORKER_NAME` is set (i.e. inside a tubemail-
    channel session). On non-tubemail sessions the hook self-skips
    and exits 0.

Settings.json registration:
    {"hooks": {"Stop": [{"hooks": [{"type": "command",
        "command": "python3 /path/to/post_stop_relay.py"}]}]}}
"""

from __future__ import annotations

# Standard Libraries
import json
import os
import sys
import urllib.error
import urllib.request


HUB_URL_DEFAULT = "http://localhost:8004"
TIMEOUT_S = 5.0


def _stderr(msg: str) -> None:
    """Log to stderr only — Claude Code captures stderr for hook diagnostics
    but doesn't act on it. stdout is reserved for hook control JSON which we
    don't emit (we never block the stop)."""
    sys.stderr.write(f"[tubemail-stop-relay] {msg}\n")


def _extract_last_assistant_text(payload: dict) -> str | None:
    """Pull the most recent assistant message text out of the Stop event.

    Claude Code's Stop hook payload includes a `transcript_path` pointing at
    a JSONL file with the conversation, OR an inline `messages` array, OR
    both. We try messages first, then transcript path. Schema isn't fully
    locked across Claude Code versions, so we're tolerant.
    """
    # Inline messages (some hook revisions include the conversation directly).
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            text = _content_to_text(content)
            if text:
                return text
        #end for
    #end if

    # Transcript path on disk (most common shape).
    transcript_path = payload.get("transcript_path")
    if isinstance(transcript_path, str) and transcript_path:
        try:
            with open(transcript_path, encoding="utf-8") as f:
                lines = [line for line in f if line.strip()]
        except OSError as err:
            _stderr(f"transcript read failed: {err}")
            return None
        #end try
        for line in reversed(lines):
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            #end try
            # Different transcript schemas — try a few common shapes.
            msg = evt.get("message") if isinstance(evt, dict) else None
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                text = _content_to_text(msg.get("content"))
                if text:
                    return text
                #end if
            #end if
            if isinstance(evt, dict) and evt.get("role") == "assistant":
                text = _content_to_text(evt.get("content"))
                if text:
                    return text
                #end if
            #end if
        #end for
    #end if

    return None


def _content_to_text(content) -> str | None:
    """Claude Code messages can have content as a string or a list of
    blocks (`{"type": "text", "text": "..."}`). Concatenate text blocks
    in order; ignore tool-use / tool-result blocks (those are structured
    events, not the assistant's prose)."""
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("text", "thinking"):
                # 'thinking' blocks are extended-thinking output; include
                # them so the relay captures the full reasoning text. The
                # LLM's qm-report fence usually lives in the 'text' block
                # but tools/skills sometimes emit fences from thinking
                # context — better to over-include than miss a fence.
                t = block.get("text") or block.get("thinking")
                if isinstance(t, str) and t:
                    parts.append(t)
                #end if
            #end if
        #end for
        joined = "\n".join(parts).strip()
        return joined or None
    #end if
    return None


def _post_outbound(hub_url: str, worker: str, secret: str, text: str) -> None:
    """POST the relay event to the hub. Best-effort; failure exits 0."""
    url = f"{hub_url.rstrip('/')}/tubemail/{worker}/outbound"
    body = json.dumps(
        {"text": text, "meta": {"kind": "stop_relay"}}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            if resp.status >= 400:
                _stderr(f"POST {url} -> {resp.status}")
            #end if
        #end with
    except urllib.error.HTTPError as err:
        _stderr(f"POST {url} HTTP {err.code}: {err.reason}")
    except (urllib.error.URLError, OSError) as err:
        _stderr(f"POST {url} failed: {err}")
    #end try


def main() -> int:
    worker = os.environ.get("TM_WORKER_NAME", "").strip()
    if not worker:
        # Not a tubemail-channel session — silently skip.
        return 0
    #end if

    secret = os.environ.get("TUBEMAIL_SECRET", "").strip()
    if not secret:
        _stderr("TUBEMAIL_SECRET not set; cannot POST relay event")
        return 0
    #end if

    # Use `or HUB_URL_DEFAULT` (not get(name, default)) so that an
    # explicitly-set EMPTY env var falls back to the default. Some
    # tubemail launches export TUBEMAIL_HUB_URL='' instead of leaving
    # it unset — without this guard, the script builds a relative URL
    # like '/tubemail/{worker}/outbound' and urllib raises 'unknown url
    # type', exit 1, and Claude Code surfaces the failure on stop.
    hub_url = (os.environ.get("TUBEMAIL_HUB_URL") or HUB_URL_DEFAULT).strip()
    if not hub_url:
        hub_url = HUB_URL_DEFAULT
    #end if

    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    #end if

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as err:
        _stderr(f"stdin not JSON: {err}")
        return 0
    #end try

    text = _extract_last_assistant_text(payload)
    if not text:
        _stderr("no assistant text found in Stop event")
        return 0
    #end if

    _post_outbound(hub_url, worker, secret, text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
