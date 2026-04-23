"""claude-tm process manager.

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

import errno
import fcntl
import json
import logging
import os
import pty
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

import httpx
import psutil

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


def _normalize_pty_tail(tail: bytes) -> bytes:
    """Strip ANSI escapes and whitespace from a pty buffer slice so text
    patterns match regardless of line wrapping or terminal formatting."""
    return (
        _ANSI_RE.sub(b"", tail)
        .replace(b" ", b"")
        .replace(b"\n", b"")
        .replace(b"\r", b"")
    )


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


# Exit code meanings for the bash restart-loop in scripts/claude-tm:
#   0 = clean exit (user typed /exit) — bash exits too
#   EXIT_UPDATE_WRAPPER (42) = re-exec the python manager to pick up updated
#       tubemail source. bash adds --continue so claude resumes the same
#       conversation. Session name + role are unchanged.
#   anything else = crash; bash restarts up to a bounded count.
EXIT_UPDATE_WRAPPER = 42


# /mcp dialog helpers (pure functions for testability) ─────────────────────

def _extract_mcp_server_list(screen: str) -> list[str]:
    """Parse an /mcp dialog screen and return the server names in list order.

    A server row looks like "name · status" where status contains one of
    connect / fail / auth / disabled (case-insensitive) or the ✔/✘ glyphs.
    Section headers ("Project MCPs", "User MCPs", "claude.ai") are skipped
    because they lack a connection-style status.
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

    def _get_screen_text(self) -> str:
        """Thread-safe ANSI-stripped snapshot of the current screen buffer."""
        with self._screen_lock:
            raw = bytes(self._screen_buf)
        return _ANSI_RE.sub(b"", raw).decode("utf-8", errors="replace")

    def _wait_for_screen(
        self, predicate, timeout_s: float, poll_s: float = 0.15
    ) -> bool:
        """Poll the screen buffer until predicate(text) is truthy or timeout."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate(self._get_screen_text()):
                return True
            time.sleep(poll_s)
        return False

    def reconnect_mcp(self, server_name: str) -> dict:
        """Deterministically drive /mcp → select server → Reconnect.

        Returns {"ok": bool, "server": str, "detail": str}.
        Meant to be called from a background thread — writes to the pty
        and polls the screen buffer with short sleeps between steps.
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
                "ok": False, "server": server_name,
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
                "ok": False, "server": server_name,
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
                "ok": False, "server": server_name,
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
            lambda s: server_name in s and (
                "failed" in s.lower() or "error" in s.lower()
            ),
            timeout_s=2.0,
        ):
            return {
                "ok": False, "server": server_name,
                "detail": "reconnect finished with failure marker on screen",
            }

        return {
            "ok": False, "server": server_name,
            "detail": "no confirmation within 20s — check manually",
        }

    def pump_io(self) -> int:
        """Forward I/O between the user's terminal and the child's pty.

        Blocks until the child exits. Returns the child's exit code.

        Auto-accepts the development channels warning prompt by watching for
        the "I am using this for local development" text in the pty output
        and sending Enter (\\r) automatically.
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

                        # Feed rolling screen buffer for screenshots
                        with self._screen_lock:
                            self._screen_buf.extend(data)
                            if len(self._screen_buf) > self._screen_buf_max:
                                self._screen_buf = self._screen_buf[-self._screen_buf_max:]

                        # Auto-accept: check only the recent screen tail.
                        # Timing matters: we delay 0.3s after each auto-accept
                        # to let the child consume the response before we send
                        # another. Without this, a \r for dev-channels can bleed
                        # into the compact prompt that appears immediately after.
                        if len(_accepted_groups) < len({g for _, _, _, g in _AUTO_ACCEPT_PATTERNS}):
                            with self._screen_lock:
                                tail = bytes(self._screen_buf[-_SCREEN_CHECK_SIZE:])
                            # Strip ANSI escapes AND whitespace so patterns
                            # like b"compactyourconversation" match regardless
                            # of spacing, line breaks, or formatting.
                            clean = _ANSI_RE.sub(b"", tail).replace(b" ", b"").replace(b"\n", b"").replace(b"\r", b"")
                            for pattern, response, label, group in _AUTO_ACCEPT_PATTERNS:
                                if group in _accepted_groups:
                                    continue
                                if pattern in clean:
                                    # Delay to let the child consume any prior
                                    # buffered keystrokes before we inject ours.
                                    time.sleep(0.3)
                                    os.write(master, response)
                                    _accepted_groups.add(group)
                                    logger.info("auto-accepted: %s (group=%s)", label, group)
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
                                    delay_s, self._rl_retry_count,
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
                _RATE_LIMIT_RESET_AFTER_S, self._rl_retry_count,
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
        self._update_wrapper_requested = False

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def update_wrapper_requested(self) -> bool:
        return self._update_wrapper_requested

    def clear_flags(self) -> None:
        self._restart_requested = False
        self._stop_requested = False
        self._update_wrapper_requested = False

    def set_child(self, child: _PtyChild | None) -> None:
        self._child = child
        self._child_start_time = time.time() if child else 0.0

    def start(self) -> None:
        self._register()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, clean: bool = False) -> None:
        """Stop the listener. Set clean=True when the session is ending
        because the user /exit'd (not on crash/update). Posts /goodbye
        instead of /unregister so the hub records a clean exit."""
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
                    self._name, resp.status_code, resp.text[:200],
                )
            else:
                logger.info(
                    "manager registered as %s (v%s)", self._name, _fwd_version,
                )
        except Exception as e:
            logger.warning(
                "manager registration failed (%s): %s: %s",
                self._name, type(e).__name__, e,
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
        crashes/hangs/kills where no POST happens."""
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

        elif kind == "update_wrapper":
            logger.info("manager: update_wrapper — exiting with %d to trigger re-exec", EXIT_UPDATE_WRAPPER)
            self._update_wrapper_requested = True
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

    def _run_reconnect_mcp(self, child: "_PtyChild", server: str) -> None:
        """Drive reconnect_mcp on a background thread, post result to the hub."""
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
                    with connect_sse(client, "GET", self._url("/stream")) as event_source:
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
    hub_url = os.environ.get("TUBEMAIL_HUB_URL", "http://localhost:8004")
    secret = os.environ.get("TUBEMAIL_SECRET", "")

    pidfile = _pidfile_path(session_name)
    pidfile.write_text(str(os.getpid()))

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

    # Resolve the forwarder plugin dir (contains .claude-plugin/ and commands/)
    # so Claude Code loads the /restart skill and other plugin commands.
    _forwarder_dir = str(Path(__file__).resolve().parents[2])

    base_cmd = [
        "claude",
        "--name", session_name,
        "--rc", session_name,
        "--dangerously-load-development-channels", "server:tubemail",
        "--plugin-dir", _forwarder_dir,
    ]
    if extra_args:
        base_cmd.extend(extra_args)

    # Set worker name in our own environment so it's inherited by the pty child,
    # which inherits it to claude, which inherits it to the forwarder.
    os.environ["TM_WORKER_NAME"] = session_name

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
                logger.info("restarting worker '%s' (restart #%d)", session_name, restart_count)
                time.sleep(2)
            else:
                logger.info("starting worker '%s'", session_name)

            child = _PtyChild(cmd, session_name=session_name)
            _current_child = child
            child.start()

            if listener:
                listener.set_child(child)

            exit_code = child.pump_io()
            child.close()
            _current_child = None

            if listener:
                listener.set_child(None)

            # Restart policy — check both tubemail channel and signal-based flags
            restart_signal = _signal_restart or (listener.restart_requested if listener else False)
            stop_signal = _signal_stop or (listener.stop_requested if listener else False)

            if stop_signal:
                logger.info("stop signal received — exiting")
                break
            elif exit_code == 0 and not restart_signal:
                logger.info("worker exited cleanly (code 0) — stopping")
                break
            elif exit_code == 0 and restart_signal:
                logger.info("worker exited with restart signal — restarting with --continue")
            else:
                logger.info("worker exited (code %d) — restarting with --continue", exit_code)

            restart_count += 1

    finally:
        pidfile.unlink(missing_ok=True)
        if listener:
            # "Clean" = the reason we fell out of the loop was "claude
            # exited code 0 without any restart/update/force-stop signal"
            # — i.e. user typed /exit. Everything else (update_wrapper,
            # force_stop, crash recovery gone wrong) is not clean.
            clean = (
                not listener.update_wrapper_requested
                and not _signal_restart
                and not _signal_stop
            )
            listener.stop(clean=clean)

    # If the stop signal came from update_wrapper, return the sentinel so the
    # bash wrapper knows to re-exec python instead of exiting.
    if listener and listener.update_wrapper_requested:
        return EXIT_UPDATE_WRAPPER
    return 0


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m tubemail.manager <session_name> [claude args...]", file=sys.stderr)
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
