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


#: Tool name → the arguments its ``inputSchema`` declares, for the tools
#: that close their schema. Built once; the catalogue is static.
ALLOWED_ARGS = {
    tool.name: set((tool.inputSchema or {}).get("properties", {}))
    for tool in TOOLS
    if (tool.inputSchema or {}).get("additionalProperties") is False
}


def reject_unknown_arguments(name, args):
    """Refuse a call carrying arguments the tool does not declare.

    Every schema says ``additionalProperties: False``. The MCP SDK
    enforces that on the stdio transport, but the HTTP transport in
    :mod:`apps.mcp.views` dispatches straight from ``CALLABLES`` — so
    over HTTP an unknown key used to reach the handler, be ignored, and
    the call still answered with success. That is the worst answer
    available: ``acta_task_update`` with ``kind="epic"`` reported a
    conversion it had not performed. A caller cannot tell a no-op from
    a success, so it must not be given one.

    Args:
        name: The tool being called.
        args: The arguments the client sent.

    Raises:
        ValueError: Naming the unknown arguments and what the tool takes.
    """
    allowed = ALLOWED_ARGS.get(name)
    if allowed is None:
        return
    unknown = sorted(set(args) - allowed)
    if not unknown:
        return
    raise ValueError(
        f"{name} does not take {', '.join(unknown)}. " f"It takes: {', '.join(sorted(allowed)) or '(no arguments)'}.",
    )


__all__ = ["TOOLS", "CALLABLES", "ALLOWED_ARGS", "reject_unknown_arguments"]
