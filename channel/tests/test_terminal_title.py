"""Tests for the terminal-title emitter (QM #572).

Class-boundary regression: "after a worker starts OR restarts, the
emitted title equals session_name." These tests pin the exact OSC
bytes emitted for a given session name without touching a real
terminal — that keeps the assertion deterministic across CI runs and
across terminal emulators.

Background: the iris-qa cwd hosts three roles differentiated only by
TM_WORKER_NAME (iris-qa-tm, iris-qa-coder-tm, iris-qa-ui-tm). A
terminal window recycled across roles keeps whatever OSC title the
previous session set, so a window that ran coder-tm last would show
"iris-qa-coder-tm" even after a fresh restart into ui-tm — visible to
Jesper on the actual desktop. The old /rename-into-conversation
correction was disabled since ceea278 (2026-04-25) because it could
bleed into the next prompt; an OSC control sequence cannot bleed
(the terminal consumes it before the pty child sees it) so the
emitter is safe to fire at any moment.
"""

from __future__ import annotations

import io

from tubemail.manager import _emit_terminal_title, _terminal_title_bytes


class _RecordingStream:
    """Minimal stand-in for sys.stdout: exposes a .buffer with write /
    flush, and records every write so the test can assert exact bytes."""

    class _Buffer:
        def __init__(self) -> None:
            self.bytes_written = bytearray()
            self.flush_count = 0

        def write(self, data: bytes) -> int:
            self.bytes_written.extend(data)
            return len(data)

        def flush(self) -> None:
            self.flush_count += 1

    def __init__(self) -> None:
        self.buffer = _RecordingStream._Buffer()


class _TextOnlyStream:
    """Stub of a text-mode file-like without a .buffer attribute. The
    emitter must fall back to str-write on this shape."""

    def __init__(self) -> None:
        self.text_written = ""
        self.flush_count = 0

    def write(self, data: str) -> int:
        self.text_written += data
        return len(data)

    def flush(self) -> None:
        self.flush_count += 1


def test_title_bytes_emits_both_osc_sequences():
    """Two OSC sequences back-to-back: OSC 2 (window title) then OSC 1
    (tab/icon title), same payload. Terminals that only honor one
    silently drop the other, so both are always safe to send."""
    out = _terminal_title_bytes("iris-qa-ui-tm")
    assert out == b"\x1b]2;iris-qa-ui-tm\x07\x1b]1;iris-qa-ui-tm\x07"


def test_title_bytes_exact_bel_terminator():
    """The trailing byte is BEL (0x07), not ST (ESC \\). BEL is the
    de-facto title terminator across xterm-family emulators; some
    terminals accept ST too, but BEL is universally understood."""
    out = _terminal_title_bytes("x")
    assert out.endswith(b"\x07")
    assert b"\x07" in out[: len(out) // 2]  # BEL appears twice (once per OSC)


def test_title_bytes_uses_session_name_verbatim():
    """No sanitizing, no truncation: the emitter passes the session
    name through byte-for-byte. The manager's session_name is already
    validated at the wrapper level, and terminals will just drop or
    escape any weird character on their own."""
    out = _terminal_title_bytes("with spaces and-hyphens_and.dots")
    assert b"with spaces and-hyphens_and.dots" in out


def test_emit_writes_expected_bytes_to_buffer():
    """The public emitter routes to the .buffer of the target stream so
    raw bytes reach the terminal untranslated, and it flushes so the
    title updates before the child pty starts booting."""
    stream = _RecordingStream()
    _emit_terminal_title("iris-qa-ui-tm", out=stream)
    assert stream.buffer.bytes_written == b"\x1b]2;iris-qa-ui-tm\x07\x1b]1;iris-qa-ui-tm\x07"
    assert stream.buffer.flush_count == 1


def test_emit_survives_startup_and_restart_paired_calls():
    """The manager calls _emit_terminal_title once per iteration of the
    run loop — startup (restart_count=0) AND every restart. Simulate
    three iterations and assert the buffer accumulates the same title
    exactly three times, i.e. the emitter is safely idempotent when
    invoked repeatedly."""
    stream = _RecordingStream()
    _emit_terminal_title("iris-qa-ui-tm", out=stream)
    _emit_terminal_title("iris-qa-ui-tm", out=stream)
    _emit_terminal_title("iris-qa-ui-tm", out=stream)
    expected_one = b"\x1b]2;iris-qa-ui-tm\x07\x1b]1;iris-qa-ui-tm\x07"
    assert stream.buffer.bytes_written == expected_one * 3
    assert stream.buffer.flush_count == 3


def test_emit_falls_back_to_text_write_when_no_buffer():
    """Stubs, redirected streams, and pytest capture objects don't
    expose a .buffer. The emitter must still write the OSC sequence as
    a decoded string in that case rather than crashing."""
    stream = _TextOnlyStream()
    _emit_terminal_title("iris-qa-tm", out=stream)
    assert "\x1b]2;iris-qa-tm\x07" in stream.text_written
    assert "\x1b]1;iris-qa-tm\x07" in stream.text_written
    assert stream.flush_count == 1


def test_emit_swallows_closed_stream_gracefully():
    """A closed stdout must not crash the manager — the title is
    decorative. This covers the case where the manager runs headless
    (stdout has been closed by systemd / a supervisor) and the ioctl
    would otherwise raise OSError."""
    stream = _RecordingStream()
    # Poison the writer: raising OSError from .write is the shape a
    # broken pipe or closed fd surfaces.
    def _boom(_: bytes) -> int:
        raise OSError("broken pipe")

    stream.buffer.write = _boom  # type: ignore[assignment]
    # The call must return without raising.
    _emit_terminal_title("iris-qa-tm", out=stream)


def test_emit_swallows_valueerror_on_closed_stream():
    """Python's TextIO raises ValueError('I/O operation on closed
    file') rather than OSError when the underlying fd has been
    closed. Cover that shape too."""
    stream = _TextOnlyStream()

    def _closed(_: str) -> int:
        raise ValueError("I/O operation on closed file")

    stream.write = _closed  # type: ignore[assignment]
    _emit_terminal_title("iris-qa-tm", out=stream)


def test_emit_defaults_to_sys_stdout(monkeypatch):
    """When called with no `out` argument the emitter must target the
    live sys.stdout — that's how run() invokes it in production."""
    import sys as real_sys

    stream = _RecordingStream()
    monkeypatch.setattr(real_sys, "stdout", stream)
    # Force the manager module to see the monkeypatched sys.stdout as
    # its sys.stdout — since it imports `sys` at module scope.
    monkeypatch.setattr("tubemail.manager.sys.stdout", stream)

    _emit_terminal_title("actuatrix-tm")

    assert stream.buffer.bytes_written == b"\x1b]2;actuatrix-tm\x07\x1b]1;actuatrix-tm\x07"


def test_title_bytes_returns_bytes_type():
    """Callers thread this through file descriptor writes — must be
    bytes, not str. Pin the type so a future 'nicer' rewrite doesn't
    accidentally hand back a string that then fails os.write."""
    assert isinstance(_terminal_title_bytes("x"), bytes)
