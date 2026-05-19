"""Launch Acta's MCP server over stdio.

Wired into Django so settings, DB connections, and migrations are
ready before the server reads its first MCP message. MCP clients
(Claude Desktop, Cursor) spawn this command with their stdio pipe
hooked to ours; the ``ACTA_API_TOKEN`` env var supplied in the
client's MCP config authenticates every tool call.

Example Claude Desktop config snippet:

    {
      "mcpServers": {
        "acta": {
          "command": "/path/to/acta/.venv/bin/python",
          "args": ["/path/to/acta/manage.py", "mcp_serve"],
          "env": { "ACTA_API_TOKEN": "<paste-token-from-/accounts/settings/>" }
        }
      }
    }
"""

from __future__ import annotations

import asyncio

from django.core.management.base import BaseCommand

from mcp.server.stdio import stdio_server

from apps.mcp.server import build_server


class Command(BaseCommand):
    """``./manage.py mcp_serve`` — runs the MCP server over stdio."""

    help = "Launch Acta's MCP server over stdio (for Claude Desktop / Cursor / Cline)."

    def handle(self, *args, **options):
        """Boot the asyncio event loop and serve until stdin closes."""
        asyncio.run(_serve())


async def _serve():
    """Hand stdio pipes to the MCP server and run until disconnect."""
    server = build_server()
    init_options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)
