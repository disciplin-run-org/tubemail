"""TubeMail MCP server.

Orchestration hub that routes work between Claude Code sessions via the
tubemail channel plugin. Exposes FastMCP tools to an orchestrator session
(send, receive, list_workers, permission relay) and a tubemail HTTP API
to the per-worker forwarders.
"""

__version__ = "0.1.0"
