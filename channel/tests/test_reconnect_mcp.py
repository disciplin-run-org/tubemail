"""Tests for /mcp dialog screen parsing used by reconnect_mcp."""

from __future__ import annotations

from tubemail.manager import (
    EXIT_UPDATE_MANAGER,
    _extract_mcp_server_list,
    _parse_detail_menu_options,
    _server_dialog_position,
)

# Real detail-view samples captured 2026-05-17. Two layouts cover the
# observed states: a ✘ failed / not-authenticated server (3 options, with
# Reconnect at position 2) and a ✔ connected / authenticated server (5
# options, with Reconnect at position 4). The bug was a hardcoded "2"
# selection that happened to work for layout A and silently broke layout B.

SAMPLE_DETAIL_3_ITEM_FAILED = """Plugin:github:github MCP Server

Status:           ✘ failed
Issue:            Streamable HTTP error: Error POSTing to endpoint: bad request: Authorization header is badly formatted

Auth:             ✘ not authenticated
URL:              https://api.githubcopilot.com/mcp/
Config location:  Dynamically configured

❯ 1. Authenticate
  2. Reconnect
  3. Disable

↑/↓ to navigate · Enter to select · Esc to back
"""

SAMPLE_DETAIL_5_ITEM_CONNECTED = """Quartermaster MCP Server

Status:           ✔ connected
Auth:             ✔ authenticated
URL:              http://localhost:8002/mcp/
Config location:  /home/jesper/PycharmProjects/disciplin-run/leanspecs/.mcp.json
Capabilities: tools
Tools: 47 tools

❯ 1. View tools
  2. Re-authenticate
  3. Clear authentication
  4. Reconnect
  5. Disable

↑/↓ to navigate · Enter to select · Esc to back
"""


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


class TestParseDetailMenuOptions:
    def test_three_item_failed_menu(self):
        # ✘ failed + ✘ not authenticated: 3-item layout
        opts = _parse_detail_menu_options(SAMPLE_DETAIL_3_ITEM_FAILED)
        assert opts == {
            "Authenticate": 1,
            "Reconnect": 2,
            "Disable": 3,
        }

    def test_five_item_connected_menu(self):
        # ✔ connected + ✔ authenticated: 5-item layout where Reconnect is row 4,
        # NOT row 2. This is the layout the old hardcoded "2" silently broke on.
        opts = _parse_detail_menu_options(SAMPLE_DETAIL_5_ITEM_CONNECTED)
        assert opts == {
            "View tools": 1,
            "Re-authenticate": 2,
            "Clear authentication": 3,
            "Reconnect": 4,
            "Disable": 5,
        }

    def test_reconnect_position_differs_by_layout(self):
        # The core regression: same label, different row depending on state.
        # Any driver that hardcodes a row number for Reconnect will break on
        # at least one of these layouts.
        three = _parse_detail_menu_options(SAMPLE_DETAIL_3_ITEM_FAILED)
        five = _parse_detail_menu_options(SAMPLE_DETAIL_5_ITEM_CONNECTED)
        assert three["Reconnect"] != five["Reconnect"]
        assert three["Reconnect"] == 2
        assert five["Reconnect"] == 4

    def test_body_field_with_digits_not_matched(self):
        # The 5-item sample contains "Tools: 47 tools" in the header body.
        # That line lacks the "N. " pattern (no period after a leading digit)
        # so it must NOT appear as a menu option.
        opts = _parse_detail_menu_options(SAMPLE_DETAIL_5_ITEM_CONNECTED)
        for label in opts:
            assert "47" not in label
            assert "Tools: " not in label

    def test_footer_help_line_not_matched(self):
        # "↑/↓ to navigate · Enter to select · Esc to back" has no "N. " prefix.
        opts = _parse_detail_menu_options(SAMPLE_DETAIL_3_ITEM_FAILED)
        assert "Enter to select · Esc to back" not in opts
        assert "to navigate" not in opts

    def test_empty_screen_returns_empty_dict(self):
        assert _parse_detail_menu_options("") == {}

    def test_main_dialog_screen_returns_empty_dict(self):
        # The /mcp top-level server list uses "name · status" — none of those
        # lines have the "N. " row-number pattern, so the detail-menu parser
        # must return an empty mapping for it (callers can distinguish "no
        # detail view rendered yet" from "detail view rendered, parsed N
        # options").
        assert _parse_detail_menu_options(SAMPLE_MCP_DIALOG) == {}


class TestExitCodes:
    def test_update_manager_exit_code_is_42(self):
        # The bash wrapper in scripts/claude-tm case-matches this exact value;
        # changing it requires a coordinated bash update.
        assert EXIT_UPDATE_MANAGER == 42
