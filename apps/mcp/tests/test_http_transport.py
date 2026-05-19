"""Tests for the HTTP MCP transport (``/mcp/`` endpoint).

The view is async, but Django's sync test client handles async views
via ``async_to_sync`` internally, so we don't need ``pytest-asyncio``.
We hit ``/mcp/`` over JSON-RPC and assert on the envelope shape — the
same surface a Claude Desktop or Cursor MCP client would see.
"""

import json

from django.core.cache import cache
from django.test import Client
from django.utils import timezone

import pytest

from apps.accounts.models import ApiToken
from apps.accounts.tests.factories import UserFactory
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory

MCP_URL = "/mcp/"


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    """Rate limiter uses Django cache — keep test counts isolated."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    """A regular active user."""
    return UserFactory()


@pytest.fixture
def auth(user):
    """Issue an API token for ``user`` and return ``(token_row, plain_secret)``."""
    return ApiToken.generate(user=user, name="http-test")


def _post(client: Client, body: dict | str, token: str | None = None) -> tuple[int, dict]:
    """POST to ``/mcp/``, optionally with a Token header. Returns ``(status, body)``."""
    headers = {}
    if token is not None:
        headers["HTTP_AUTHORIZATION"] = f"Token {token}"
    payload = body if isinstance(body, str) else json.dumps(body)
    response = client.post(MCP_URL, data=payload, content_type="application/json", **headers)
    try:
        parsed = response.json()
    except ValueError:
        parsed = {}
    return response.status_code, parsed


@pytest.mark.django_db
class TestAuth:
    def test_missing_authorization_header_returns_401(self, client):
        status, body = _post(client, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert status == 401
        assert "Authorization" in body["error"]["message"]

    def test_malformed_scheme_returns_401(self, client):
        status, body = _post(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            token=None,
        )
        # No "Token " prefix at all
        response = client.post(
            MCP_URL,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer abc123",
        )
        assert response.status_code == 401
        assert "Token" in response.json()["error"]["message"]

    def test_unknown_token_returns_401(self, client):
        status, body = _post(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            token="not-a-real-token",
        )
        assert status == 401
        assert "Invalid token" in body["error"]["message"]

    def test_revoked_token_returns_401(self, client, auth):
        token, plain = auth
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        status, body = _post(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            token=plain,
        )
        assert status == 401
        assert "revoked" in body["error"]["message"].lower()

    def test_inactive_user_returns_401(self, client):
        user = UserFactory(is_active=False)
        _, plain = ApiToken.generate(user=user, name="t")
        status, body = _post(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            token=plain,
        )
        assert status == 401
        assert "inactive" in body["error"]["message"].lower()


@pytest.mark.django_db
class TestRateLimit:
    def test_429_after_quota_exceeded(self, client, auth, monkeypatch):
        monkeypatch.setenv("ACTA_MCP_RATE_LIMIT_PER_MINUTE", "3")
        _, plain = auth
        # First three pings are within quota.
        for _ in range(3):
            status, _ = _post(
                client,
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                token=plain,
            )
            assert status == 200
        # Fourth blows past the limit.
        status, body = _post(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            token=plain,
        )
        assert status == 429
        assert "Rate limit" in body["error"]["message"]


@pytest.mark.django_db
class TestProtocol:
    def test_get_is_not_allowed(self, client, auth):
        _, plain = auth
        response = client.get(MCP_URL, HTTP_AUTHORIZATION=f"Token {plain}")
        assert response.status_code == 405

    def test_malformed_json_returns_400(self, client, auth):
        _, plain = auth
        status, body = _post(client, "{this is not json", token=plain)
        assert status == 400
        assert body["error"]["code"] == -32700

    def test_initialize_returns_server_info(self, client, auth):
        _, plain = auth
        status, body = _post(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            token=plain,
        )
        assert status == 200
        result = body["result"]
        assert result["serverInfo"]["name"] == "acta"
        assert result["protocolVersion"] == "2024-11-05"
        assert "tools" in result["capabilities"]

    def test_ping_returns_empty_result(self, client, auth):
        _, plain = auth
        status, body = _post(
            client,
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
            token=plain,
        )
        assert status == 200
        assert body["id"] == 7
        assert body["result"] == {}

    def test_notification_returns_202(self, client, auth):
        _, plain = auth
        # No ``id`` — this is a notification per JSON-RPC.
        response = client.post(
            MCP_URL,
            data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {plain}",
        )
        assert response.status_code == 202

    def test_unknown_method_returns_method_not_found(self, client, auth):
        _, plain = auth
        status, body = _post(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "something/weird"},
            token=plain,
        )
        assert status == 200
        assert body["error"]["code"] == -32601


@pytest.mark.django_db
class TestTools:
    def test_tools_list_returns_full_catalogue(self, client, auth):
        _, plain = auth
        status, body = _post(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            token=plain,
        )
        assert status == 200
        names = {t["name"] for t in body["result"]["tools"]}
        # Spot-check: ping + one read tool + one write tool present.
        assert "acta_ping" in names
        assert "acta_workspaces_list" in names
        assert "acta_task_create" in names

    def test_tools_call_acta_ping_returns_user_info(self, client, auth, user):
        _, plain = auth
        status, body = _post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "acta_ping", "arguments": {}},
            },
            token=plain,
        )
        assert status == 200
        result = body["result"]
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload["user"] == user.username

    def test_tools_call_unknown_tool_returns_error(self, client, auth):
        _, plain = auth
        status, body = _post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "acta_nonexistent", "arguments": {}},
            },
            token=plain,
        )
        assert status == 200
        assert body["error"]["code"] == -32602
        assert "Unknown tool" in body["error"]["message"]

    def test_tools_call_workspaces_list_returns_real_data(self, client, auth, user):
        _, plain = auth
        # Two workspaces, user is a member of only one — second must
        # NOT appear in the response so authorization works as expected.
        ws_visible = WorkspaceFactory(name="VisibleToUser")
        WorkspaceMemberFactory(workspace=ws_visible, user=user)
        WorkspaceFactory(name="HiddenFromUser")

        status, body = _post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "acta_workspaces_list", "arguments": {}},
            },
            token=plain,
        )
        assert status == 200
        payload = json.loads(body["result"]["content"][0]["text"])
        names = {ws["name"] for ws in payload}
        assert "VisibleToUser" in names
        assert "HiddenFromUser" not in names
