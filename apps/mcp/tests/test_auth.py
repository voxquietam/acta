"""Tests for the MCP server's token authentication helper."""

from django.utils import timezone

import pytest

from apps.accounts.models import ApiToken
from apps.accounts.tests.factories import UserFactory
from apps.mcp.auth import AuthenticationError, authenticate_from_env, authenticate_secret


@pytest.mark.django_db
class TestAuthenticateSecret:
    def test_valid_secret_returns_session(self):
        user = UserFactory()
        token, plain = ApiToken.generate(user=user, name="t")
        session = authenticate_secret(plain)
        assert session.user == user
        assert session.token.pk == token.pk

    def test_unknown_secret_raises(self):
        with pytest.raises(AuthenticationError, match="Invalid token"):
            authenticate_secret("definitely-not-a-real-token")

    def test_revoked_token_raises(self):
        user = UserFactory()
        token, plain = ApiToken.generate(user=user, name="t")
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        with pytest.raises(AuthenticationError, match="revoked"):
            authenticate_secret(plain)

    def test_inactive_user_raises(self):
        user = UserFactory(is_active=False)
        _, plain = ApiToken.generate(user=user, name="t")
        with pytest.raises(AuthenticationError, match="inactive"):
            authenticate_secret(plain)


@pytest.mark.django_db
class TestAuthenticateFromEnv:
    def test_missing_env_var_raises(self, monkeypatch):
        monkeypatch.delenv("ACTA_API_TOKEN", raising=False)
        with pytest.raises(AuthenticationError, match="ACTA_API_TOKEN"):
            authenticate_from_env()

    def test_empty_env_var_raises(self, monkeypatch):
        monkeypatch.setenv("ACTA_API_TOKEN", "   ")
        with pytest.raises(AuthenticationError, match="ACTA_API_TOKEN"):
            authenticate_from_env()

    def test_valid_env_var_returns_session(self, monkeypatch):
        user = UserFactory()
        _, plain = ApiToken.generate(user=user, name="t")
        monkeypatch.setenv("ACTA_API_TOKEN", plain)
        session = authenticate_from_env()
        assert session.user == user
