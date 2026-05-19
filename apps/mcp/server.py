"""MCP server bootstrap for Acta.

Builds the :class:`mcp.server.Server` instance and registers all
tools. Wired up by ``manage.py mcp_serve`` so Django settings are
loaded before any model import.

Tool catalogue lives in :mod:`apps.mcp.tools`. ``acta_ping`` here is
the connectivity / auth-check tool that doesn't fit the data-tool
pattern (no DB read) so it stays inline.
"""

from __future__ import annotations

import json
from typing import Any

from asgiref.sync import sync_to_async
from mcp.server import Server
from mcp.types import TextContent, Tool

from apps.mcp.auth import AuthenticationError, RateLimitExceeded, authenticate_from_env, enforce_rate_limit
from apps.mcp.context import mcp_request_scope
from apps.mcp.tools import CALLABLES, TOOLS

ACTA_MCP_VERSION = "0.1.0"


_PING_TOOL = Tool(
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
)


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
        return [_PING_TOOL, *TOOLS]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        """Dispatch a tool call. Authenticates per-call.

        Args:
            name: Tool name from the tool catalogue.
            arguments: JSON-shaped arguments declared in the tool's
                ``inputSchema``. ``None`` is normalised to ``{}``.

        Returns:
            A list of :class:`TextContent` items — currently a single
            JSON-encoded response per call.

        Raises:
            Exception: Re-raised with a user-friendly message when
                authentication fails or the tool name is unknown.
                MCP clients surface this as the tool-call error.
        """
        session = await sync_to_async(_safe_authenticate)()
        args = arguments or {}

        if name == "acta_ping":
            payload = {
                "version": ACTA_MCP_VERSION,
                "user": session.user.username,
                "display_name": session.user.display_name,
            }
            return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

        handler = CALLABLES.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name!r}")

        # Tool callables hit Django ORM (sync), so jump out of the
        # async loop for the DB pass. The result is JSON-serialisable
        # (list of dicts) and serialised back in this coroutine. The
        # ``mcp_request_scope`` flag marks the SSE broadcasts so the
        # originating user's browser tab applies the swap (instead of
        # dropping it as a "self" event from a different MCP client).
        with mcp_request_scope():
            payload = await sync_to_async(handler)(session.user, args)
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))]

    return server


def _safe_authenticate():
    """Wrap auth + rate-limit so failures read as actionable tool errors.

    Both errors get a distinct prefix so the LLM (or the client log
    user) can tell auth-misconfig apart from quota-exhaustion. The
    rate-limit pass runs ONLY when auth succeeds — otherwise we'd
    rate-limit nameless attackers, which doesn't add security since
    the auth check itself is the bottleneck.
    """
    try:
        session = authenticate_from_env()
    except AuthenticationError as exc:
        raise RuntimeError(f"Acta MCP authentication failed: {exc}") from exc
    try:
        enforce_rate_limit(session.token)
    except RateLimitExceeded as exc:
        raise RuntimeError(f"Acta MCP rate limit: {exc}") from exc
    return session
