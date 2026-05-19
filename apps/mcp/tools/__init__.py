"""Tool registry for the Acta MCP server.

Aggregates read-only tools (``apps.mcp.tools.read``) and mutating
tools (``apps.mcp.tools.write``) into the unified ``TOOLS`` / ``CALLABLES``
the server builder uses to register them with the MCP framework.

Each tool is a sync callable taking ``(user, arguments)`` and returning
a JSON-serialisable payload. The dispatcher in
``apps.mcp.server.build_server`` wraps the call in ``sync_to_async``
so Django ORM access stays sync.

The payload shape is documented inline on each tool's description so
the LLM can produce well-shaped follow-up requests without needing a
separate schema lookup.
"""

from __future__ import annotations

from apps.mcp.tools import read, write

TOOLS = read.TOOLS + write.TOOLS

CALLABLES = {**read.CALLABLES, **write.CALLABLES}


__all__ = ["TOOLS", "CALLABLES"]
