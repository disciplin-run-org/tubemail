"""Container layer: tubemail-hub is running and healthy.

Issues this catches:
- Container stopped (compose down, OOM, manual rm)
- Image rebuilt but container not recreated → stale code running
- Healthcheck failing (app crashed, port not bound, etc.)

Issues this heals:
- Not running        → compose up -d
- Unhealthy          → compose restart, then force-recreate, then rebuild
- compose up failure → reports stderr; user must intervene (likely .env
  missing TUBEMAIL_SECRET — compose refuses to start without it)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import (  # noqa: E402
    CONTAINER, HEAL,
    compose_build, compose_restart, compose_up,
    container_state, fail, header, healing, ok,
    result_fail, result_pass, tail_logs, wait_until, warn,
)


def test() -> dict:
    header(f"[container] {CONTAINER}")
    running, healthy, status = container_state()

    if not running:
        fail(f"container not running ({status})")
        return result_fail("not_running")
    ok(f"container running ({status})")

    if not healthy and "(healthy)" not in status and "(starting)" not in status:
        fail(f"container unhealthy ({status})")
        return result_fail("unhealthy")

    if "(starting)" in status:
        warn("healthcheck still starting — waiting up to 30s")
        if not wait_until(lambda: container_state()[1], timeout=30, interval=2):
            return result_fail("healthcheck_never_passed")
        ok("healthcheck passed")

    return result_pass()


def heal() -> dict:
    r = test()
    if r["status"] == "pass" or not HEAL:
        return r

    err = r["error"]

    # 1. Not running → compose up
    if err == "not_running":
        rc = compose_up()
        if rc != 0:
            fail("compose up failed — likely missing TUBEMAIL_SECRET in .env")
            warn("logs (last 20 lines):")
            print(tail_logs(20))
            return result_fail("compose_up_failed", healed=False)
        # Wait for healthy
        if not wait_until(lambda: container_state()[1], timeout=30, interval=2):
            return _escalate_unhealthy()
        ok("container started + healthy")
        return result_pass()

    # 2. Unhealthy → restart, then force-recreate, then rebuild
    if err in ("unhealthy", "healthcheck_never_passed"):
        return _escalate_unhealthy()

    return r


def _escalate_unhealthy() -> dict:
    """Three-step escalation: restart → recreate → rebuild."""
    healing("step 1/3: restart")
    compose_restart(wait=5)
    if container_state()[1]:
        ok("healthy after restart")
        return result_pass()

    healing("step 2/3: force-recreate")
    compose_up(force_recreate=True)
    if not wait_until(lambda: container_state()[1], timeout=30, interval=2):
        warn("still unhealthy after recreate")
    else:
        ok("healthy after force-recreate")
        return result_pass()

    healing("step 3/3: rebuild image + recreate")
    if compose_build() != 0:
        fail("build failed — manual intervention required")
        return result_fail("build_failed", healed=False)
    compose_up(force_recreate=True)
    if not wait_until(lambda: container_state()[1], timeout=60, interval=3):
        fail("still unhealthy after rebuild — check logs")
        print(tail_logs(40))
        return result_fail("unhealthy_after_rebuild", healed=False)
    ok("healthy after rebuild")
    return result_pass()


if __name__ == "__main__":
    sys.exit(0 if heal()["status"] == "pass" else 1)
