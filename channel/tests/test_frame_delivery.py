"""Manager frame-delivery resilience tests.

Covers the class of failure where the hub is slow, down, or returns
errors and the manager must:

- Keep pump_io running (never blocks on hub I/O).
- Back off exponentially on consecutive POST failures.
- Drop the stream entirely after the give-up window so it doesn't
  burn CPU forever.
- Drain the queue while backing off so memory stays bounded.
- Reset the failure counter on the next success.

These are regression tests for the eviction-storm incident
(see jjstack/rca-2026-04-25.md, Branch D).
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tubemail.manager import (
    _FRAMES_BACKOFF_S,
    _FRAMES_GIVE_UP_AFTER_S,
    _ManagerChannelListener,
    _PtyChild,
    _drain_queue,
    _frames_backoff_s,
    _is_actively_processing,
)


class TestIsActivelyProcessing:
    """Detector for claude's TUI-busy state. Used by the manager to push
    an authoritative is_active signal to the hub instead of relying on
    event-timeline decay. Failure modes covered:

    - active timer parenthetical "(8s · ↑512 tokens)"
    - spinner-label words (Mulling, Unfurling, thinking, …)
    - tool-call indicators (Calling tubemail, Running…)
    - idle screens with just the prompt (must NOT match)
    - false-positive defenses (the markers don't appear at the idle
      prompt or in static UI chrome)
    """

    def test_running_timer_parenthetical(self):
        # Real frame from the recording: " (20s · ↑986 tokens · …)"
        tail = b"* Unfurling... (20s\xc2\xb7 986 tokens)"
        assert _is_actively_processing(tail) is True

    def test_running_timer_with_seconds_only(self):
        # Variant: "(8s ·" without tokens.
        tail = b"Mulling... (8s \xc2\xb7"
        assert _is_actively_processing(tail) is True

    def test_mulling_label(self):
        assert _is_actively_processing(b"\x1b[31m* Mulling...\x1b[0m") is True

    def test_unfurling_label(self):
        assert _is_actively_processing(b"Unfurling...") is True

    def test_still_thinking_label(self):
        assert _is_actively_processing(b"still thinking with xhigh effort") is True

    def test_calling_tubemail_tool(self):
        assert _is_actively_processing(b"Calling tubemail (ctrl+o to expand)") is True

    def test_brewed_for_completion_marker(self):
        # The "Brewed for Xs" line lingers briefly post-completion.
        # Counted as busy — the screen hasn't returned to the idle
        # prompt yet, and the marker vanishes within a second.
        assert _is_actively_processing(b"Brewed for 8m 50s") is True

    def test_running_subtext(self):
        # The "⎿ Running…" sub-text under a tool-call indicator. Use
        # raw UTF-8 bytes rather than a non-ASCII literal so the test
        # source itself stays plain ASCII.
        assert _is_actively_processing(
            "  ⎿  Running...".encode("utf-8")
        ) is True

    def test_idle_prompt_only(self):
        # Just the input box, the divider, and the status bar — what
        # the screen looks like when claude is waiting for input.
        tail = (
            b"\xe2\x9d\xaf  \n"
            b"\xe2\x94\x80 tubemail-tm \xe2\x94\x80\n"
            b"Opus 4.7 (1M context) \xc2\xb7xhigh | tubemail | main | "
            b"context 28% | usage 23%\n"
            b"\xe2\x8f\xb5\xe2\x8f\xb5 auto mode on\n"
        )
        assert _is_actively_processing(tail) is False

    def test_empty_buffer(self):
        assert _is_actively_processing(b"") is False

    def test_no_false_positive_on_status_bar_alone(self):
        # The status bar contains "context X%" and other static chrome
        # but none of the active-state markers.
        tail = (
            b"Opus 4.7 (1M context) \xc2\xb7xhigh | tubemail | main "
            b"~1 ?5 | context 28% | usage 23%\n"
        )
        assert _is_actively_processing(tail) is False


class TestFramesBackoff:
    def test_first_failure_uses_shortest_delay(self):
        assert _frames_backoff_s(1) == _FRAMES_BACKOFF_S[0]

    def test_progressive_backoff(self):
        delays = [_frames_backoff_s(i) for i in range(1, len(_FRAMES_BACKOFF_S) + 1)]
        assert delays == list(_FRAMES_BACKOFF_S)

    def test_saturates_at_max(self):
        # Way past the table — clamps to last value.
        assert _frames_backoff_s(99) == _FRAMES_BACKOFF_S[-1]

    def test_zero_failures_does_not_index_below_zero(self):
        # Defensive: even though callers should pass >=1, this must not
        # raise IndexError.
        assert _frames_backoff_s(0) == _FRAMES_BACKOFF_S[0]


class TestDrainQueue:
    def test_drains_all_pending_items(self):
        q: queue.Queue[bytes] = queue.Queue()
        for i in range(5):
            q.put_nowait(f"chunk{i}".encode())
        dropped = _drain_queue(q)
        assert dropped == 5
        assert q.empty()

    def test_empty_queue_drops_zero(self):
        q: queue.Queue[bytes] = queue.Queue()
        assert _drain_queue(q) == 0


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


def _make_listener() -> _ManagerChannelListener:
    """Construct a listener bypassing __init__'s side effects (no hub
    registration). We only need the methods, not a live network."""
    listener = _ManagerChannelListener.__new__(_ManagerChannelListener)
    listener._hub_url = "http://localhost:8001"
    listener._name = "test-tm-manager"
    listener._secret = "x"
    listener._session_name = "test-tm"
    listener._headers = {"Authorization": "Bearer x"}
    listener._stop_event = threading.Event()
    listener._thread = None
    listener._child = None
    listener._child_start_time = 0.0
    listener._restart_requested = False
    listener._stop_requested = False
    listener._update_manager_requested = False
    listener._pty_stream_stop = None
    listener._pty_stream_thread = None
    listener._pty_attach_pending = False
    listener._last_context_pct = None
    listener._last_active_state = None
    listener._context_pct_thread = None
    return listener


def _make_attached_child(initial: bytes = b"") -> _PtyChild:
    """A _PtyChild with attach_stream() wired, no real pty fork."""
    child = _PtyChild.__new__(_PtyChild)
    child._cmd = ["claude"]
    child._session_name = "test-tm"
    child._master_fd = 999  # non-None sentinel; we never call os.write on it
    child._child_pid = None
    child._screen_buf = bytearray(initial)
    child._screen_buf_max = 32768
    child._screen_lock = threading.Lock()
    child._stream_queue = None
    child._rl_waiting = False
    child._rl_retry_count = 0
    child._rl_timer = None
    child._rl_reset_timer = None
    return child


class TestPtyStreamLoopFailureHandling:
    def test_gives_up_after_circuit_breaker_window(self, monkeypatch: pytest.MonkeyPatch):
        """When httpx.post fails continuously beyond _FRAMES_GIVE_UP_AFTER_S,
        the loop must exit cleanly instead of looping forever — even if
        the queue keeps filling with new bytes the whole time."""
        listener = _make_listener()
        child = _make_attached_child()
        listener._child = child

        post_calls = {"n": 0}

        def boom(*_a: Any, **_kw: Any) -> _FakeResponse:
            post_calls["n"] += 1
            raise ConnectionError("hub is down")

        # Larger window than backoff so we can verify multiple retries
        # happen before the circuit breaker fires.
        monkeypatch.setattr("tubemail.manager._FRAMES_GIVE_UP_AFTER_S", 0.3)
        monkeypatch.setattr("tubemail.manager._FRAMES_BACKOFF_S", (0.01,))

        # Keep the queue fed during the test so the loop can iterate
        # POST attempts. Without this, the failure-path drain empties
        # the queue and the loop just sits in q.get() until the
        # top-of-loop give-up check fires on the NEXT iteration.
        feeder_stop = threading.Event()
        _, q = child.attach_stream()

        def feed() -> None:
            i = 0
            while not feeder_stop.is_set():
                try:
                    q.put_nowait(f"chunk{i}".encode())
                except queue.Full:
                    pass
                i += 1
                feeder_stop.wait(0.005)

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()

        try:
            with patch("tubemail.manager.httpx.post", side_effect=boom):
                stop = threading.Event()
                t = threading.Thread(
                    target=listener._pty_stream_loop, args=(stop,), daemon=True,
                )
                t.start()
                t.join(timeout=2.0)
        finally:
            feeder_stop.set()
            feeder.join(timeout=1.0)

        assert not t.is_alive(), "stream loop must exit after circuit breaker"
        assert post_calls["n"] >= 2, (
            "expected at least 2 POST attempts before giving up "
            f"(got {post_calls['n']})"
        )
        # After give-up, the queue should be detached.
        assert child._stream_queue is None

    def test_recovers_from_transient_failure(self, monkeypatch: pytest.MonkeyPatch):
        """One failure followed by a success must reset the failure
        counter — not escalate into give-up."""
        listener = _make_listener()
        child = _make_attached_child()
        listener._child = child

        _, q = child.attach_stream()
        q.put_nowait(b"first")

        # First call raises, subsequent calls succeed.
        side_effects = [ConnectionError("transient")] + [
            _FakeResponse(200) for _ in range(50)
        ]
        post_mock = MagicMock(side_effect=side_effects)

        # Tiny backoff so the test runs fast.
        monkeypatch.setattr("tubemail.manager._FRAMES_BACKOFF_S", (0.0,))

        # Keep feeding data on a background thread so the loop has
        # something to POST after the first failure (the failure path
        # drains the queue, so we need a fresh supply).
        feeder_stop = threading.Event()

        def feed() -> None:
            i = 0
            while not feeder_stop.is_set():
                try:
                    q.put_nowait(f"chunk{i}".encode())
                except queue.Full:
                    pass
                i += 1
                feeder_stop.wait(0.01)

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()

        try:
            with patch("tubemail.manager.httpx.post", post_mock):
                stop = threading.Event()
                t = threading.Thread(
                    target=listener._pty_stream_loop, args=(stop,), daemon=True,
                )
                t.start()
                # Let it process several items, then stop cleanly.
                time.sleep(0.3)
                stop.set()
                t.join(timeout=2.0)
        finally:
            feeder_stop.set()
            feeder.join(timeout=1.0)

        assert not t.is_alive()
        # Must have made at least 2 POST attempts (the failed one + a
        # successful retry). The success must not trigger circuit breaker.
        assert post_mock.call_count >= 2, (
            f"expected ≥2 POSTs, got {post_mock.call_count}"
        )

    def test_404_short_circuits_without_backoff(self, monkeypatch: pytest.MonkeyPatch):
        """404 means 'no clients' and is NOT a hub-failure. Loop should
        exit immediately without engaging the failure path."""
        listener = _make_listener()
        child = _make_attached_child()
        listener._child = child

        _, q = child.attach_stream()
        q.put_nowait(b"first")

        post_mock = MagicMock(return_value=_FakeResponse(404, "no clients"))

        with patch("tubemail.manager.httpx.post", post_mock):
            stop = threading.Event()
            t = threading.Thread(
                target=listener._pty_stream_loop, args=(stop,), daemon=True,
            )
            t.start()
            t.join(timeout=2.0)

        assert not t.is_alive()
        # 404 must exit on the first response — not retry.
        assert post_mock.call_count == 1


class TestContextPctLoopFailureHandling:
    def test_backs_off_on_consecutive_failures(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        """Consecutive POST failures should not fire every 3s into a
        wall — they should back off exponentially."""
        listener = _make_listener()
        child = _make_attached_child(b"context 42%")
        listener._child = child

        # Make sleeps near-zero to keep the test fast.
        monkeypatch.setattr("tubemail.manager._FRAMES_BACKOFF_S", (0.001,))

        # Replace stop_event with one whose .wait(timeout) returns False
        # immediately (i.e. doesn't block). This lets us iterate the loop
        # at full speed without waiting for the production 3s baseline.
        class FastEvent:
            def __init__(self) -> None:
                self._set = False

            def wait(self, timeout: float | None = None) -> bool:
                return self._set

            def set(self) -> None:
                self._set = True

            def is_set(self) -> bool:
                return self._set

        listener._stop_event = FastEvent()  # type: ignore[assignment]

        post_mock = MagicMock(side_effect=ConnectionError("hub down"))

        with patch("tubemail.manager.httpx.post", post_mock):
            t = threading.Thread(target=listener._context_pct_loop, daemon=True)
            t.start()
            # Spin briefly — with FastEvent.wait returning instantly, the
            # loop iterates as fast as POSTs fail.
            time.sleep(0.05)
            listener._stop_event.set()
            t.join(timeout=2.0)

        assert not t.is_alive()
        # The loop should have made multiple POST attempts.
        assert post_mock.call_count >= 2, (
            f"expected ≥2 POSTs, got {post_mock.call_count}"
        )
        # _last_context_pct must NOT have been updated on failure.
        assert listener._last_context_pct is None
