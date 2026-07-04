"""Tests for the fresh-restart signal path (skip --continue for one cycle).

The manager normally restarts a worker with `--continue` so conversation
context survives. When the caller passes `meta.fresh=true` on a
restart/force_restart event (or `tm_restart(fresh=true)` at the MCP layer),
the manager must skip `--continue` for exactly one cycle so Claude Code's
startup sequence performs the automatic /rename and the worker
re-registers cleanly.

These tests pin four properties:
  1. `_build_restart_cmd` omits `--continue` when the fresh flag is set,
     and appends it otherwise (default behavior unchanged).
  2. `_next_pending_fresh` treats the fresh flag as one-shot: it is only
     honored on a clean-exit restart; crash-recovery restarts always fall
     back to --continue regardless of any prior fresh flag.
  3. The listener's `_handle_event` sets the fresh flag when meta.fresh
     is present and leaves it clear otherwise.
  4. `clear_flags()` wipes the fresh flag alongside the other one-shots.
"""

from __future__ import annotations

import threading

from tubemail.manager import (
    _ManagerChannelListener,
    _build_restart_cmd,
    _next_pending_fresh,
)


BASE_CMD = ["claude", "--name", "sacrificial-tm"]


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────


def test_first_launch_never_appends_continue():
    """restart_count == 0 → argv is base_cmd untouched, was_fresh False."""
    cmd, was_fresh = _build_restart_cmd(BASE_CMD, restart_count=0, pending_fresh_restart=False)
    assert cmd == BASE_CMD
    assert was_fresh is False


def test_first_launch_ignores_fresh_flag():
    """The fresh flag only matters on restarts; the very first launch is
    already fresh by definition. was_fresh stays False so the log line
    doesn't lie."""
    cmd, was_fresh = _build_restart_cmd(BASE_CMD, restart_count=0, pending_fresh_restart=True)
    assert cmd == BASE_CMD
    assert was_fresh is False


def test_default_restart_appends_continue():
    """Default: restarts get --continue so conversation context survives."""
    cmd, was_fresh = _build_restart_cmd(BASE_CMD, restart_count=1, pending_fresh_restart=False)
    assert cmd == BASE_CMD + ["--continue"]
    assert was_fresh is False


def test_fresh_restart_omits_continue():
    """fresh=true on a restart → --continue is NOT appended."""
    cmd, was_fresh = _build_restart_cmd(BASE_CMD, restart_count=1, pending_fresh_restart=True)
    assert "--continue" not in cmd
    assert "-c" not in cmd
    assert cmd == BASE_CMD
    assert was_fresh is True


def test_fresh_restart_leaves_operator_continue_untouched():
    """If the operator's own base_cmd already carries --continue, the fresh
    flag can't strip it (that's the operator's explicit config). was_fresh
    reports False so the operator's intent wins the log line too."""
    cmd_with_continue = BASE_CMD + ["--continue"]
    cmd, was_fresh = _build_restart_cmd(
        cmd_with_continue, restart_count=1, pending_fresh_restart=True
    )
    assert cmd == cmd_with_continue
    assert was_fresh is False


def test_fresh_restart_respects_short_continue_flag():
    """`-c` is Claude's short form of --continue; same rule."""
    cmd_with_short = BASE_CMD + ["-c"]
    cmd, was_fresh = _build_restart_cmd(
        cmd_with_short, restart_count=1, pending_fresh_restart=True
    )
    assert cmd == cmd_with_short
    assert was_fresh is False


# ─────────────────────────────────────────────────────────────────────────
# One-shot semantics
# ─────────────────────────────────────────────────────────────────────────


def test_pending_fresh_is_one_shot_on_clean_restart():
    """Only a CLEAN-exit restart that also carries the fresh flag arms the
    next cycle. Consumers are expected to reset pending_fresh_restart to
    False after building the cmd — so any following restart falls back to
    --continue."""
    assert _next_pending_fresh(exit_code=0, restart_signal=True, fresh_signal=True) is True
    # clean exit, restart requested, but no fresh flag → normal --continue restart
    assert _next_pending_fresh(exit_code=0, restart_signal=True, fresh_signal=False) is False
    # clean exit, no restart requested → we're stopping anyway
    assert _next_pending_fresh(exit_code=0, restart_signal=False, fresh_signal=True) is False


