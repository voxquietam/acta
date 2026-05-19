"""HTTP transport for the Acta MCP server.

Companion to :mod:`apps.mcp.server`'s stdio transport. stdio is the
admin-local path (one ``ACTA_API_TOKEN`` env var, no public network).
This view is the prod-shared path: any user with an API token from
``/accounts/settings/`` can point their MCP client at
``https://actaspace.com/mcp/`` and authenticate with the
``Authorization: Token <secret>`` header — no SSH, no wrapper script.

Wire protocol: a minimal subset of MCP Streamable HTTP in JSON-response
mode. Each POST is one JSON-RPC envelope; the response is one
JSON-RPC envelope back with ``Content-Type: application/json``. We
don't keep sessions between requests — every request reauthenticates,
so token revocation in the UI takes effect on the next call.

We deliberately skip the full SDK transport
(:class:`mcp.server.streamable_http_manager.StreamableHTTPSessionManager`):
its session lifecycle + SSE streaming buy nothing for short, sync tools
(every Acta tool resolves in well under one HTTP timeout). If we ever
add a streaming tool we can swap to the SDK transport without changing
the URL.
"""

from __future__ import annotations

import json
from typing import Any

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from asgiref.sync import sync_to_async

from apps.mcp.auth import (
    AuthenticatedSession,
    AuthenticationError,
    RateLimitExceeded,
    authenticate_secret,
    enforce_rate_limit,
)
from apps.mcp.context import mcp_request_scope
from apps.mcp.server import _PING_TOOL, ACTA_MCP_VERSION
from apps.mcp.tools import CALLABLES, TOOLS

# Protocol version we advertise back to the client during ``initialize``.
# Matches what ``mcp_serve`` (stdio) negotiates so the two transports
# behave identically.
MCP_PROTOCOL_VERSION = "2024-11-05"


@csrf_exempt
@require_http_methods(["POST"])
async def mcp_http(request: HttpRequest) -> JsonResponse:
    """Single-endpoint MCP JSON-RPC handler.

    Method routing (per MCP spec):
      - ``initialize``           handshake; returns server info + capabilities
      - ``ping``                 keepalive; returns ``{}``
      - ``tools/list``           catalogue
      - ``tools/call``           invoke one tool, return content
      - ``notifications/*``      fire-and-forget; we ack with 202

    Auth runs before parsing the body so a malformed but unauthenticated
    request still gets a clean 401 instead of leaking parser internals.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Token "):
        return JsonResponse(
            _error(None, -32001, "Missing or malformed Authorization header. Expected: Token <secret>."),
            status=401,
        )
    secret = auth_header.removeprefix("Token ").strip()

    try:
        session = await sync_to_async(authenticate_secret)(secret)
    except AuthenticationError as exc:
        return JsonResponse(_error(None, -32001, str(exc)), status=401)

    try:
        await sync_to_async(enforce_rate_limit)(session.token)
    except RateLimitExceeded as exc:
        return JsonResponse(_error(None, -32002, str(exc)), status=429)

    try:
        msg = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        return JsonResponse(_error(None, -32700, f"Parse error: {exc.msg}"), status=400)

    method = msg.get("method")
    params = msg.get("params") or {}
    req_id = msg.get("id")

    # Notifications carry no ``id`` per JSON-RPC 2.0 and don't expect a
    # response body. Ack with 202 to keep the client moving.
    if req_id is None and isinstance(method, str) and method.startswith("notifications/"):
        return JsonResponse({}, status=202)

    # Mark every ORM write under this request as MCP-driven so SSE
    # payloads carry ``via_mcp`` and the browser's self-filter applies
    # the swap instead of dropping the event.
    with mcp_request_scope():
        return JsonResponse(await _dispatch(session, method, params, req_id))


async def _dispatch(session: AuthenticatedSession, method: str | None, params: dict, req_id: Any) -> dict:
    """Route one JSON-RPC method to its handler.

    Unknown methods return a JSON-RPC ``-32601 Method not found`` so
    forward-compatible clients can fall back gracefully.
    """
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "acta", "version": ACTA_MCP_VERSION},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        all_tools = [_PING_TOOL, *TOOLS]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": [t.model_dump(exclude_none=True, by_alias=True) for t in all_tools]},
        }

    if method == "tools/call":
        return await _call_tool(session, params, req_id)

    return _error(req_id, -32601, f"Method not found: {method!r}")


async def _call_tool(session: AuthenticatedSession, params: dict, req_id: Any) -> dict:
    """Invoke one tool from CALLABLES (or the inline ``acta_ping``)."""
    name = params.get("name")
    arguments = params.get("arguments") or {}

    if name == "acta_ping":
        payload = {
            "version": ACTA_MCP_VERSION,
            "user": session.user.username,
            "display_name": session.user.display_name,
        }
        return _tool_result(req_id, payload)

    handler = CALLABLES.get(name)
    if handler is None:
        return _error(req_id, -32602, f"Unknown tool: {name!r}")

    try:
        payload = await sync_to_async(handler)(session.user, arguments)
    except Exception as exc:
        # Surface tool errors as ``isError=True`` results per MCP spec
        # so the LLM can react (retry with different args, give up,
        # ask the user) instead of the whole transport failing.
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            },
        }
    return _tool_result(req_id, payload)


def _tool_result(req_id: Any, payload: Any) -> dict:
    """Wrap a tool payload in MCP's ``CallToolResult`` shape."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)},
            ],
            "isError": False,
        },
    }


def _error(req_id: Any, code: int, message: str) -> dict:
    """Build a JSON-RPC error envelope."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }
