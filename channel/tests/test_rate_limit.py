"""Tests for rate-limit detection in the claude-tm pty manager."""

from __future__ import annotations

from tubemail.manager import (
    _contains_rate_limit,
    _rate_limit_delay,
    _RATE_LIMIT_BACKOFF_S,
    _RATE_LIMIT_RESET_AFTER_S,
)


class TestContainsRateLimit:
    def test_matches_exact_error_line(self):
        tail = b"API Error: Server is temporarily limiting requests (not your usage limit) \xc2\xb7 Rate limited"
        assert _contains_rate_limit(tail)

    def test_matches_across_line_wrap(self):
        tail = b"API Error: Server is temporarily\nlimiting requests"
        assert _contains_rate_limit(tail)

    def test_matches_with_ansi_escapes(self):
        tail = b"\x1b[31mAPI Error: Server is temporarily limiting requests\x1b[0m"
        assert _contains_rate_limit(tail)

    def test_no_match_on_unrelated_text(self):
        tail = b"Loading... ready for input"
        assert not _contains_rate_limit(tail)

    def test_no_match_on_generic_rate_limit_phrase(self):
        assert not _contains_rate_limit(b"You hit your usage limit")


class TestRateLimitDelay:
    def test_first_retry_is_shortest(self):
        assert _rate_limit_delay(0) == _RATE_LIMIT_BACKOFF_S[0]

    def test_progressive_backoff(self):
        delays = [_rate_limit_delay(i) for i in range(len(_RATE_LIMIT_BACKOFF_S))]
        assert delays == list(_RATE_LIMIT_BACKOFF_S)

    def test_caps_at_two_minutes(self):
        assert _rate_limit_delay(99) == 120
        assert _RATE_LIMIT_BACKOFF_S[-1] == 120


class TestRateLimitResetWindow:
    def test_reset_window_is_twenty_seconds(self):
        # Spec: if 'continue' runs cleanly for 20s, reset the counter.
        assert _RATE_LIMIT_RESET_AFTER_S == 20