def test_crash_recovery_is_never_fresh():
    """Non-zero exit == crash. Crash-recovery restart always uses --continue,
    even if the fresh flag was somehow set. This makes fresh genuinely
    one-shot and matches the work-order's constraint: 'crash recovery after
    a fresh restart behaves exactly as today.'"""
    assert _next_pending_fresh(exit_code=1, restart_signal=True, fresh_signal=True) is False
    assert _next_pending_fresh(exit_code=137, restart_signal=True, fresh_signal=True) is False
    assert _next_pending_fresh(exit_code=1, restart_signal=False, fresh_signal=True) is False


# ─────────────────────────────────────────────────────────────────────────
# Listener wiring
# ─────────────────────────────────────────────────────────────────────────


def _bare_listener() -> _ManagerChannelListener:
    """Construct a listener bypassing __init__'s network side effects."""
    listener = _ManagerChannelListener.__new__(_ManagerChannelListener)
    listener._hub_url = "http://localhost:8001"
    listener._name = "sacrificial-tm-manager"
    listener._secret = "x"
    listener._session_name = "sacrificial-tm"
    listener._headers = {"Authorization": "Bearer x"}
    listener._stop_event = threading.Event()
    listener._thread = None
    listener._child = None
    listener._child_start_time = 0.0
    listener._restart_requested = False
    listener._stop_requested = False
    listener._update_manager_requested = False
    listener._fresh_restart_requested = False
    listener._pty_stream_stop = None
    listener._pty_stream_thread = None
    listener._pty_attach_pending = False
    listener._last_context_pct = None
    listener._last_active_state = None
    listener._context_pct_thread = None
    return listener


def test_restart_signal_without_fresh_meta_leaves_fresh_clear():
    """Existing behavior: meta={'kind': 'restart'} raises the restart flag
    but NOT the fresh flag."""
    listener = _bare_listener()
    listener._handle_event("channel_event", {"meta": {"kind": "restart"}})
    assert listener.restart_requested is True
    assert listener.fresh_restart_requested is False


def test_force_restart_without_fresh_meta_leaves_fresh_clear():
    """Same for the force_restart path (tm_restart's default route)."""
    listener = _bare_listener()
    listener._handle_event("channel_event", {"meta": {"kind": "force_restart"}})
    assert listener.restart_requested is True
    assert listener.fresh_restart_requested is False


def test_restart_signal_with_fresh_meta_sets_flag():
    """meta.fresh=True on a restart → fresh flag raised alongside restart."""
    listener = _bare_listener()
    listener._handle_event(
        "channel_event", {"meta": {"kind": "restart", "fresh": True}}
    )
    assert listener.restart_requested is True
    assert listener.fresh_restart_requested is True


def test_force_restart_signal_with_fresh_meta_sets_flag():
    """Same for force_restart — the MCP tm_restart(fresh=true) path."""
    listener = _bare_listener()
    listener._handle_event(
        "channel_event", {"meta": {"kind": "force_restart", "fresh": True}}
    )
    assert listener.restart_requested is True
    assert listener.fresh_restart_requested is True


def test_clear_flags_wipes_fresh_alongside_restart():
    """clear_flags() runs at the top of every loop iteration; if the fresh
    flag survived across iterations it would stop being one-shot."""
    listener = _bare_listener()
    listener._restart_requested = True
    listener._fresh_restart_requested = True
    listener._stop_requested = True
    listener._update_manager_requested = True

    listener.clear_flags()

    assert listener.restart_requested is False
    assert listener.fresh_restart_requested is False
    assert listener.stop_requested is False
    assert listener.update_manager_requested is False
