"""Tests for the MCP server bootstrap + the ``acta_ping`` tool.

The MCP framework runs over stdio, but we don't need to spin up an
actual subprocess to test the tool catalogue. We grab the internal
handlers off the :class:`Server` instance and call them with the
same kind of payloads the framework would send. That keeps the tests
fast and deterministic.
"""

import asyncio
import json

import pytest

from apps.accounts.models import ApiToken
from apps.accounts.tests.factories import UserFactory
from apps.mcp.server import ACTA_MCP_VERSION, build_server


def _run(coro):
    """Run an async coroutine in a fresh event loop for one test."""
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.iscoroutine(coro) else asyncio.run(coro)


def _resolve_handlers(server):
    """Return ``(list_tools_handler, call_tool_handler)`` from the server.

    The MCP SDK stores registered handlers on a ``request_handlers``
    mapping keyed by request type. We look them up by class name so
    SDK refactors that rename the internal request types don't break
    these tests silently.
    """
    handlers = {type(req).__name__: handler for req, handler in server.request_handlers.items()}
    return handlers.get("ListToolsRequest"), handlers.get("CallToolRequest")


@pytest.mark.django_db(transaction=True)
class TestMcpServer:
    def test_list_tools_includes_ping(self):
        server = build_server()
        list_handler, _ = _resolve_handlers(server)
        # Bypass the request envelope — the @list_tools decorator
        # wraps a 0-arg coroutine that just returns the catalogue.
        # We invoke the underlying function directly by reaching for
        # the registered request handler's bound callable.
        result = asyncio.run(_invoke_list_tools(server))
        tool_names = {tool.name for tool in result}
        assert "acta_ping" in tool_names

    def test_acta_ping_returns_version_and_user(self, monkeypatch):
        user = UserFactory(username="vox", first_name="Vox", last_name="Quietam")
        _, plain = ApiToken.generate(user=user, name="claude-desktop")
        monkeypatch.setenv("ACTA_API_TOKEN", plain)

        server = build_server()
        result = asyncio.run(_invoke_call_tool(server, "acta_ping", {}))
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["version"] == ACTA_MCP_VERSION
        assert payload["user"] == "vox"
        assert payload["display_name"] == "Vox Quietam"

    def test_unknown_tool_raises(self, monkeypatch):
        user = UserFactory()
        _, plain = ApiToken.generate(user=user, name="t")
        monkeypatch.setenv("ACTA_API_TOKEN", plain)
        server = build_server()
        with pytest.raises(ValueError, match="Unknown tool"):
            asyncio.run(_invoke_call_tool(server, "acta.does_not_exist", {}))

    def test_auth_failure_surfaces_to_client(self, monkeypatch):
        monkeypatch.delenv("ACTA_API_TOKEN", raising=False)
        server = build_server()
        with pytest.raises(RuntimeError, match="authentication failed"):
            asyncio.run(_invoke_call_tool(server, "acta_ping", {}))


async def _invoke_list_tools(server):
    """Find and call the ``list_tools`` handler the same way MCP does."""
    from mcp.types import ListToolsRequest

    handler = server.request_handlers[ListToolsRequest]
    req = ListToolsRequest(method="tools/list")
    result = await handler(req)
    # ServerResult wraps the actual ListToolsResult; the tools are
    # always under ``.tools`` regardless of the wrapper depth.
    return getattr(result.root, "tools", getattr(result, "tools", []))


async def _invoke_call_tool(server, name, arguments):
    """Invoke a tool by name with the given arguments dict."""
    from mcp.types import CallToolRequest, CallToolRequestParams

    handler = server.request_handlers[CallToolRequest]
    req = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    result = await handler(req)
    # ServerResult → CallToolResult; tools/call returns a list of
    # content blocks under ``.content``. If the SDK changes the
    # envelope shape we'll see a clear AttributeError here.
    root = getattr(result, "root", result)
    if getattr(root, "isError", False):
        # Mirror what the MCP framework would do — re-raise the
        # underlying error so tests can match on the message.
        raise _unwrap_error(root)
    return root.content


def _unwrap_error(call_tool_result):
    """Pull the original exception message out of a CallToolResult error.

    MCP wraps thrown exceptions as a TextContent block with
    ``isError=True``. The text follows the pattern
    ``"<ExceptionClassName>: <message>"`` so we can re-instantiate a
    matching exception for the test assertion.
    """
    text = call_tool_result.content[0].text if call_tool_result.content else ""
    if "Unknown tool" in text:
        return ValueError(text)
    if "authentication failed" in text.lower():
        return RuntimeError(text)
    return RuntimeError(text)
