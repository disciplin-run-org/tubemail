"""End-to-end pipeline smoke test.

This is the single most useful test: if it passes, the whole stack is
working as the operator (and the CC MCP client) experience it. We use
exactly the same path a real client would: initialize → tools/list with
a session id, with the bearer that the operator has in .env.

If this passes, heal.py skips the per-component dance.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import test_mcp  # noqa: E402
from _common import header, ok, result_fail, result_pass  # noqa: E402


def test() -> dict:
    header("[e2e] real client path: initialize → tools/list")
    r = test_mcp.test()
    if r["status"] == "pass":
        ok("end-to-end MCP path is healthy")
        return result_pass()
    return result_fail(f"e2e_failed:{r['error']}")


if __name__ == "__main__":
    sys.exit(0 if test()["status"] == "pass" else 1)
