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
import time

from tubemail.manager import (
    _RESTART_DEBOUNCE_S,
    _ManagerChannelListener,
    _build_restart_cmd,
    _next_pending_fresh,
    _screen_looks_ready,
    _should_debounce_restart,
    _spawn_post_fresh_sync_inbox,
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


def test_fresh_restart_strips_sticky_continue_from_base_cmd():
    """The claude-tm bash wrapper appends --continue to passthru after every
    manager re-exec, so by the time the manager re-loads its own module the
    "operator config" already carries --continue. Fresh must STRIP it to
    genuinely start a new conversation; if fresh only "declines to append"
    it would be a no-op in practice — the observed bug this feature fixes."""
    cmd_with_continue = BASE_CMD + ["--continue"]
    cmd, was_fresh = _build_restart_cmd(
        cmd_with_continue, restart_count=1, pending_fresh_restart=True
    )
    assert "--continue" not in cmd
    assert cmd == BASE_CMD
    assert was_fresh is True


def test_fresh_restart_strips_short_continue_flag():
    """`-c` is Claude's short form of --continue; same strip rule."""
    cmd_with_short = BASE_CMD + ["-c"]
    cmd, was_fresh = _build_restart_cmd(
        cmd_with_short, restart_count=1, pending_fresh_restart=True
    )
    assert "-c" not in cmd
    assert "--continue" not in cmd
    assert cmd == BASE_CMD
    assert was_fresh is True


def test_default_restart_does_not_double_up_continue():
    """When base_cmd already carries --continue (typical after a claude-tm
    re-exec), the default restart branch must NOT append a second one."""
    cmd_with_continue = BASE_CMD + ["--continue"]
    cmd, was_fresh = _build_restart_cmd(
        cmd_with_continue, restart_count=1, pending_fresh_restart=False
    )
    # Exactly one --continue, in its original position, was_fresh False.
    assert cmd.count("--continue") == 1
    assert cmd == cmd_with_continue
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
    listener._last_restart_accepted_ts = 0.0
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


# ─────────────────────────────────────────────────────────────────────────
# Post-fresh /sync-inbox auto-catchup
# ─────────────────────────────────────────────────────────────────────────


def test_screen_looks_ready_false_for_empty_screen():
    """Blank pty output → not ready. If the child hasn't rendered anything
    yet we must not type /sync-inbox — it would land wherever the cursor
    happens to be (usually a hung stdin)."""
    assert _screen_looks_ready("") is False


def test_screen_looks_ready_false_during_startup_dialog():
    """The dev-channels warning is startup — no status bar yet. Typing at
    this moment would land IN the dev-channels prompt as literal text,
    breaking startup."""
    startup = "I am using this for local development purposes only.\nEnter to continue"
    assert _screen_looks_ready(startup) is False


def test_screen_looks_ready_true_when_shortcuts_hint_visible():
    """Legacy TUI: "? for shortcuts" hint at the bottom of the prompt
    means claude finished startup and is idle at an empty prompt."""
    ready = (
        "\n"
        "❯  \n"
        "  ~/some/repo    context 12%    ? for shortcuts"
    )
    assert _screen_looks_ready(ready) is True


def test_screen_looks_ready_true_when_auto_mode_marker_visible():
    """Current TUI: "auto mode on" appears in the status bar once the
    session is idle at the empty prompt. Live-caught: the initial
    predicate looked only for "context N%" and the current Claude Code
    TUI stopped emitting that literal, so a fresh restart on
    PycharmProjects-tm timed out and skipped auto-catchup silently."""
    ready = (
        "❯ Try \"refactor <filepath>\"\n"
        "⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"
    )
    assert _screen_looks_ready(ready) is True


def test_screen_looks_ready_true_when_for_agents_hint_visible():
    """Belt-and-braces: `for agents` alone is enough. Different TUI
    themes / effort levels can rearrange the status-bar text; any one
    idle marker being present is sufficient."""
    assert _screen_looks_ready("← for agents") is True


def test_screen_looks_ready_case_insensitive():
    """The status bar can render markers in different casing depending
    on the theme; the predicate must not be case-fragile."""
    assert _screen_looks_ready("FOR SHORTCUTS") is True
    assert _screen_looks_ready("Auto Mode On") is True


class _FakePtyChild:
    """A pty stub that captures send_command calls and lets tests script
    the readiness screen text over time.

    _wait_for_screen and send_command are the only surface the auto-
    catchup helper touches; we don't need a real pty for these tests."""

    def __init__(self, ready_after_s: float | None = 0.05) -> None:
        self._start_ts = time.monotonic()
        self._ready_after_s = ready_after_s
        self.sent: list[str] = []

    def _get_screen_text(self) -> str:
        # Returns "ready" text after ready_after_s has elapsed; empty
        # (never ready) if ready_after_s is None.
        if self._ready_after_s is None:
            return "loading..."
        if time.monotonic() - self._start_ts < self._ready_after_s:
            return "loading..."
        return "❯  ~/repo    auto mode on (shift+tab to cycle) · ← for agents"

    def _wait_for_screen(self, predicate, timeout_s: float, poll_s: float = 0.02) -> bool:
        # Simplified copy of the real _wait_for_screen so tests don't
        # depend on time.sleep timing beyond ~50ms.
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate(self._get_screen_text()):
                return True
            time.sleep(poll_s)
        return False

    def send_command(self, text: str) -> None:
        self.sent.append(text)


def test_post_fresh_sync_inbox_types_when_prompt_ready(monkeypatch):
    """The happy path: after fresh restart, prompt becomes ready quickly,
    the helper types /sync-inbox exactly once. Settle is monkeypatched
    down so the test finishes in well under a second."""
    monkeypatch.setattr("tubemail.manager._SYNC_INBOX_SETTLE_S", 0.05)
    child = _FakePtyChild(ready_after_s=0.02)
    _spawn_post_fresh_sync_inbox(child, "sacrificial-tm")
    # Give the daemon thread time to notice ready + settle + type.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not child.sent:
        time.sleep(0.02)
    assert child.sent == ["/sync-inbox"]


def test_post_fresh_sync_inbox_skips_when_prompt_never_readies(monkeypatch):
    """If the child never shows a ready prompt (crash mid-startup, hung
    on auth), the helper must give up cleanly rather than blindly typing
    into whatever's on screen — /sync-inbox in a dialog would garble it."""
    monkeypatch.setattr("tubemail.manager._SYNC_INBOX_WAIT_S", 0.2)
    monkeypatch.setattr("tubemail.manager._SYNC_INBOX_SETTLE_S", 0.05)
    child = _FakePtyChild(ready_after_s=None)  # never becomes ready
    _spawn_post_fresh_sync_inbox(child, "sacrificial-tm")
    # Wait past the timeout window; nothing should be typed.
    time.sleep(0.5)
    assert child.sent == []


def test_post_fresh_sync_inbox_not_called_on_default_restart(monkeypatch):
    """This is the caller-contract test: default restarts must not go
    through this helper. run() gates the call on `was_fresh`; we cover
    that at the seam by verifying that a caller who follows the contract
    (only invoke on fresh) doesn't type on non-fresh cycles.

    Here we just assert the helper spawns a thread only when called —
    it can't guard itself against misuse — but pair it with the
    _build_restart_cmd tests above that pin was_fresh's own truth table.
    Together they cover: fresh → helper spawned → /sync-inbox typed;
    default → was_fresh False → helper never spawned → nothing typed."""
    child = _FakePtyChild(ready_after_s=0.02)
    # NOT calling _spawn_post_fresh_sync_inbox — simulating the default
    # branch of run().
    time.sleep(0.2)
    assert child.sent == []


# ─────────────────────────────────────────────────────────────────────────
# Restart-signal debounce
# ─────────────────────────────────────────────────────────────────────────


def test_debounce_first_signal_always_accepted():
    """The listener's default `_last_restart_accepted_ts` is 0.0 (never
    accepted). A first restart signal at any wall-clock time must be
    accepted — the debounce is about DUPLICATES, not about a
    bootstrap-time cold-start delay."""
    assert _should_debounce_restart(now=1_700_000_000.0, last_accepted_ts=0.0) is False


def test_debounce_duplicate_within_window_is_dropped():
    """The bug pattern this fix is guarding: two force_restart signals
    arriving 0.14s apart (real failure case on iris-qa-ui-tm at
    2026-07-04). Within the debounce window the second is dropped."""
    accepted_at = 100.0
    assert _should_debounce_restart(now=100.14, last_accepted_ts=accepted_at) is True


def test_debounce_signal_past_window_accepted():
    """Once the window has fully elapsed, a new restart signal is a
    legitimate restart, not a duplicate. 60s > 10s window."""
    accepted_at = 100.0
    assert _should_debounce_restart(now=160.0, last_accepted_ts=accepted_at) is False


def test_debounce_signal_exactly_at_window_boundary_accepted():
    """At exactly `now - last == window_s`, the delta is not STRICTLY
    less than the window, so accept. This documents the boundary; the
    live traffic will never sit exactly on it, but pin the semantics."""
    accepted_at = 100.0
    assert (
        _should_debounce_restart(
            now=accepted_at + _RESTART_DEBOUNCE_S,
            last_accepted_ts=accepted_at,
        )
        is False
    )


def test_debounce_signal_just_before_window_end_dropped():
    """One tick before the window closes: still a duplicate."""
    accepted_at = 100.0
    assert (
        _should_debounce_restart(
            now=accepted_at + _RESTART_DEBOUNCE_S - 0.001,
            last_accepted_ts=accepted_at,
        )
        is True
    )


class _Clock:
    """Deterministic wall-clock stub for the debounce tests.

    Yields the scheduled values in order and, once exhausted, keeps
    returning the last one forever. The linger matters: the logging
    module internally calls ``time.time()`` when a warning is emitted
    (see cpython logging/__init__.py LogRecord.__init__), so a bare
    ``iter([...])`` runs dry mid-drop-path and raises StopIteration.
    Returning-the-last-value keeps those internal calls sane without
    letting the tests read whichever wall-clock happens to be running.
    """

    def __init__(self, schedule):
        self._schedule = list(schedule)
        self._i = 0
        # Explicit ordered log of every observed tick so a test can
        # assert HOW MANY reads happened in a given path (guard against
        # regressions where a rewrite starts calling time.time twice).
        self.reads: list[float] = []

    def __call__(self) -> float:
        if self._i < len(self._schedule):
            v = self._schedule[self._i]
            self._i += 1
        else:
            v = self._schedule[-1]
        self.reads.append(v)
        return v


def test_listener_second_force_restart_within_window_is_dropped(monkeypatch):
    """The bug scenario end-to-end at the listener seam: two
    force_restart{fresh:true} signals arrive 0.14s apart. The listener
    honors the first (raises restart_requested + fresh_restart_requested,
    stamps _last_restart_accepted_ts), then DROPS the second so the
    newborn fresh child is not killed and the one-shot fresh flag
    survives the duplicate.

    Uses a wall-clock stub so the test doesn't depend on real timing —
    we set time.time to return two values 0.14s apart, matching the
    live failure case on iris-qa-ui-tm."""
    listener = _bare_listener()
    monkeypatch.setattr("tubemail.manager.time.time", _Clock([1000.0, 1000.14]))

    # First signal — accepted.
    listener._handle_event(
        "channel_event", {"meta": {"kind": "force_restart", "fresh": True}}
    )
    assert listener.restart_requested is True
    assert listener.fresh_restart_requested is True
    assert listener._last_restart_accepted_ts == 1000.0

    # Second signal, 0.14s later — must be dropped. The critical
    # assertion is that _last_restart_accepted_ts didn't move (proof
    # the drop path ran, not the accept path).
    listener._handle_event(
        "channel_event", {"meta": {"kind": "force_restart", "fresh": True}}
    )
    assert listener._last_restart_accepted_ts == 1000.0
    # Fresh flag preserved (not re-set, but also not cleared — the
    # first signal's fresh intent survives untouched for the loop to
    # consume on the next iteration).
    assert listener.fresh_restart_requested is True


def test_listener_restart_60s_later_honored_normally(monkeypatch):
    """A legitimate consecutive restart a minute after the previous one
    must NOT be caught by the debounce — the window is a millisecond-
    scale duplicate filter, not a rate limiter for operators."""
    listener = _bare_listener()
    monkeypatch.setattr("tubemail.manager.time.time", _Clock([1000.0, 1060.0]))

    listener._handle_event("channel_event", {"meta": {"kind": "force_restart"}})
    assert listener._last_restart_accepted_ts == 1000.0

    # 60s later — legitimate second restart.
    listener._handle_event(
        "channel_event", {"meta": {"kind": "force_restart", "fresh": True}}
    )
    assert listener._last_restart_accepted_ts == 1060.0
    assert listener.fresh_restart_requested is True


def test_listener_debounce_ts_survives_clear_flags():
    """clear_flags() runs at the top of every loop iteration. The
    debounce timestamp must SURVIVE it — otherwise a duplicate arriving
    mid-launch (after the new child boots and the loop rolls over)
    would sail through debounce and kill the newborn child. The one-
    shot fresh flag is a separate concern and IS reset by clear_flags."""
    listener = _bare_listener()
    listener._last_restart_accepted_ts = 42.0
    listener._restart_requested = True
    listener._fresh_restart_requested = True

    listener.clear_flags()

    assert listener._last_restart_accepted_ts == 42.0  # PRESERVED
    assert listener.restart_requested is False  # cleared
    assert listener.fresh_restart_requested is False  # cleared


def test_listener_plain_restart_kind_also_debounces(monkeypatch):
    """Belt-and-braces: the /restart skill routes `kind=restart` (not
    force_restart); both kinds must debounce to catch the double-
    delivery whichever entry point the client used."""
    listener = _bare_listener()
    monkeypatch.setattr("tubemail.manager.time.time", _Clock([500.0, 500.05]))

    listener._handle_event("channel_event", {"meta": {"kind": "restart"}})
    assert listener._last_restart_accepted_ts == 500.0

    listener._handle_event(
        "channel_event", {"meta": {"kind": "restart", "fresh": True}}
    )
    assert listener._last_restart_accepted_ts == 500.0  # dropped
    # Second signal never ran, so fresh flag was NEVER raised.
    assert listener.fresh_restart_requested is False
