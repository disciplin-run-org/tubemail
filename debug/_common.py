"""Shared helpers for tubemail debug/heal scripts.

Stdlib-only by policy: heal must work even when the project's deps are
broken. No third-party imports.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ── CLI flags ─────────────────────────────────────────────────────────────────
HEAL = "--debugonly" not in sys.argv  # heal by default; --debugonly diagnoses

# ── Repo-relative paths ───────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
COMPOSE_OVERRIDE = REPO_ROOT / "docker-compose.override.yml"
ENV_FILE = REPO_ROOT / ".env"

# ── Service identity ──────────────────────────────────────────────────────────
SERVICE = "tubemail-hub"
CONTAINER = "tubemail-tubemail-hub-1"
PORT = 8004
BASE = f"http://localhost:{PORT}"

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN, YELLOW, RED, CYAN, BOLD, RESET = (
    "\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[1m", "\033[0m"
)


def ok(msg: str) -> None:        print(f"  {GREEN}✓{RESET} {msg}")
def warn(msg: str) -> None:      print(f"  {YELLOW}⚠{RESET} {msg}")
def fail(msg: str) -> None:      print(f"  {RED}✗{RESET} {msg}")
def header(msg: str) -> None:    print(f"\n{BOLD}{msg}{RESET}")
def healing(msg: str) -> None:   print(f"  {CYAN}⚕{RESET} {BOLD}HEAL:{RESET} {msg}")


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def http(method: str, url: str, *, headers: dict | None = None,
         body: bytes | None = None, timeout: float = 5.0) -> tuple[int | None, dict, str]:
    """Return (status, response_headers, body_text). status is None on transport failure."""
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode(errors="replace")
    except Exception as e:
        return None, {}, f"transport_error: {e}"


def get(url: str, **kw) -> tuple[int | None, str]:
    s, _, b = http("GET", url, **kw)
    return s, b


def post_json(url: str, payload: dict, *, bearer: str | None = None,
              timeout: float = 10.0) -> tuple[int | None, dict, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json,text/event-stream",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return http("POST", url, headers=headers,
                body=json.dumps(payload).encode(), timeout=timeout)


# ── Docker / compose helpers ──────────────────────────────────────────────────
def compose_cmd() -> list[str]:
    """Base docker compose command — both files so override (dev) is honored."""
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    if COMPOSE_OVERRIDE.exists():
        cmd += ["-f", str(COMPOSE_OVERRIDE)]
    cmd += ["--project-directory", str(REPO_ROOT)]
    return cmd


def compose_up(force_recreate: bool = False) -> int:
    healing(f"compose up -d {SERVICE}{' --force-recreate' if force_recreate else ''}")
    cmd = [*compose_cmd(), "up", "-d"]
    if force_recreate:
        cmd.append("--force-recreate")
    cmd.append(SERVICE)
    return subprocess.run(cmd, capture_output=True).returncode


def compose_restart(wait: float = 3.0) -> int:
    healing(f"compose restart {SERVICE}")
    rc = subprocess.run([*compose_cmd(), "restart", SERVICE],
                        capture_output=True).returncode
    time.sleep(wait)
    return rc


def compose_build() -> int:
    healing(f"compose build {SERVICE} (no cache for image layer drift)")
    return subprocess.run([*compose_cmd(), "build", SERVICE],
                          capture_output=True).returncode


def docker_ps_json() -> list[dict]:
    r = subprocess.run(["docker", "ps", "-a", "--format", "{{json .}}"],
                       capture_output=True, text=True)
    out: list[dict] = []
    for line in r.stdout.strip().split("\n"):
        if line.strip():
            out.append(json.loads(line))
    return out


def container_state(name: str = CONTAINER) -> tuple[bool, bool, str]:
    """Return (running, healthy, status_string).
    healthy is False if no healthcheck or status is unhealthy/starting.
    """
    for c in docker_ps_json():
        if c.get("Names", "") == name:
            status = c.get("Status", "")
            running = status.startswith("Up")
            healthy = "(healthy)" in status
            return running, healthy, status
    return False, False, "not_found"


def container_env(name: str = CONTAINER) -> dict[str, str]:
    """Return the env vars currently visible inside the running container."""
    r = subprocess.run(
        ["docker", "exec", name, "env"],
        capture_output=True, text=True
    )
    out: dict[str, str] = {}
    if r.returncode != 0:
        return out
    for line in r.stdout.split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def tail_logs(lines: int = 30) -> str:
    r = subprocess.run([*compose_cmd(), "logs", "--tail", str(lines), SERVICE],
                       capture_output=True, text=True)
    return r.stdout + r.stderr


# ── .env parsing ──────────────────────────────────────────────────────────────
def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE per line, ignoring comments + blanks.
    Does NOT expand variables; matches docker-compose's behavior closely
    enough for our drift checks.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        # Strip wrapping quotes if present
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or \
           (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k.strip()] = v
    return out


# ── Result helpers ────────────────────────────────────────────────────────────
def result_pass(msg: str = "") -> dict[str, Any]:
    return {"status": "pass", "error": None, "msg": msg}


def result_fail(error: str, healed: bool = False) -> dict[str, Any]:
    return {"status": "fail", "error": error, "healed": healed}


def wait_until(predicate, *, timeout: float = 30.0, interval: float = 1.0) -> bool:
    """Poll `predicate` until it returns truthy or timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
