"""Register the standard refresh_tools MCP tool on any FastMCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import Context

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_refresh_tool(mcp: FastMCP) -> None:
    """Register a refresh_tools tool that notifies clients of tool list changes."""

    @mcp.tool
    async def refresh_tools(ctx: Context) -> str:
        """Refresh the tool list in Claude Code. Call after server rebuild or restart."""
        from mcp import types as mcp_types

        await ctx.send_notification(mcp_types.ToolListChangedNotification())
        return "Tool list refreshed. New and changed tools are now available."
