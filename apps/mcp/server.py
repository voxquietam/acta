"""MCP server bootstrap for Acta.

Builds the :class:`mcp.server.Server` instance and registers all
tools. Wired up by ``manage.py mcp_serve`` so Django settings are
loaded before any model import.

Tool registry currently has one entry — ``acta_ping`` — which lets a
client verify their token works and confirms which Acta user the MCP
session runs as. Real CRUD tools land in the next phase
([[project-todo-mcp-server]] section "Stage 2 — Read-only tools").
"""

from __future__ import annotations

import json
from typing import Any

from asgiref.sync import sync_to_async
from mcp.server import Server
from mcp.types import TextContent, Tool

from apps.mcp.auth import AuthenticationError, authenticate_from_env

ACTA_MCP_VERSION = "0.1.0"


def build_server() -> Server:
    """Build the MCP server with Acta's tools registered.

    The server itself is stateless — each tool call re-authenticates
    the calling client (via the same ``ACTA_API_TOKEN`` env var so
    token revocation takes effect immediately). Authentication errors
    are surfaced to the client as tool-call errors with a clear
    message so a misconfigured MCP client config can be debugged
    without server logs.
    """
    server = Server("acta")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """Return the catalogue of MCP tools Acta exposes."""
        return [
            Tool(
                name="acta_ping",
                description=(
                    "Verify the MCP connection. Returns the Acta server version "
                    "and the username the session is authenticated as."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        """Dispatch a tool call. Authenticates per-call.

        Args:
            name: Tool name from the tool catalogue.
            arguments: JSON-shaped arguments declared in the tool's
                ``inputSchema``. ``None`` is normalised to ``{}``.

        Returns:
            A list of :class:`TextContent` items — currently a single
            JSON-encoded response. Multi-content responses (e.g.
            embedded resources) are a future extension.

        Raises:
            Exception: Re-raised with a user-friendly message when
                authentication fails or the tool name is unknown.
                MCP clients surface this as the tool-call error.
        """
        session = await sync_to_async(_safe_authenticate)()

        if name == "acta_ping":
            payload = {
                "version": ACTA_MCP_VERSION,
                "user": session.user.username,
                "display_name": session.user.display_name,
            }
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        raise ValueError(f"Unknown tool: {name!r}")

    return server


def _safe_authenticate():
    """Wrap ``authenticate_from_env`` so failure reads as a tool error.

    The MCP framework surfaces uncaught exceptions to the client; a
    bare :class:`AuthenticationError` would still work, but
    re-raising as a generic ``Exception`` with a message that names
    the env var and links to the settings page makes the client log
    actionable.
    """
    try:
        return authenticate_from_env()
    except AuthenticationError as exc:
        raise RuntimeError(f"Acta MCP authentication failed: {exc}") from exc
