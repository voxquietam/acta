"""Per-request context flag for the MCP transports.

Set by the MCP request entrypoints (HTTP view + stdio command) for the
duration of a single tool call, read by
:func:`apps.tasks.events.broadcast_task_events` to mark SSE payloads as
``via_mcp``. The browser uses that flag to skip the "ignore self"
filter — a MCP-driven write came from a *different* client (Claude
Desktop, Cursor, curl) so the browser tab needs to apply the swap
even when the acting user is the one logged in.

Implemented as a :class:`contextvars.ContextVar` so per-request state
doesn't leak between concurrent ASGI requests sharing the worker.
"""

from __future__ import annotations

import contextlib
import contextvars

IS_MCP_REQUEST: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "acta_is_mcp_request",
    default=False,
)


@contextlib.contextmanager
def mcp_request_scope():
    """Mark the current task/thread as serving a MCP request.

    Use as ``with mcp_request_scope(): ...`` around any code path
    that calls into ORM mutations whose SSE broadcasts should reach
    the originating user's browser tab.
    """
    token = IS_MCP_REQUEST.set(True)
    try:
        yield
    finally:
        IS_MCP_REQUEST.reset(token)
