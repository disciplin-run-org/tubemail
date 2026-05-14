"""Tests for /mcp dialog screen parsing used by reconnect_mcp."""

from __future__ import annotations

from tubemail.manager import (
    _extract_mcp_server_list,
    _server_dialog_position,
    EXIT_UPDATE_MANAGER,
)


# Realistic sample captured from the leanspecs-spec-tm session on 2026-04-22.
# Mixed sections, a failed server, and headers that must NOT be picked up.
SAMPLE_MCP_DIALOG = """Manage MCP servers
11 servers

Project MCPs (~/projects/disciplin-run/leanspecs/.mcp.json)
❯ google-workspace · ✔ connected
iris-qa · ✔ connected
leanspecs · ✘ failed
tubemail · ✔ connected
tubemail · ✔ connected

Local MCPs (~/.claude.json [project: ~/projects/disciplin-run/leanspecs])
mock-refresh · ✘ failed

User MCPs (~/.claude.json)
context7 · ✔ connected

claude.ai
claude.ai Google Drive · ✔ connected
claude.ai HealthEx · ✔ connected

Built-in MCPs (always available)
claude-in-chrome · ✔ connected
plugin:github:github · ✔ connected

↑↓ to navigate · Enter to confirm · Esc to cancel
"""


class TestExtractServerList:
    def test_all_servers_in_listed_order(self):
        servers = _extract_mcp_server_list(SAMPLE_MCP_DIALOG)
        assert servers == [
            "google-workspace",
            "iris-qa",
            "leanspecs",
            "tubemail",
            "tubemail",
            "mock-refresh",
            "context7",
            "claude.ai Google Drive",
            "claude.ai HealthEx",
            "claude-in-chrome",
            "plugin:github:github",
        ]

    def test_section_headers_excluded(self):
        servers = _extract_mcp_server_list(SAMPLE_MCP_DIALOG)
        # "Project MCPs", "User MCPs", "claude.ai" (the bare header) must NOT
        # appear as server entries.
        assert "Project MCPs" not in servers
        assert "User MCPs" not in servers
        assert "Built-in MCPs" not in servers
        assert "claude.ai" not in servers  # bare header, not the google-drive entry

    def test_cursor_marker_stripped(self):
        # The ❯ cursor sits on google-workspace; parsed name must not contain it.
        servers = _extract_mcp_server_list(SAMPLE_MCP_DIALOG)
        assert "google-workspace" in servers
        assert "❯ google-workspace" not in servers

    def test_empty_screen_returns_empty_list(self):
        assert _extract_mcp_server_list("") == []

    def test_footer_help_line_excluded(self):
        # "↑↓ to navigate · Enter to confirm · Esc to cancel" has · but no
        # connect/fail/auth/disabled keyword → excluded.
        servers = _extract_mcp_server_list(SAMPLE_MCP_DIALOG)
        assert "↑↓ to navigate" not in servers


class TestServerDialogPosition:
    def test_first_server_is_position_zero(self):
        assert _server_dialog_position(SAMPLE_MCP_DIALOG, "google-workspace") == 0

    def test_leanspecs_is_position_two(self):
        # After google-workspace, iris-qa → leanspecs is idx 2.
        assert _server_dialog_position(SAMPLE_MCP_DIALOG, "leanspecs") == 2

    def test_cross_section_position(self):
        # mock-refresh is in Local MCPs, after the 5 Project MCPs → idx 5.
        assert _server_dialog_position(SAMPLE_MCP_DIALOG, "mock-refresh") == 5

    def test_missing_server_returns_none(self):
        assert _server_dialog_position(SAMPLE_MCP_DIALOG, "nonexistent") is None


class TestExitCodes:
    def test_update_manager_exit_code_is_42(self):
        # The bash wrapper in scripts/claude-tm case-matches this exact value;
        # changing it requires a coordinated bash update.
        assert EXIT_UPDATE_MANAGER == 42
