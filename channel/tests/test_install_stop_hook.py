"""Tests for the Stop hook installer.

Idempotent installer that adds a `Stop` entry to the user's
~/.claude/settings.json. Adding twice produces the same final state.
Skips if settings.json doesn't exist (don't dictate the user's
permissions config).
"""

from __future__ import annotations

# Standard Libraries
import importlib.util
import json
import sys
from pathlib import Path

# 3rd party
import pytest


INSTALLER_PATH = (
    Path(__file__).resolve().parents[1] / "hooks" / "install_stop_hook.py"
)


@pytest.fixture
def installer_module(monkeypatch, tmp_path: Path):
    """Import the script as a module with SETTINGS_PATH redirected to tmp."""
    spec = importlib.util.spec_from_file_location(
        "install_stop_hook", INSTALLER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(mod, "SETTINGS_PATH", settings)
    return mod, settings


def test_skip_when_settings_missing(installer_module):
    mod, settings = installer_module
    assert not settings.exists()
    rc = mod.install()
    assert rc == 0
    # Did NOT create settings.json — leaves the user's setup alone.
    assert not settings.exists()


def test_adds_stop_hook_to_existing_settings(installer_module):
    mod, settings = installer_module
    settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}}))

    rc = mod.install()
    assert rc == 0

    out = json.loads(settings.read_text())
    # Existing keys preserved.
    assert out["permissions"] == {"allow": ["Read"]}
    # Stop hook added.
    assert "Stop" in out["hooks"]
    stop_entries = out["hooks"]["Stop"]
    assert len(stop_entries) == 1
    cmd = stop_entries[0]["hooks"][0]["command"]
    assert "post_stop_relay.py" in cmd


def test_idempotent_running_twice_no_duplicate(installer_module):
    mod, settings = installer_module
    settings.write_text("{}")

    assert mod.install() == 0
    first = json.loads(settings.read_text())
    assert mod.install() == 0
    second = json.loads(settings.read_text())

    # Same state both times.
    assert first == second
    assert len(second["hooks"]["Stop"]) == 1


def test_appends_alongside_existing_stop_hooks(installer_module):
    mod, settings = installer_module
    # User already has their own Stop hook (e.g., a custom logger).
    existing = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": "/usr/local/bin/my-logger"}
                    ],
                }
            ]
        }
    }
    settings.write_text(json.dumps(existing))

    assert mod.install() == 0
    out = json.loads(settings.read_text())
    stop_entries = out["hooks"]["Stop"]
    # Existing hook preserved + ours appended.
    assert len(stop_entries) == 2
    commands = [
        e["hooks"][0]["command"]
        for e in stop_entries
        if e.get("hooks")
    ]
    assert "/usr/local/bin/my-logger" in commands
    assert any("post_stop_relay.py" in c for c in commands)


def test_returns_1_on_unparseable_settings(installer_module):
    mod, settings = installer_module
    settings.write_text("not json {{{")
    assert mod.install() == 1


def test_returns_1_when_settings_root_is_not_object(installer_module):
    mod, settings = installer_module
    settings.write_text("[]")  # array, not object
    assert mod.install() == 1
