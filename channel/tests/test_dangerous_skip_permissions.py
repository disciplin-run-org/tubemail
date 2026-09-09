"""Tests for the opt-in `--dangerously-skip-permissions` launch flag.

Claude Code's permission prompts stop autonomous workers mid-run. On a machine
whose permission floor is enforced deterministically elsewhere (jjstack's
PreToolUse deny hook, ADR AR-4), running the worker with
`--dangerously-skip-permissions` is the intended posture. On a stock install
from the public repo it is emphatically NOT — the flag bypasses every
permission check.

So the flag is opt-in via `TM_DANGEROUSLY_SKIP_PERMISSIONS` and these tests
pin the properties that keep it safe:

  1. Default (env unset or empty) → flag absent. This is the property that
     protects a fresh public-repo install; if it ever regresses, every new
     user silently loses their permission prompts.
  2. Common truthy spellings turn it on; everything else leaves it off.
     Anything unrecognised must fail SAFE (off), never on.
  3. The channel wiring args are never disturbed, and caller passthrough
     args still land last.
  4. A caller who already passed the flag explicitly does not get it twice.
"""

from __future__ import annotations

import pytest

from tubemail.manager import _build_base_cmd

SESSION = "sacrificial-tm"
CHANNEL_DIR = "/opt/tubemail/channel"
FLAG = "--dangerously-skip-permissions"


def _build(env, extra_args=None):
    return _build_base_cmd(
        session_name=SESSION,
        channel_dir=CHANNEL_DIR,
        extra_args=extra_args or [],
        env=env,
    )


# ─────────────────────────────────────────────────────────────────────────
# Default-off: the property that protects public-repo installs
# ─────────────────────────────────────────────────────────────────────────


def test_flag_absent_when_env_unset():
    """A stock install has no opt-in var and must keep its permission prompts."""
    assert FLAG not in _build({})


@pytest.mark.parametrize("value", ["", "   ", "0", "false", "no", "off", "maybe", "2"])
def test_flag_absent_for_empty_falsy_and_unrecognised_values(value):
    """Unrecognised values fail SAFE — a typo must never bypass permissions."""
    assert FLAG not in _build({"TM_DANGEROUSLY_SKIP_PERMISSIONS": value})


# ─────────────────────────────────────────────────────────────────────────
# Opt-in
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", " 1 "])
def test_flag_present_for_truthy_values(value):
    assert FLAG in _build({"TM_DANGEROUSLY_SKIP_PERMISSIONS": value})


def test_flag_not_duplicated_when_caller_already_passed_it():
    cmd = _build({"TM_DANGEROUSLY_SKIP_PERMISSIONS": "1"}, extra_args=[FLAG])
    assert cmd.count(FLAG) == 1


# ─────────────────────────────────────────────────────────────────────────
# The rest of the command line is untouched
# ─────────────────────────────────────────────────────────────────────────


def test_channel_wiring_args_always_present():
    for env in ({}, {"TM_DANGEROUSLY_SKIP_PERMISSIONS": "1"}):
        cmd = _build(env)
        assert cmd[0] == "claude"
        assert cmd[cmd.index("--name") + 1] == SESSION
        assert cmd[cmd.index("--rc") + 1] == SESSION
        assert (
            cmd[cmd.index("--dangerously-load-development-channels") + 1]
            == "server:tubemail-channel"
        )
        assert cmd[cmd.index("--plugin-dir") + 1] == CHANNEL_DIR


def test_passthrough_args_land_last():
    cmd = _build({"TM_DANGEROUSLY_SKIP_PERMISSIONS": "1"}, extra_args=["--continue"])
    assert cmd[-1] == "--continue"
