#!/usr/bin/env python3
"""Bring the TubeMail hub up and verify it's healthy.

Steps, in order:
  1. Ensure .env exists (bootstrap with a fresh TUBEMAIL_SECRET if missing).
  2. Ensure the hub image is built (docker compose build if not).
  3. Ensure the hub container is running (docker compose up -d if not).
  4. Poll :8001/health until it responds (or fail with diagnostics).

Exit codes:
  0 — hub is healthy on :8001.
  1 — heal failed; diagnostic output printed above.

Run from the tubemail repo root:
    python scripts/heal.py
"""

from __future__ import annotations

import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"
COMPOSE_FILE = REPO / "docker-compose.yml"
SERVICE = "tubemail"
HEALTH_URL = "http://localhost:8001/health"
HEALTH_TIMEOUT_S = 30
HEALTH_INTERVAL_S = 1


def log(msg: str) -> None:
    print(f"[heal] {msg}", flush=True)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO, check=check, capture_output=True, text=True)


def ensure_env() -> None:
    if ENV_FILE.exists():
        content = ENV_FILE.read_text()
        if "TUBEMAIL_SECRET=" in content:
            log(f".env present with TUBEMAIL_SECRET at {ENV_FILE}")
            return
        log(f".env exists but TUBEMAIL_SECRET missing — appending")
        secret = secrets.token_hex(32)
        with ENV_FILE.open("a") as f:
            if not content.endswith("\n"):
                f.write("\n")
            f.write(f"TUBEMAIL_SECRET={secret}\n")
        return
    secret = secrets.token_hex(32)
    ENV_FILE.write_text(f"TUBEMAIL_SECRET={secret}\n")
    log(f"created {ENV_FILE} with fresh TUBEMAIL_SECRET")


def image_exists() -> bool:
    # docker compose prefixes images with the project name (dir name).
    # Rather than guessing the name, ask compose itself.
    result = run(["docker", "compose", "images", "-q", SERVICE], check=False)
    return bool(result.stdout.strip())


def container_running() -> bool:
    result = run(
        ["docker", "compose", "ps", "--status", "running", "-q", SERVICE],
        check=False,
    )
    return bool(result.stdout.strip())


def build_image() -> None:
    log(f"building image for {SERVICE}")
    result = subprocess.run(
        ["docker", "compose", "build", SERVICE],
        cwd=REPO,
    )
    if result.returncode != 0:
        fail(f"docker compose build exited {result.returncode}")


def start_container() -> None:
    log(f"starting {SERVICE}")
    result = subprocess.run(
        ["docker", "compose", "up", "-d", SERVICE],
        cwd=REPO,
    )
    if result.returncode != 0:
        fail(f"docker compose up exited {result.returncode}")


def poll_health() -> bool:
    deadline = time.time() + HEALTH_TIMEOUT_S
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
                if resp.status == 200:
                    log(f"health OK on attempt {attempt} ({HEALTH_URL})")
                    return True
                log(f"attempt {attempt}: HTTP {resp.status}")
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            log(f"attempt {attempt}: {e}")
        time.sleep(HEALTH_INTERVAL_S)
    return False


def dump_diagnostics() -> None:
    log("--- docker compose ps ---")
    subprocess.run(["docker", "compose", "ps"], cwd=REPO)
    log(f"--- last 40 lines of logs for {SERVICE} ---")
    subprocess.run(
        ["docker", "compose", "logs", "--tail", "40", SERVICE],
        cwd=REPO,
    )


def fail(msg: str) -> None:
    log(f"FAILED: {msg}")
    dump_diagnostics()
    sys.exit(1)


def main() -> None:
    if not COMPOSE_FILE.exists():
        fail(f"no docker-compose.yml at {COMPOSE_FILE}")

    ensure_env()

    if not image_exists():
        build_image()
    else:
        log("image already built")

    if not container_running():
        start_container()
    else:
        log(f"{SERVICE} already running")

    if not poll_health():
        fail(f"{HEALTH_URL} did not respond within {HEALTH_TIMEOUT_S}s")

    log(f"hub healthy at {HEALTH_URL}")


if __name__ == "__main__":
    main()
