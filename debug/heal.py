"""Master heal orchestrator for tubemail.

Strategy (per the skill template):
1. Run the end-to-end test first. If it passes, we're done — no need to
   poke individual components.
2. On e2e failure, walk the pipeline in order (cheapest test first) and
   call .heal() on the first failing layer. Each layer's heal returns
   structured pass/fail; the next layer in the pipeline only runs if
   the previous one is healthy.
3. After any successful heal, re-run e2e to verify.

Pipeline order matters: a downstream test cannot pass if an upstream
layer is broken (no point checking MCP if the container is down).

Usage:
    python debug/heal.py              # diagnose + heal (default)
    python debug/heal.py --debugonly  # diagnose only, no fixes

Issues in scope (handled by some layer):
- Hub container stopped, unhealthy, or on stale image
- /health failing (process up but listener gone)
- Env drift between .env and container env
- TUBEMAIL_ALLOWED_ORIGINS plumbing regression in docker-compose.yml
- Missing TUBEMAIL_SECRET in .env (surfaced; can't auto-fix)
- /mcp/ 401 bearer mismatch
- /mcp/ initialize/tools/list 5xx or transport failure
- Stale Mcp-Session-Id after a hub recreate (this session's symptom)

Out of scope (a different repo or a client-side bug):
- CC's /mcp dialog refusing to reconnect after this script makes the
  hub healthy → user runs /mcp Reconnect (or Authenticate) themselves.
- Forwarder workers' connection state — they live in another process.
- Other ai-agents services (leanspecs, iris-qa, quartermaster) — each
  gets its own debug/ framework.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import (  # noqa: E402
    BOLD, GREEN, HEAL, RED, RESET,
    fail, header, ok,
)
import test_container  # noqa: E402
import test_e2e        # noqa: E402
import test_hub        # noqa: E402
import test_mcp        # noqa: E402


# Pipeline order: cheapest + most upstream first.
PIPELINE = [
    ("container", test_container),
    ("hub",       test_hub),
    ("mcp",       test_mcp),
]


def _walk_pipeline_and_heal() -> dict:
    """Test each layer in order; on first failure, heal it and stop.
    Skip downstream layers — they'll be re-tested by the e2e re-run."""
    for name, mod in PIPELINE:
        r = mod.test()
        if r["status"] == "pass":
            continue
        header(f"Layer '{name}' failed: {r['error']} — attempting heal")
        return mod.heal()
    return {"status": "pass", "error": None}


def main() -> int:
    header("Tubemail Heal Framework")
    print(f"  Mode: {'HEAL' if HEAL else 'DEBUG ONLY'}")
    print(f"  Repo: {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")

    # Step 1 — e2e
    e2e = test_e2e.test()
    if e2e["status"] == "pass":
        print(f"\n{GREEN}{BOLD}All systems operational.{RESET}")
        return 0

    fail(f"e2e failed: {e2e['error']}")

    # Step 2 — walk + heal
    heal_result = _walk_pipeline_and_heal()
    if heal_result["status"] != "pass":
        print(f"\n{RED}{BOLD}Could not heal.{RESET} Manual intervention required.")
        return 1

    # Step 3 — verify
    header("Post-heal verification")
    verify = test_e2e.test()
    if verify["status"] == "pass":
        print(f"\n{GREEN}{BOLD}All systems operational after heal.{RESET}")
        print("  If CC's /mcp client still shows 'Failed to reconnect',")
        print("  open the /mcp menu and pick Reconnect (or Authenticate")
        print("  if the menu shows '✘ not authenticated').")
        return 0

    fail(f"e2e still failing after heal: {verify['error']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
