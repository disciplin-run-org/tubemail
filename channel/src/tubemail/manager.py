"""Claude-tm process manager.

Runs `claude` as a child process inside a pseudo-terminal (pty), enabling
the manager to inject CLI commands (/compact, /clear, /exit, /rename) as
keystrokes — the same as if the user typed them.

Restart policy:
- Exit code 0, no restart signal → stop (user typed /exit)
- Exit code 0, restart signal received → restart with --continue
- Exit code non-zero → crash recovery, restart with --continue
- force_restart from manager channel → type /exit, restart with --continue
- force_stop from manager channel → type /exit, stop
- Commands like compact/clear/rename → type them into the pty

The manager registers as `<session>-manager` on the TubeMail hub and
maintains its own SSE subscription that survives Claude restarts.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import pty
import queue
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path
from typing import Any

import httpx
import psutil

from .local_ipc import SOCK_ENV, LocalIPCServer, default_sock_path

logger = logging.getLogger(__name__)

# Regex to strip ANSI escape sequences from pty output for text matching
_ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][0-9A-Z]")

# Anthropic transient 429 marker. We match the whitespace-stripped core so
# line wrapping and ANSI formatting in the pty output don't defeat it.
_RATE_LIMIT_MARKER = b"temporarilylimitingrequests"

# Progressive backoff before auto-typing "continue" after a rate-limit.
# Capped at 2 minutes per the requested policy.
_RATE_LIMIT_BACKOFF_S = (15, 30, 60, 120)

# If the typed "continue" runs cleanly for this long without a new rate-limit
# marker, treat the transient 429 as resolved and reset the backoff counter
# so the next rate-limit again starts at 15s (not wherever we left off).
_RATE_LIMIT_RESET_AFTER_S = 20

# Hub-frame delivery backoff. When pty_out / context-pct POSTs fail in a
# row, the manager sleeps for these intervals before the next try. Saturates
# at the last value. The pump_io thread is unaffected — it keeps draining
# the master pty regardless. These bounds only control how often background
# threads hit a degraded hub.
_FRAMES_BACKOFF_S = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0)

# After this many seconds of unbroken POST failures, give up the pty stream
# entirely. The hub's next pty_attach will start a fresh stream when the
# operator re-attaches. Without this cap a perpetually-broken hub burns
# CPU forever in the backoff loop.
_FRAMES_GIVE_UP_AFTER_S = 120.0


def _frames_backoff_s(consecutive_failures: int) -> float:
    """Pick the backoff for the Nth consecutive frame-delivery failure."""
    idx = min(max(consecutive_failures - 1, 0), len(_FRAMES_BACKOFF_S) - 1)
    return _FRAMES_BACKOFF_S[idx]


def _drain_queue(q: queue.Queue[bytes]) -> int:
    """Empty `q` of all currently-buffered bytes.

    Returns the number of items dropped. Used while a frame-delivery
    loop is backing off so the queue cannot grow unbounded during a hub
    outage.
    """
    dropped = 0
    while True:
        try:
            q.get_nowait()
            dropped += 1
        except queue.Empty:
            return dropped


def _normalize_pty_tail(tail: bytes) -> bytes:
    """Strip ANSI escapes and whitespace from a pty buffer slice so text
    patterns match regardless of line wrapping or terminal formatting."""
    return (
        _ANSI_RE.sub(b"", tail)
        .replace(b" ", b"")
        .replace(b"\n", b"")
        .replace(b"\r", b"")
    )


# `context\s+(\d+)%` shows up in claude's TUI status bar. Match the
# normalized form (no spaces) so it survives whatever ANSI/wrapping the
# screen rendered.
_CONTEXT_PCT_RE = re.compile(rb"context(\d{1,3})%")

# Markers that appear in claude's TUI ONLY while a request is in flight.
# When claude is at a quiet prompt waiting for input, none of these are
# on screen.
#
# - `(Xs·` is the running timer parenthetical that wraps the spinner
#   text ("Mulling… (8s · ↑512 tokens)"). Definitively in-flight.
# - The verb-ing nouns are the spinner labels claude cycles through
#   while generating tokens / running tools. They're absent from the
#   idle prompt.
#
# Whitespace/newlines are stripped by `_normalize_pty_tail` before the
# search, so multi-line word wrap doesn't defeat the patterns.
_ACTIVE_RE = re.compile(
    rb"\(\d+s"  # "(8s" — running timer parenthetical
    rb"|Mulling"
    rb"|Unfurling"
    rb"|thinking"
    rb"|stillthinking"
    rb"|thoughtfor"
    rb"|Brewedfor"  # past-tense; lingers briefly post-completion
    rb"|Callingtubemail"  # tool call ("Calling tubemail (ctrl+o…)")
    rb"|Running"  # bash tool spinner sub-text ("Running…")
)


def _is_actively_processing(tail: bytes) -> bool:
    """Return True if claude's TUI is mid-request right now.

    Used by the manager to push an authoritative busy/idle signal up to
    the hub so the roster doesn't have to guess from event-timeline
    decay heuristics. The manager has direct line of sight to the pty;
    the hub does not.

    The detector is a substring match on the normalized last-screen
    tail. False positives would happen only if claude's chat content
    itself contained one of the spinner labels — very unlikely outside a
    doc page about claude's own UI.
    """
    return _ACTIVE_RE.search(_normalize_pty_tail(tail)) is not None


def _parse_context_pct(tail: bytes) -> int | None:
    """Return the most recent context-window percent in a normalized pty tail,
    or None if no match.

    Multiple matches are possible while redraws overlap; pick the LAST
    one because newer text is appended later in the buffer.
    """
    matches = _CONTEXT_PCT_RE.findall(_normalize_pty_tail(tail))
    if not matches:
        return None
    try:
        pct = int(matches[-1])
    except ValueError:
        return None
    if 0 <= pct <= 100:
        return pct
    return None


def _contains_rate_limit(tail: bytes) -> bool:
    return _RATE_LIMIT_MARKER in _normalize_pty_tail(tail)


def _rate_limit_delay(retry_count: int) -> int:
    """Pick the backoff delay for the Nth consecutive rate-limit retry."""
    idx = min(retry_count, len(_RATE_LIMIT_BACKOFF_S) - 1)
    return _RATE_LIMIT_BACKOFF_S[idx]


def _pidfile_path(session_name: str) -> Path:
    return Path(f"/tmp/claude-tm-{session_name}.pid")


def _logfile_path(session_name: str) -> Path:
    return Path(f"/tmp/claude-tm-{session_name}.log")


def _install_stop_hook() -> None:
    """Idempotently register the tubemail Stop hook in the user's
    ~/.claude/settings.json so every assistant turn relays its reply to
    tubemail's bridge.

    This is part of claude-tm's contract: install tubemail, restart
    claude-tm, the relay just works — no manual scripts needed. The
    hook script itself self-skips when TM_WORKER_NAME is unset, so
    non-tubemail Claude Code sessions on the same machine are
    unaffected.

    Best-effort — any failure is logged and the manager continues.
    Without the hook installed, channel-mode workers fall back to
    the LLM-discipline path (call the `reply` tool explicitly), which
    is unreliable but not fatal.
    """
    import sys

    installer = Path(__file__).resolve().parents[2] / "hooks" / "install_stop_hook.py"
    if not installer.exists():
        logger.debug("install_stop_hook.py not found at %s; skipping", installer)
        return
    # end if
    try:
        proc = subprocess.run(
            [sys.executable, str(installer)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            logger.warning(
                "install_stop_hook returned %d: %s",
                proc.returncode,
                proc.stderr.strip()[:200],
            )
        elif proc.stderr.strip():
            # Installer logs to stderr only when something happened
            # (installed-or-already-installed are both rc=0; the
            # success case logs once when newly installed).
            logger.info("stop hook: %s", proc.stderr.strip()[:200])
        # end if
    except (OSError, subprocess.TimeoutExpired) as err:
        logger.warning("install_stop_hook failed: %s", err)
    # end try


# Exit code meanings for the bash restart-loop in scripts/claude-tm:
#   0 = clean exit (user typed /exit) — bash exits too
#   EXIT_UPDATE_MANAGER (42) = re-exec the python manager to pick up updated
#       tubemail source. bash adds --continue so claude resumes the same
#       conversation. Session name + role are unchanged.
#   anything else = crash; bash restarts up to a bounded count.
EXIT_UPDATE_MANAGER = 42


# /mcp dialog helpers (pure functions for testability) ─────────────────────


def _extract_mcp_server_list(screen: str) -> list[str]:
    """Parse an /mcp dialog screen and return the server names in list order.

    A server row looks like "name · status" where status contains one of
    connect / fail / auth / disabled (case-insensitive) or the ✔/✘
    glyphs. Section headers ("Project MCPs", "User MCPs", "claude.ai")
    are skipped because they lack a connection-style status.
    """
    servers: list[str] = []
    for line in screen.splitlines():
        stripped = line.lstrip("❯ ").strip()
        if "·" not in stripped:
            continue
        name, _, status = stripped.partition("·")
        name = name.strip()
        status_lc = status.strip().lower()
        if not name:
            continue
        if any(kw in status_lc for kw in ("connect", "fail", "auth", "disabl")):
            servers.append(name)
    return servers


def _server_dialog_position(screen: str, server_name: str) -> int | None:
    """Return the 0-based position of server_name in the /mcp list, or None."""
    servers = _extract_mcp_server_list(screen)
    try:
        return servers.index(server_name)
    except ValueError:
        return None


class _PtyChild:
    """Manages a child process running in a pseudo-terminal.

    The pty lets us:
    - Forward the user's terminal I/O to the child (normal interactive use)
    - Inject commands by writing to the pty master (like the user typing)
    """

    def __init__(self, cmd: list[str], session_name: str = ""):
        self._cmd = cmd
        self._session_name = session_name
        self._master_fd: int | None = None
        self._child_pid: int | None = None
        # Rolling buffer of recent pty output for screenshots
        self._screen_buf = bytearray()
        self._screen_buf_max = 32768  # 32KB of recent output
        self._screen_lock = threading.Lock()
        # Optional live-stream tap: pump_io writes every chunk read from
        # the pty master to this queue if attached. The web-UI bridge
        # consumes from here. Dropping bytes is preferable to blocking
        # the pty reader, so the queue is bounded and oldest is dropped
        # on overflow.
        self._stream_queue: queue.Queue[bytes] | None = None
        # Rate-limit auto-retry state: Anthropic occasionally 429s with
        # "API Error: Server is temporarily limiting requests". We detect
        # it in the pty output and type "continue" after a progressive
        # backoff (15s, 30s, 60s, 120s, capped) so the session recovers
        # without user intervention.
        self._rl_waiting = False
        self._rl_retry_count = 0
        self._rl_timer: threading.Timer | None = None
        # After a successful 'continue', start a reset timer. If it fires
        # without a new rate-limit interrupting, drop retry_count back to 0.
        self._rl_reset_timer: threading.Timer | None = None

    @property
    def pid(self) -> int | None:
        return self._child_pid

    def _sync_terminal_size(self) -> None:
        """Copy the real terminal's dimensions to the pty."""
        if self._master_fd is None:
            return
        try:
            size = os.get_terminal_size(sys.stdin.fileno())
            winsize = struct.pack("HHHH", size.lines, size.columns, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        except (OSError, ValueError):
            pass

    def start(self) -> None:
        """Spawn the child in a pty with the real terminal's dimensions."""
        self._child_pid, self._master_fd = pty.fork()
        if self._child_pid == 0:
            # Child process — exec claude (inherits parent env including TM_WORKER_NAME)
            os.execvp(self._cmd[0], self._cmd)
            # execvp doesn't return
        # Parent — set pty to match the real terminal size
        self._sync_terminal_size()

    def get_screenshot(self, max_lines: int = 0) -> str:
        """Return what's currently visible on the terminal screen.

        Uses the actual terminal height to return exactly one screenful
        from the tail of the buffer. Pass max_lines to override.
        """
        if max_lines <= 0:
            try:
                max_lines = os.get_terminal_size(sys.stdin.fileno()).lines
            except (OSError, ValueError):
                max_lines = 50
        with self._screen_lock:
            raw = bytes(self._screen_buf)
        # Strip ANSI escape sequences
        clean = _ANSI_RE.sub(b"", raw)
        text = clean.decode("utf-8", errors="replace")
        lines = text.splitlines()
        screen = lines[-max_lines:] if len(lines) > max_lines else lines
        # Strip empty trailing lines
        while screen and not screen[-1].strip():
            screen.pop()
        return "\n".join(screen)

    def send_command(self, text: str) -> None:
        """Inject a command into the child's stdin as if the user typed it."""
        if self._master_fd is not None:
            # Use \r (carriage return) — that's what the Enter key sends in raw terminal mode
            os.write(self._master_fd, (text + "\r").encode())

    def send_bytes(self, data: bytes) -> None:
        """Send raw bytes to the child's stdin."""
        if self._master_fd is not None:
            os.write(self._master_fd, data)

    def attach_stream(self) -> object:
        """Atomically snapshot the current screen and start streaming.

        Returns (initial_bytes, queue). Caller posts initial_bytes to
        the consumer to render the existing screen, then drains queue
        for live updates. Both are taken under one lock so the boundary
        is exact — no byte appears in both.
        """
        with self._screen_lock:
            initial = bytes(self._screen_buf)
            if self._stream_queue is None:
                self._stream_queue = queue.Queue(maxsize=1024)
            q = self._stream_queue
        return initial, q

    def detach_stream(self) -> None:
        """Stop the live-stream tap.

        Bytes from pump_io are no longer copied to the queue.
        Idempotent.
        """
        with self._screen_lock:
            self._stream_queue = None

    def pty_size(self) -> tuple[int, int] | None:
        """Return (cols, rows) of the pty master, or None if not open."""
        if self._master_fd is None:
            return None
        try:
            winsize = fcntl.ioctl(self._master_fd, termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols, _, _ = struct.unpack("HHHH", winsize)
            return cols, rows
        except OSError:
            return None

    def trigger_redraw(self) -> None:
        """Force the slave-side application to redraw by toggling the pty size.

        TUIs (Claude's Ink, vim, htop, …) redraw on SIGWINCH; a one-row
        delta is enough to provoke it without a long visual flicker.
        Used on web-UI attach so the cursor lands at the real terminal
        cursor position instead of at the end of whatever bytes happened
        to be in the screen buffer.
        """
        if self._master_fd is None:
            return
        size = self.pty_size()
        if size is None:
            return
        cols, rows = size
        try:
            # Briefly drop one row, then restore. The slave receives
            # SIGWINCH twice; the second one with the original size
            # leaves the pty back where it was.
            tmp = struct.pack("HHHH", max(rows - 1, 1), cols, 0, 0)
            full = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, tmp)
            time.sleep(0.02)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, full)
        except OSError as e:
            logger.debug("trigger_redraw failed: %s", e)

    def _get_screen_text(self) -> str:
        """Thread-safe ANSI-stripped snapshot of the current screen buffer."""
        with self._screen_lock:
            raw = bytes(self._screen_buf)
        return _ANSI_RE.sub(b"", raw).decode("utf-8", errors="replace")

    def _wait_for_screen(
        self, predicate, timeout_s: float, poll_s: float = 0.15
    ) -> bool:
        """Poll the screen buffer until predicate(text) is truthy or
        timeout."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate(self._get_screen_text()):
                return True
            time.sleep(poll_s)
        return False

    def reconnect_mcp(self, server_name: str) -> dict:
        """Deterministically drive /mcp → select server → Reconnect.

        Returns {"ok": bool, "server": str, "detail": str}. Meant to be
        called from a background thread — writes to the pty and polls
        the screen buffer with short sleeps between steps.
        """
        if self._master_fd is None:
            return {"ok": False, "server": server_name, "detail": "no pty"}
        master = self._master_fd

        # 1. Open the /mcp dialog.
        with self._screen_lock:
            self._screen_buf.clear()
        os.write(master, b"/mcp\r")
        if not self._wait_for_screen(
            lambda s: "Manage MCP servers" in s or "MCP Config" in s,
            timeout_s=5.0,
        ):
            # If /mcp opened the Remote Control view instead (the "Enter to
            # view" status-bar shortcut can intercept), escape and bail —
            # we can't recover without knowing which mode we're in.
            os.write(master, b"\x1b")
            return {
                "ok": False,
                "server": server_name,
                "detail": "/mcp dialog did not open within 5s",
            }
        time.sleep(0.3)

        # 2. Locate the target server's position in the rendered list.
        screen = self._get_screen_text()
        pos = _server_dialog_position(screen, server_name)
        if pos is None:
            os.write(master, b"\x1b")
            servers = _extract_mcp_server_list(screen)
            return {
                "ok": False,
                "server": server_name,
                "detail": f"server not found in dialog; listed: {servers}",
            }

        # 3. Navigate down `pos` times (cursor starts at position 0 on fresh open).
        for _ in range(pos):
            os.write(master, b"\x1b[B")
            time.sleep(0.15)

        # 4. Enter to open server detail.
        os.write(master, b"\r")
        if not self._wait_for_screen(
            lambda s: "Reconnect" in s and ("Authenticate" in s or "Disable" in s),
            timeout_s=5.0,
        ):
            os.write(master, b"\x1b")
            return {
                "ok": False,
                "server": server_name,
                "detail": "detail view did not appear (expected Authenticate/Reconnect/Disable menu)",
            }
        time.sleep(0.3)

        # 5. Numbered selection is more reliable than arrow-navigation in
        #    the sub-menu: type "2" then Enter to pick Reconnect.
        with self._screen_lock:
            self._screen_buf.clear()
        os.write(master, b"2")
        time.sleep(0.2)
        os.write(master, b"\r")

        # 6. Wait for the success/failure marker.
        if self._wait_for_screen(
            lambda s: f"Reconnected to {server_name}" in s,
            timeout_s=20.0,
        ):
            return {"ok": True, "server": server_name, "detail": "reconnected"}

        # Give any explicit failure message a brief window to surface.
        if self._wait_for_screen(
            lambda s: server_name in s
            and ("failed" in s.lower() or "error" in s.lower()),
            timeout_s=2.0,
        ):
            return {
                "ok": False,
                "server": server_name,
                "detail": "reconnect finished with failure marker on screen",
            }

        return {
            "ok": False,
            "server": server_name,
            "detail": "no confirmation within 20s — check manually",
        }

    def pump_io(self) -> int:
        """Forward I/O between the user's terminal and the child's pty.

        Blocks until the child exits. Returns the child's exit code.

        Auto-accepts the development channels warning prompt by watching
        for the "I am using this for local development" text in the pty
        output and sending Enter (\\r) automatically.
        """
        stdin_fd = sys.stdin.fileno()
        master = self._master_fd
        assert master is not None

        # Forward terminal resize events to the pty
        _prev_sigwinch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, lambda *_: self._sync_terminal_size())

        old_settings = termios.tcgetattr(stdin_fd)

        # ── Startup auto-accept ──────────────────────────────────────────
        # Claude Code shows interactive prompts during startup: dev-channels
        # warning, MCP server trust, resume/compact menu.
        #
        # SAFETY: we only check the most recent screen-worth of output
        # (~4KB), not a rolling history. Prompts are always the last thing
        # on screen. Group dedup ensures only one match per prompt type.
        _SCREEN_CHECK_SIZE = 4096
        # (match_bytes, response_bytes, label, group)
        _AUTO_ACCEPT_PATTERNS = [
            # Dev-channels warning: "I am using this for local development" → Enter
            (b"localdevelopment", b"\r", "dev-channels", "dev-channels"),
            # Trust MCP servers — DISABLED. Its \r bleeds into the resume
            # menu that appears immediately after, selecting option 1
            # (compact) before the resume pattern can fire. Trust-MCP is
            # a one-time prompt; user can accept manually.
            # (b"Trustserversfrom", b"\r", "trust-mcp-servers", "trust-mcp"),
            # (b"newMCPservers", b"\r", "new-mcp-servers", "trust-mcp"),
            #
            # Resume prompt — send "3" to select "Don't ask me again".
            # Actual prompt text (2026-04-16):
            #   ❯ 1. Resume from summary (recommended)
            #     2. Resume full session as-is
            #     3. Don't ask me again
            (b"Resumefromsummary", b"3\r", "resume-menu", "resume"),
        ]
        _accepted_groups: set[str] = set()

        try:
            tty.setraw(stdin_fd)

            while True:
                try:
                    fds = [stdin_fd, master]
                    rlist, _, _ = select.select(fds, [], [], 0.1)
                except (ValueError, OSError):
                    break

                if stdin_fd in rlist:
                    try:
                        data = os.read(stdin_fd, 4096)
                        if not data:
                            break
                        os.write(master, data)
                    except OSError:
                        break

                if master in rlist:
                    try:
                        data = os.read(master, 4096)
                        if not data:
                            break
                        os.write(sys.stdout.fileno(), data)

                        # Feed rolling screen buffer for screenshots AND
                        # the live-stream tap (web-UI bridge) under one
                        # lock so the snapshot+queue handoff during
                        # attach is atomic.
                        with self._screen_lock:
                            self._screen_buf.extend(data)
                            if len(self._screen_buf) > self._screen_buf_max:
                                self._screen_buf = self._screen_buf[
                                    -self._screen_buf_max :
                                ]
                            q = self._stream_queue
                        if q is not None:
                            try:
                                q.put_nowait(data)
                            except queue.Full:
                                # Drop oldest, push new — terminal data
                                # is realtime; old bytes are useless.
                                try:
                                    q.get_nowait()
                                    q.put_nowait(data)
                                except (queue.Empty, queue.Full):
                                    pass

                        # Auto-accept: check only the recent screen tail.
                        # Timing matters: we delay 0.3s after each auto-accept
                        # to let the child consume the response before we send
                        # another. Without this, a \r for dev-channels can bleed
                        # into the compact prompt that appears immediately after.
                        if len(_accepted_groups) < len(
                            {g for _, _, _, g in _AUTO_ACCEPT_PATTERNS}
                        ):
                            with self._screen_lock:
                                tail = bytes(self._screen_buf[-_SCREEN_CHECK_SIZE:])
                            # Strip ANSI escapes AND whitespace so patterns
                            # like b"compactyourconversation" match regardless
                            # of spacing, line breaks, or formatting.
                            clean = (
                                _ANSI_RE.sub(b"", tail)
                                .replace(b" ", b"")
                                .replace(b"\n", b"")
                                .replace(b"\r", b"")
                            )
                            for (
                                pattern,
                                response,
                                label,
                                group,
                            ) in _AUTO_ACCEPT_PATTERNS:
                                if group in _accepted_groups:
                                    continue
                                if pattern in clean:
                                    # Delay to let the child consume any prior
                                    # buffered keystrokes before we inject ours.
                                    time.sleep(0.3)
                                    os.write(master, response)
                                    _accepted_groups.add(group)
                                    logger.info(
                                        "auto-accepted: %s (group=%s)", label, group
                                    )
                                    # Clear screen buffer so stale text doesn't
                                    # re-trigger patterns on the next cycle.
                                    with self._screen_lock:
                                        self._screen_buf.clear()
                                    # TEMPORARILY DISABLED — rename timer fires
                                    # blind and can bleed into the next prompt.
                                    # if label == "dev-channels":
                                    #     threading.Timer(
                                    #         2.0,
                                    #         lambda: os.write(master, f"/rename {self._session_name}\r".encode()),
                                    #     ).start()
                                    break

                        # Rate-limit auto-retry: detect Anthropic's transient
                        # "API Error: Server is temporarily limiting requests"
                        # and type "continue" after progressive backoff.
                        if not self._rl_waiting:
                            with self._screen_lock:
                                rl_tail = bytes(self._screen_buf[-_SCREEN_CHECK_SIZE:])
                            if _contains_rate_limit(rl_tail):
                                # New rate-limit before the reset timer fired
                                # means the previous 'continue' didn't hold —
                                # cancel the reset and keep climbing the curve.
                                if self._rl_reset_timer is not None:
                                    self._rl_reset_timer.cancel()
                                    self._rl_reset_timer = None
                                delay_s = _rate_limit_delay(self._rl_retry_count)
                                self._rl_waiting = True
                                self._rl_retry_count += 1
                                logger.warning(
                                    "rate-limit detected — typing 'continue' in %ds (retry #%d)",
                                    delay_s,
                                    self._rl_retry_count,
                                )
                                with self._screen_lock:
                                    self._screen_buf.clear()
                                self._rl_timer = threading.Timer(
                                    delay_s, self._rl_fire_continue
                                )
                                self._rl_timer.daemon = True
                                self._rl_timer.start()
                    except OSError:
                        break

                # Check if child is still alive
                try:
                    pid, status = os.waitpid(self._child_pid, os.WNOHANG)
                    if pid != 0:
                        if os.WIFEXITED(status):
                            return os.WEXITSTATUS(status)
                        return 1
                except ChildProcessError:
                    return 1

        finally:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
            signal.signal(signal.SIGWINCH, _prev_sigwinch or signal.SIG_DFL)

        # Wait for child if it hasn't exited yet
        try:
            _, status = os.waitpid(self._child_pid, 0)
            if os.WIFEXITED(status):
                return os.WEXITSTATUS(status)
            return 1
        except ChildProcessError:
            return 1

    def _rl_fire_continue(self) -> None:
        """Type 'continue' after the rate-limit backoff elapsed."""
        self._rl_waiting = False
        if self._master_fd is None:
            return
        try:
            os.write(self._master_fd, b"continue\r")
            logger.warning("rate-limit retry: typed 'continue'")
        except OSError:
            return
        # Arm a reset: if no new rate-limit marker appears in the next
        # _RATE_LIMIT_RESET_AFTER_S, the retry counter drops back to 0 so
        # a later transient 429 again starts at the shortest backoff step.
        if self._rl_reset_timer is not None:
            self._rl_reset_timer.cancel()
        self._rl_reset_timer = threading.Timer(
            _RATE_LIMIT_RESET_AFTER_S, self._rl_reset_counter
        )
        self._rl_reset_timer.daemon = True
        self._rl_reset_timer.start()

    def _rl_reset_counter(self) -> None:
        """Reset the retry counter once the typed 'continue' has held."""
        if self._rl_retry_count > 0:
            logger.info(
                "rate-limit: continue held for %ds — resetting retry count from %d to 0",
                _RATE_LIMIT_RESET_AFTER_S,
                self._rl_retry_count,
            )
        self._rl_retry_count = 0
        self._rl_reset_timer = None

    def terminate(self) -> None:
        """Send SIGTERM to the child."""
        if self._child_pid is not None:
            try:
                os.kill(self._child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def kill(self) -> None:
        """Send SIGKILL to the child."""
        if self._child_pid is not None:
            try:
                os.kill(self._child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self) -> None:
        """Close the pty master fd."""
        if self._rl_timer is not None:
            self._rl_timer.cancel()
            self._rl_timer = None
        if self._rl_reset_timer is not None:
            self._rl_reset_timer.cancel()
            self._rl_reset_timer = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None


class _ManagerChannelListener:
    """Background thread subscribed to the manager's SSE stream on the hub.

    Translates hub commands into actions on the pty child: inject CLI
    commands, kill the child, or report health stats.
    """

    def __init__(
        self,
        hub_url: str,
        manager_name: str,
        secret: str,
        session_name: str,
    ):
        self._hub_url = hub_url.rstrip("/")
        self._name = manager_name
        self._secret = secret
        self._session_name = session_name
        self._headers = {"Authorization": f"Bearer {secret}"}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._child: _PtyChild | None = None
        self._child_start_time: float = 0.0
        self._restart_requested = False
        self._stop_requested = False
        self._update_manager_requested = False
        # pty-bridge streaming state (v1 of the Integrated Terminal cap).
        # When a browser attaches via the WS bridge, the hub fans pty_attach
        # here; we spawn a background thread that copies pty master bytes
        # to POST /tubemail/<worker>/pty-out in batches.
        self._pty_stream_stop: threading.Event | None = None
        self._pty_stream_thread: threading.Thread | None = None
        # Set when pty_attach arrives before the pty child is ready
        # (e.g. between manager re-exec and claude startup). Cleared in
        # set_child() once we kick off the deferred stream, or in
        # _stop_pty_stream() if the operator detaches first.
        self._pty_attach_pending: bool = False
        # Last context-pct value we POSTed to the hub. Used to suppress
        # repeated POSTs when the parsed value hasn't changed. None
        # means "never posted yet" — the next non-None parse will fire.
        self._last_context_pct: int | None = None
        # Last "is claude actively processing" state we POSTed. Same
        # change-only push contract as context-pct. None means we
        # haven't reported yet; the first scan will always fire.
        self._last_active_state: bool | None = None
        self._context_pct_thread: threading.Thread | None = None

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def update_manager_requested(self) -> bool:
        return self._update_manager_requested

    def clear_flags(self) -> None:
        self._restart_requested = False
        self._stop_requested = False
        self._update_manager_requested = False

    def set_child(self, child: _PtyChild | None) -> None:
        self._child = child
        self._child_start_time = time.time() if child else 0.0
        # If a pty client attached while the child was restarting, kick
        # off the stream now that the new pty master exists.
        if child is not None and getattr(self, "_pty_attach_pending", False):
            logger.info("set_child: starting deferred pty stream")
            self._pty_attach_pending = False
            self._start_pty_stream()

    def start(self) -> None:
        self._register()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._context_pct_thread = threading.Thread(
            target=self._context_pct_loop,
            daemon=True,
        )
        self._context_pct_thread.start()

    def _context_pct_loop(self) -> None:
        """Periodically scan the child's screen buffer and push two
        derived signals to the hub when they change:

        - `context X%` — the TUI status bar's context-window indicator,
          rendered as the "Ctx" column in the roster.
        - `is_active` — whether claude is mid-request (spinner labels
          like "Mulling…" + the "(8s · ↑512 tokens)" running timer
          are present in the recent screen). The hub uses this as the
          authoritative busy/idle signal in `WorkerState.status_state()`.

        Same loop, same scan, same change-only push contract — both
        signals POST only when their value differs from the last one
        we sent, so an idle worker is silent on the wire. Exits when
        stop_event is set.

        Failure handling: when the hub is slow or down, consecutive POST
        failures back off exponentially via _frames_backoff_s instead of
        firing every 3s into a wall. The 3s base interval resumes once a
        POST succeeds.
        """
        consecutive_failures = 0
        while True:
            wait_s = (
                3.0
                if consecutive_failures == 0
                else _frames_backoff_s(consecutive_failures)
            )
            if self._stop_event.wait(wait_s):
                return
            child = self._child
            if child is None or child._master_fd is None:
                continue
            with child._screen_lock:
                tail = bytes(child._screen_buf)
            # Derive both signals from the same snapshot.
            pct = _parse_context_pct(tail)
            active = _is_actively_processing(tail)

            failed_this_tick = False

            if pct != self._last_context_pct:
                ok = self._post_signal(
                    self._context_pct_url(),
                    {"pct": pct},
                    label="context-pct",
                )
                if ok:
                    self._last_context_pct = pct
                else:
                    failed_this_tick = True

            if active != self._last_active_state:
                ok = self._post_signal(
                    self._active_state_url(),
                    {"is_active": active},
                    label="active-state",
                )
                if ok:
                    self._last_active_state = active
                else:
                    failed_this_tick = True

            if failed_this_tick:
                consecutive_failures += 1
            elif consecutive_failures > 0:
                logger.info(
                    "status loop: recovered after %d failures",
                    consecutive_failures,
                )
                consecutive_failures = 0

    def _post_signal(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        label: str,
    ) -> bool:
        """POST a small status-signal JSON payload.

        Returns True on a
        2xx-ish response, False on transport error or 4xx/5xx. Used by
        `_context_pct_loop` for the context-pct and is-active signals
        so a 404 from one doesn't block the other.
        """
        try:
            resp = httpx.post(
                url,
                json=payload,
                headers=self._headers,
                timeout=2.0,
            )
            if resp.status_code < 400:
                return True
            # 404 here means the active-state endpoint isn't deployed
            # yet — quiet that case so logs aren't spammed during a
            # rolling deploy.
            log = logger.debug if resp.status_code == 404 else logger.warning
            log(
                "%s POST %d: %s",
                label,
                resp.status_code,
                resp.text[:120],
            )
            return False
        except Exception as e:
            logger.debug("%s POST failed: %s", label, e)
            return False

    def stop(self, clean: bool = False) -> None:
        """Stop the listener.

        Set clean=True when the session is ending because the user
        /exit'd (not on crash/update). Posts /goodbye instead of
        /unregister so the hub records a clean exit.
        """
        self._stop_event.set()
        if clean:
            self._goodbye()
        else:
            self._unregister()

    def _url(self, path: str) -> str:
        return f"{self._hub_url}/tubemail/{self._name}{path}"

    def _register(self) -> None:
        from tubemail import __version__ as _fwd_version

        try:
            resp = httpx.post(
                self._url("/register"),
                json={
                    "cwd": str(Path.cwd()),
                    "pid": os.getpid(),
                    "forwarder_version": _fwd_version,
                },
                headers=self._headers,
                timeout=5.0,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "manager registration rejected (%s %d): %s",
                    self._name,
                    resp.status_code,
                    resp.text[:200],
                )
            else:
                logger.info(
                    "manager registered as %s (v%s)",
                    self._name,
                    _fwd_version,
                )
        except Exception as e:
            logger.warning(
                "manager registration failed (%s): %s: %s",
                self._name,
                type(e).__name__,
                e,
            )

    def _unregister(self) -> None:
        try:
            httpx.post(self._url("/unregister"), headers=self._headers, timeout=2.0)
        except Exception:
            pass

    def _goodbye(self) -> None:
        """Tell the hub we're shutting down cleanly (user typed /exit).

        Differs from _unregister: /goodbye sets exited_cleanly=True on the
        worker state, so list_workers can distinguish clean exits from
        crashes/hangs/kills where no POST happens.
        """
        try:
            httpx.post(self._url("/goodbye"), headers=self._headers, timeout=2.0)
        except Exception:
            pass

    def _post_screenshot(self, max_lines: int = 50) -> None:
        """Capture the current screen and post it as an outbound event."""
        child = self._child
        if child:
            screen = child.get_screenshot(max_lines=max_lines)
        else:
            screen = "(no child process running)"
        try:
            httpx.post(
                self._url("/outbound"),
                json={"text": screen, "meta": {"kind": "screenshot"}},
                headers=self._headers,
                timeout=5.0,
            )
        except Exception:
            logger.warning("failed to post screenshot")

    def _post_health(self) -> None:
        stats: dict = {
            "child_alive": False,
            "cpu_percent": 0.0,
            "memory_mb": 0.0,
            "uptime_s": 0,
        }
        child = self._child
        if child and child.pid:
            try:
                proc = psutil.Process(child.pid)
                stats["child_alive"] = proc.is_running()
                stats["cpu_percent"] = proc.cpu_percent(interval=0.5)
                stats["memory_mb"] = round(proc.memory_info().rss / (1024 * 1024), 1)
                stats["uptime_s"] = int(time.time() - self._child_start_time)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            httpx.post(
                self._url("/outbound"),
                json={"text": json.dumps(stats), "meta": {"kind": "health_response"}},
                headers=self._headers,
                timeout=5.0,
            )
        except Exception:
            logger.warning("failed to post health response")

    def _handle_event(self, event_type: str, data: dict) -> None:
        # pty_* events are a separate event-kind family — dispatch them
        # before the channel_event kind-matching because they don't have
        # the meta.kind / content shape.
        if event_type == "pty_attach":
            self._start_pty_stream()
            return
        if event_type == "pty_detach":
            self._stop_pty_stream()
            return
        if event_type == "pty_data":
            hex_str = data.get("bytes_hex", "")
            if hex_str and self._child is not None:
                try:
                    self._child.send_bytes(bytes.fromhex(hex_str))
                except ValueError:
                    logger.warning("pty_data: bad hex payload")
            return
        if event_type == "pty_resize":
            cols = int(data.get("cols", 80))
            rows = int(data.get("rows", 24))
            if self._child is not None and self._child._master_fd is not None:
                try:
                    winsize = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(self._child._master_fd, termios.TIOCSWINSZ, winsize)
                except OSError as e:
                    logger.warning("pty_resize: %s", e)
            return

        meta = data.get("meta", {})
        kind = meta.get("kind", "") or data.get("content", "")
        child = self._child

        if kind == "force_restart":
            logger.info("manager: force_restart — typing /exit into session")
            self._restart_requested = True
            if child:
                child.send_command("/exit")

        elif kind == "force_stop":
            logger.info("manager: force_stop — typing /exit into session")
            self._stop_requested = True
            if child:
                child.send_command("/exit")

        elif kind == "restart":
            logger.info("manager: restart signal from /restart skill")
            self._restart_requested = True
            if child:
                child.send_command("/exit")

        elif kind == "update_manager":
            logger.info(
                "manager: update_manager — exiting with %d to trigger re-exec",
                EXIT_UPDATE_MANAGER,
            )
            self._update_manager_requested = True
            self._stop_requested = True
            if child:
                child.send_command("/exit")

        elif kind == "health_check":
            logger.info("manager: health_check")
            self._post_health()

        elif kind == "screenshot" or kind.startswith("screenshot"):
            max_lines = 50
            if ":" in kind:
                try:
                    max_lines = int(kind.split(":", 1)[1])
                except ValueError:
                    pass
            logger.debug("manager: screenshot (%d lines)", max_lines)
            self._post_screenshot(max_lines=max_lines)

        elif kind == "compact":
            logger.info("manager: typing /compact into session")
            if child:
                child.send_command("/compact")

        elif kind == "clear":
            logger.info("manager: typing /clear into session")
            if child:
                child.send_command("/clear")

        elif kind.startswith("rename:"):
            new_name = kind.split(":", 1)[1]
            logger.info("manager: typing /rename %s into session", new_name)
            if child:
                child.send_command(f"/rename {new_name}")

        elif kind.startswith("type:"):
            raw = kind.split(":", 1)[1]
            logger.info("manager: typing raw command into session")
            if child:
                child.send_command(raw)

        elif kind.startswith("keystroke:"):
            # Raw bytes as hex-encoded string (no \r appended).
            # Used for interactive dialog navigation: arrow keys, Escape, etc.
            hex_str = kind.split(":", 1)[1]
            raw_bytes = bytes.fromhex(hex_str)
            logger.info("manager: sending raw keystroke (%d bytes)", len(raw_bytes))
            if child:
                # Clear buffer BEFORE sending so the next screenshot
                # only contains output that appeared after this keystroke.
                with child._screen_lock:
                    child._screen_buf.clear()
                child.send_bytes(raw_bytes)

        elif kind.startswith("reconnect_mcp:"):
            server = kind.split(":", 1)[1]
            logger.info("manager: driving /mcp reconnect for server=%s", server)
            if child:
                threading.Thread(
                    target=self._run_reconnect_mcp,
                    args=(child, server),
                    daemon=True,
                ).start()

    def _worker_name_for_pty(self) -> str:
        """The worker name the browser connects to for this session.

        The manager registered as `<session_name>-manager`; pty-out
        POSTs go to the plain session name (that's what the browser WS
        targets).
        """
        return self._session_name

    def _pty_out_url(self) -> str:
        return f"{self._hub_url}/tubemail/{self._worker_name_for_pty()}/pty-out"

    def _pty_control_url(self) -> str:
        return f"{self._hub_url}/tubemail/{self._worker_name_for_pty()}/pty-control"

    def _context_pct_url(self) -> str:
        return f"{self._hub_url}/tubemail/{self._worker_name_for_pty()}/context-pct"

    def _active_state_url(self) -> str:
        return f"{self._hub_url}/tubemail/{self._worker_name_for_pty()}/active"

    def _post_pty_size(self) -> None:
        """Tell attached browser clients the current pty cols/rows so their
        xterm renders at the same size claude is rendering for.

        Without this, the browser uses its FitAddon-derived size and the
        user sees output at one size while the real terminal sees it at
        another.
        """
        if self._child is None:
            return
        size = self._child.pty_size()
        if size is None:
            return
        cols, rows = size
        try:
            httpx.post(
                self._pty_control_url(),
                json={"kind": "size", "cols": cols, "rows": rows},
                headers={**self._headers, "Content-Type": "application/json"},
                timeout=2.0,
            )
        except Exception as e:
            logger.debug("post_pty_size failed: %s", e)

    def _start_pty_stream(self) -> None:
        """Handle a `pty_attach` event from the hub.

        Two responsibilities, separately idempotent:

        1. **Re-pump size + redraw on every attach.** The first attach
           starts the stream loop which dumps the screen buffer and
           triggers a TUI repaint. Subsequent attaches (pop-out windows,
           extra tabs) join an already-running stream loop, but they
           still need a fresh size echo and a SIGWINCH-driven repaint
           so their xterm renders against current pty state instead of
           an empty alt-screen. We always run those here — not inside
           the loop.

        2. **Start the stream loop once.** The loop attaches its own
           queue tap to the pty reader and POSTs bytes to the hub
           forever. Idempotent: a second call while the thread is alive
           is a no-op for the loop itself.

        Race with manager-restart: pty_attach can arrive in the window
        between Python re-exec and claude startup, when `_child` is
        still None. We mark it pending so `set_child()` can start the
        stream as soon as the new claude child is wired up.
        """
        # Always: tell new (and existing) clients the current size and
        # trigger a TUI repaint. Both helpers no-op safely when no
        # clients are attached on the hub side.
        #
        # _post_pty_size hits the hub over HTTP and can take up to
        # timeout=2.0s when the hub is degraded. We're called from the
        # SSE event-handler thread, so a slow POST here would queue up
        # ALL subsequent SSE events (force_restart, clear, keystroke,
        # etc.) for that long. Fire it on a daemon thread so the SSE
        # loop stays responsive.
        threading.Thread(
            target=self._post_pty_size,
            daemon=True,
        ).start()
        if self._child is not None:
            try:
                self._child.trigger_redraw()
            except Exception as e:
                logger.debug("trigger_redraw on attach failed: %s", e)

        if self._pty_stream_thread and self._pty_stream_thread.is_alive():
            logger.debug("pty stream already running — refreshed new client")
            return
        if self._child is None or self._child._master_fd is None:
            logger.info(
                "pty_attach: no pty child yet — deferring stream start "
                "until set_child() runs",
            )
            self._pty_attach_pending = True
            return
        logger.info(
            "pty_attach: starting pty stream for %s", self._worker_name_for_pty()
        )
        self._pty_stream_stop = threading.Event()
        self._pty_stream_thread = threading.Thread(
            target=self._pty_stream_loop,
            args=(self._pty_stream_stop,),
            daemon=True,
        )
        self._pty_stream_thread.start()

    def _stop_pty_stream(self) -> None:
        """Signal the stream thread to exit.

        Idempotent. Also clears any pending-attach intent — pty_detach
        means the operator is no longer asking for a stream, so we
        shouldn't auto-start one when set_child fires next.
        """
        self._pty_attach_pending = False
        if self._pty_stream_stop is not None:
            logger.info("pty_detach: stopping pty stream")
            self._pty_stream_stop.set()
        # Thread exits on its own; we don't join here to avoid blocking
        # the SSE loop on a socket timeout.
        self._pty_stream_thread = None
        self._pty_stream_stop = None

    def _pty_stream_loop(self, stop: threading.Event) -> None:
        """Copy pty master output to the hub in real time. Runs in a background
        thread.

        Pumping is queue-based, not buffer-polling: pump_io appends
        every chunk read from the master FD into a bounded queue
        (`child._stream_queue`); this loop drains that queue and
        POSTs to the hub. Bursty redraws (Claude's TUI repaints) no
        longer get lost when the screen buffer wraps.

        On attach, the existing screen contents are sent first so
        the browser xterm renders what the operator sees in the real
        terminal — no blank screen waiting on the next keystroke.

        This is additive: tm_screenshot / tm_keystroke keep working
        alongside (they read/write the same pty master).

        Failure handling: this loop never blocks pump_io, but a slow
        or down hub can still pile up failed POSTs. We track
        consecutive failures and back off exponentially (0.5s → 30s).
        After ~_FRAMES_GIVE_UP_AFTER_S of nothing-but-failures we stop
        the stream entirely — the hub's next pty_attach event will
        start a fresh one. While backing off we keep draining the
        queue so memory doesn't grow.
        """
        child = self._child
        if child is None or child._master_fd is None:
            return
        url = self._pty_out_url()

        # Atomic snapshot+attach: bytes that were in screen_buf at
        # this moment go in `initial`; bytes that arrive after go in
        # `q`. No duplication. Size echo + redraw already happened in
        # `_start_pty_stream` (and they fire on every attach, not just
        # this first one).
        initial, q = child.attach_stream()

        if initial:
            try:
                resp = httpx.post(
                    url,
                    content=initial,
                    headers={
                        **self._headers,
                        "Content-Type": "application/octet-stream",
                    },
                    timeout=2.0,
                )
                if resp.status_code == 404:
                    logger.debug(
                        "pty_out: hub says no clients on initial dump; stopping"
                    )
                    child.detach_stream()
                    return
            except Exception as e:
                logger.debug("pty_out initial dump failed: %s", e)
        # Drain the queue forever. Block on get() so we react instantly
        # to new bytes — no 16ms polling lag, no missed data when the
        # screen buffer wraps under a TUI redraw.
        Empty = queue.Empty
        consecutive_failures = 0
        first_failure_at = 0.0
        while not stop.is_set():
            # Circuit-breaker check fires every iteration, even when the
            # queue is idle. Without this, an empty queue during an
            # outage would leave the loop spinning on q.get() until the
            # next byte arrives — meanwhile we'd never re-evaluate
            # whether the hub has been dead long enough to give up.
            if (
                consecutive_failures > 0
                and (time.monotonic() - first_failure_at) > _FRAMES_GIVE_UP_AFTER_S
            ):
                logger.warning(
                    "pty_out: hub unresponsive for >%ds (%d failures);"
                    " dropping stream until next pty_attach",
                    _FRAMES_GIVE_UP_AFTER_S,
                    consecutive_failures,
                )
                break
            try:
                data = q.get(timeout=0.5)
            except Empty:
                continue
            if not data:
                # Empty bytes are a "soft close" sentinel. Loop and
                # check stop.is_set() to exit cleanly.
                continue
            # Coalesce any further pending chunks so each POST carries
            # as much as possible — fewer round trips, same latency.
            chunks = [data]
            while True:
                try:
                    chunks.append(q.get_nowait())
                except Empty:
                    break
            payload = b"".join(chunks)
            try:
                resp = httpx.post(
                    url,
                    content=payload,
                    headers={
                        **self._headers,
                        "Content-Type": "application/octet-stream",
                    },
                    timeout=2.0,
                )
                if resp.status_code == 404:
                    logger.debug("pty_out: hub says no clients; stopping stream")
                    break
                if resp.status_code >= 400:
                    logger.warning(
                        "pty_out POST %d: %s",
                        resp.status_code,
                        resp.text[:100],
                    )
                    raise RuntimeError(f"pty_out HTTP {resp.status_code}")
                # Successful delivery — clear failure state.
                if consecutive_failures > 0:
                    logger.info(
                        "pty_out: recovered after %d failures",
                        consecutive_failures,
                    )
                consecutive_failures = 0
                first_failure_at = 0.0
            except Exception as e:
                consecutive_failures += 1
                if first_failure_at == 0.0:
                    first_failure_at = time.monotonic()
                # Top-of-loop check fires on the NEXT iteration if we've
                # passed the give-up window — so we don't need to repeat
                # it here. Still set first_failure_at so it's accurate.
                backoff = _frames_backoff_s(consecutive_failures)
                logger.debug(
                    "pty_out POST failed (#%d): %s — backing off %.1fs",
                    consecutive_failures,
                    e,
                    backoff,
                )
                # Drain queued chunks while we wait so the queue can't
                # grow unbounded during an outage. pump_io already
                # drops oldest on overflow, but draining here turns
                # "lossy ring buffer" into "best-effort delivery."
                _drain_queue(q)
                if stop.wait(backoff):
                    break

        child.detach_stream()
        logger.info("pty_stream: exited")

    def _run_reconnect_mcp(self, child: _PtyChild, server: str) -> None:
        """Drive reconnect_mcp on a background thread, post result to the
        hub."""
        try:
            result = child.reconnect_mcp(server)
        except Exception as e:
            result = {"ok": False, "server": server, "detail": f"exception: {e}"}
        logger.info("reconnect_mcp(%s) → %s", server, result)
        try:
            httpx.post(
                self._url("/outbound"),
                json={
                    "text": json.dumps(result),
                    "meta": {"kind": "reconnect_mcp_result", **result},
                },
                headers=self._headers,
                timeout=5.0,
            )
        except Exception:
            logger.warning("failed to post reconnect_mcp result")

    def _run(self) -> None:
        from httpx_sse import connect_sse

        delay = 0.5
        while not self._stop_event.is_set():
            try:
                # Re-register before each SSE connect — idempotent, ensures
                # the hub knows about us after a hub restart wiped its state.
                try:
                    self._register()
                except Exception:
                    logger.debug("re-register before SSE failed (will retry)")

                with httpx.Client(
                    timeout=httpx.Timeout(10.0, read=None),
                    headers=self._headers,
                ) as client:
                    with connect_sse(
                        client, "GET", self._url("/stream")
                    ) as event_source:
                        delay = 0.5
                        for sse in event_source.iter_sse():
                            if self._stop_event.is_set():
                                return
                            try:
                                data = json.loads(sse.data) if sse.data else {}
                            except json.JSONDecodeError:
                                continue
                            self._handle_event(sse.event or "message", data)
            except Exception as e:
                if self._stop_event.is_set():
                    return
                logger.debug("manager SSE: %s — reconnecting in %.1fs", e, delay)
                self._stop_event.wait(delay)
                delay = min(delay * 2, 10.0)


def run(session_name: str, extra_args: list[str] | None = None) -> int:
    """Run claude in a managed pty restart loop."""
    hub_url = os.environ.get("TUBEMAIL_HUB_URL", "http://localhost:8001")
    secret = os.environ.get("TUBEMAIL_SECRET", "")

    pidfile = _pidfile_path(session_name)
    pidfile.write_text(str(os.getpid()))

    # Idempotently install the Stop hook into the user's
    # ~/.claude/settings.json so every assistant turn relays its reply
    # to tubemail's bridge. Without this, the LLM has to remember to
    # call the channel `reply` tool explicitly, which it forgets for
    # normal chat replies — the response never reaches tm_receive and
    # QM's auto-extract finds nothing. Self-installing on every run
    # means "install tubemail, restart claude-tm, it just works."
    _install_stop_hook()

    # Start manager channel listener
    listener: _ManagerChannelListener | None = None
    if secret:
        manager_name = f"{session_name}-manager"
        listener = _ManagerChannelListener(
            hub_url=hub_url,
            manager_name=manager_name,
            secret=secret,
            session_name=session_name,
        )
        listener.start()
    else:
        logger.warning("TUBEMAIL_SECRET not set — manager channel disabled")

    # Resolve the channel plugin dir (contains .claude-plugin/ and commands/)
    # so Claude Code loads the /restart skill and other plugin commands.
    _channel_dir = str(Path(__file__).resolve().parents[2])

    base_cmd = [
        "claude",
        "--name",
        session_name,
        "--rc",
        session_name,
        "--dangerously-load-development-channels",
        "server:tubemail-channel",
        "--plugin-dir",
        _channel_dir,
    ]
    if extra_args:
        base_cmd.extend(extra_args)

    # Set worker name in our own environment so it's inherited by the pty child,
    # which inherits it to claude, which inherits it to the channel.
    os.environ["TM_WORKER_NAME"] = session_name

    # Start the local IPC server so the channel plugin can drive
    # `/mcp` reconnect even when the tubemail hub itself is the dead
    # MCP. Path is exported via TUBEMAIL_LOCAL_SOCK so the channel
    # inherits it through the pty child's environment; if the bind
    # fails we keep going without local IPC and the channel falls back
    # to the hub-routed reconnect path.
    sock_path = os.environ.get(SOCK_ENV, "").strip() or default_sock_path(session_name)
    _ipc_child_lock = threading.Lock()
    _ipc_child_ref: dict[str, _PtyChild | None] = {"child": None}

    def _set_ipc_child(c: _PtyChild | None) -> None:
        with _ipc_child_lock:
            _ipc_child_ref["child"] = c

    def _handle_local_ipc(req: dict) -> dict:
        action = req.get("action")
        request_id = req.get("request_id", "")
        if action == "reconnect_mcp":
            server = req.get("server", "")
            if not server:
                return {
                    "request_id": request_id,
                    "ok": False,
                    "server": "",
                    "detail": "missing 'server' in request",
                }
            with _ipc_child_lock:
                c = _ipc_child_ref["child"]
            if c is None:
                return {
                    "request_id": request_id,
                    "ok": False,
                    "server": server,
                    "detail": "no active pty child — manager between restarts",
                }
            try:
                result = c.reconnect_mcp(server)
            except Exception as e:
                return {
                    "request_id": request_id,
                    "ok": False,
                    "server": server,
                    "detail": f"exception: {e}",
                }
            return {"request_id": request_id, **result}
        return {
            "request_id": request_id,
            "ok": False,
            "error": f"unknown action: {action!r}",
        }

    ipc_server: LocalIPCServer | None = None
    try:
        ipc_server = LocalIPCServer(sock_path, _handle_local_ipc)
        ipc_server.start()
        os.environ[SOCK_ENV] = sock_path
    except OSError as e:
        logger.warning(
            "local-ipc: failed to bind %s (%s) — channel will fall back to hub",
            sock_path,
            e,
        )
        ipc_server = None
        os.environ.pop(SOCK_ENV, None)

    # Signal handlers — fallback control path when tubemail is down.
    # SIGUSR1 = restart (kill child, loop continues with --continue)
    # SIGTERM/SIGINT = stop (kill child, exit manager)
    _current_child: _PtyChild | None = None
    _signal_restart = False
    _signal_stop = False

    def _on_restart_signal(signum, frame):
        nonlocal _signal_restart
        _signal_restart = True
        if listener:
            listener._restart_requested = True
        if _current_child and _current_child.pid:
            _current_child.terminate()

    def _on_stop_signal(signum, frame):
        nonlocal _signal_stop
        _signal_stop = True
        if listener:
            listener._stop_requested = True
        if _current_child and _current_child.pid:
            _current_child.terminate()

    signal.signal(signal.SIGUSR1, _on_restart_signal)
    signal.signal(signal.SIGTERM, _on_stop_signal)
    signal.signal(signal.SIGINT, _on_stop_signal)

    restart_count = 0
    try:
        while True:
            _signal_restart = False
            if listener:
                listener.clear_flags()

            cmd = list(base_cmd)
            if restart_count > 0 and "--continue" not in cmd and "-c" not in cmd:
                cmd.append("--continue")
                logger.info(
                    "restarting worker '%s' (restart #%d)", session_name, restart_count
                )
                time.sleep(2)
            else:
                logger.info("starting worker '%s'", session_name)

            child = _PtyChild(cmd, session_name=session_name)
            _current_child = child
            child.start()

            if listener:
                listener.set_child(child)
            _set_ipc_child(child)

            exit_code = child.pump_io()
            child.close()
            _current_child = None

            if listener:
                listener.set_child(None)
            _set_ipc_child(None)

            # Restart policy — check both tubemail channel and signal-based flags
            restart_signal = _signal_restart or (
                listener.restart_requested if listener else False
            )
            stop_signal = _signal_stop or (
                listener.stop_requested if listener else False
            )

            if stop_signal:
                logger.info("stop signal received — exiting")
                break
            elif exit_code == 0 and not restart_signal:
                logger.info("worker exited cleanly (code 0) — stopping")
                break
            elif exit_code == 0 and restart_signal:
                logger.info(
                    "worker exited with restart signal — restarting with --continue"
                )
            else:
                logger.info(
                    "worker exited (code %d) — restarting with --continue", exit_code
                )

            restart_count += 1

    finally:
        pidfile.unlink(missing_ok=True)
        if ipc_server is not None:
            ipc_server.stop()
        if listener:
            # "Clean" = the reason we fell out of the loop was "claude
            # exited code 0 without any restart/update/force-stop signal"
            # — i.e. user typed /exit. Everything else (update_manager,
            # force_stop, crash recovery gone wrong) is not clean.
            clean = (
                not listener.update_manager_requested
                and not _signal_restart
                and not _signal_stop
            )
            listener.stop(clean=clean)

    # If the stop signal came from update_manager, return the sentinel so the
    # bash wrapper knows to re-exec python instead of exiting.
    if listener and listener.update_manager_requested:
        return EXIT_UPDATE_MANAGER
    return 0


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "usage: python -m tubemail.manager <session_name> [claude args...]",
            file=sys.stderr,
        )
        sys.exit(1)
    session_name = sys.argv[1]
    extra_args = sys.argv[2:] if len(sys.argv) > 2 else []

    # Log to a per-session file — NOT stderr — because the manager's stderr
    # shares the terminal with the claude pty child, so any warning lands on
    # top of claude's TUI mid-session. Users can `tail -f` the file:
    #   tail -f /tmp/claude-tm-<session>.log
    # Override with TUBEMAIL_LOG_FILE=- to force stderr (useful for tests).
    log_target = os.environ.get("TUBEMAIL_LOG_FILE") or str(_logfile_path(session_name))
    level = os.environ.get("TUBEMAIL_LOG", "WARNING").upper()
    fmt = "%(asctime)s [claude-tm] %(levelname)s %(message)s"
    if log_target == "-":
        logging.basicConfig(stream=sys.stderr, level=level, format=fmt)
    else:
        logging.basicConfig(filename=log_target, level=level, format=fmt)
    # Suppress httpx's per-request INFO logging — same pty-noise concern.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    sys.exit(run(session_name, extra_args))


if __name__ == "__main__":
    main()
