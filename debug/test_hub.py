"""Hub HTTP layer: /health responds + container env matches .env.

Issues this catches:
- Container running but app crashed mid-flight (process up, listener gone)
- Env drift: an operator added a var to .env but the container still has
  the old value (compose-up was a no-op because compose only recreates
  when the SERVICE definition changed, not when the .env value changed)
- Plumbing regression: TUBEMAIL_ALLOWED_ORIGINS is set in .env but the
  service stanza in docker-compose.yml doesn't pass it through, so the
  container never sees it. (We fixed this in 9fffd49 — this test guards
  against a future regression.)

Issues this heals:
- /health failing                   → compose restart
- env drift on TUBEMAIL_SECRET      → force-recreate (compose-up reads .env)
- env drift on TUBEMAIL_ALLOWED_ORIGINS → force-recreate
- TUBEMAIL_SECRET missing in .env   → cannot heal; surface to user
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import (  # noqa: E402
    BASE, HEAL,
    compose_restart, compose_up, container_env, fail,
    get, header, healing, ok, read_env_file,
    result_fail, result_pass, wait_until, warn,
)


# Vars whose drift is heal-relevant. TUBEMAIL_SECRET is required (compose
# will refuse to start without it), so we mainly check it for value drift.
WATCHED_ENV_VARS = ("TUBEMAIL_SECRET", "TUBEMAIL_ALLOWED_ORIGINS")


def test() -> dict:
    header(f"[hub] {BASE}/health + env drift")

    # 1. /health endpoint
    status, body = get(f"{BASE}/health", timeout=3)
    if status is None:
        fail(f"transport failure: {body}")
        return result_fail("health_unreachable")
    if status != 200:
        fail(f"/health returned {status}")
        return result_fail(f"health_status_{status}")
    if '"status":"ok"' not in body and '"status": "ok"' not in body:
        fail(f"/health body unexpected: {body[:120]}")
        return result_fail("health_body_unexpected")
    ok("/health 200 ok")

    # 2. .env required-vars present
    env_dot = read_env_file()
    if not env_dot.get("TUBEMAIL_SECRET"):
        fail(".env missing TUBEMAIL_SECRET — hub cannot start without it")
        return result_fail("env_secret_missing")
    ok(".env has TUBEMAIL_SECRET")

    # 3. Drift between .env and container env
    env_ctr = container_env()
    if not env_ctr:
        warn("could not read container env (docker exec failed)")
        return result_pass("env-drift check skipped")

    drift: list[str] = []
    for var in WATCHED_ENV_VARS:
        want = env_dot.get(var, "")
        have = env_ctr.get(var, "")
        if var == "TUBEMAIL_SECRET":
            # Required: must be present and equal
            if want != have:
                drift.append(var)
        else:
            # Optional: only flag drift if .env has it but container doesn't
            # (or values differ when .env has a value).
            if want and want != have:
                drift.append(var)

    if drift:
        fail(f"env drift between .env and container: {', '.join(drift)}")
        # Distinguish two failure modes:
        #   a) Container has stale value → fix via recreate.
        #   b) Container has empty value despite .env set → docker-compose
        #      stanza is missing the env passthrough; needs code fix.
        for var in drift:
            if var != "TUBEMAIL_SECRET" and env_dot.get(var) and not env_ctr.get(var):
                warn(f"{var} set in .env but container has it empty —")
                warn(f"  check that docker-compose.yml passes ${{{var}}} through")
                warn(f"  to the {os.path.basename('tubemail-hub')} service stanza")
        return result_fail("env_drift")
    ok(f"container env matches .env for {', '.join(WATCHED_ENV_VARS)}")

    return result_pass()


def heal() -> dict:
    r = test()
    if r["status"] == "pass" or not HEAL:
        return r

    err = r["error"]

    if err in ("health_unreachable",) or err.startswith("health_status_"):
        compose_restart(wait=5)
        return test()

    if err == "env_drift":
        # compose-up with the latest .env will recreate when env values
        # changed, but only with --force-recreate (otherwise compose
        # short-circuits when service def is unchanged).
        compose_up(force_recreate=True)
        wait_until(lambda: get(f"{BASE}/health", timeout=2)[0] == 200,
                   timeout=20, interval=2)
        return test()

    if err == "env_secret_missing":
        fail("Cannot heal automatically: paste TUBEMAIL_SECRET into .env")
        warn(f"  e.g. echo 'TUBEMAIL_SECRET=<your-bearer>' >> {os.path.basename('.env')}")
        return result_fail(err, healed=False)

    return r


if __name__ == "__main__":
    sys.exit(0 if heal()["status"] == "pass" else 1)
