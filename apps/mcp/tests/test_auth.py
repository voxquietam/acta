"""Tests for the MCP server's token authentication + rate-limit helpers."""

from django.core.cache import cache
from django.utils import timezone

import pytest

from apps.accounts.models import ApiToken
from apps.accounts.tests.factories import UserFactory
from apps.mcp.auth import (
    AuthenticationError,
    RateLimitExceeded,
    authenticate_from_env,
    authenticate_secret,
    enforce_rate_limit,
)


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    """The rate limiter uses Django's cache; clean it so tests don't bleed counts."""
    cache.clear()
    yield
    cache.clear()


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


@pytest.mark.django_db
class TestRateLimit:
    def test_within_quota_passes(self, monkeypatch):
        monkeypatch.setenv("ACTA_MCP_RATE_LIMIT_PER_MINUTE", "5")
        user = UserFactory()
        token, _ = ApiToken.generate(user=user, name="t")
        for _ in range(5):
            enforce_rate_limit(token)  # should not raise

    def test_over_quota_raises(self, monkeypatch):
        monkeypatch.setenv("ACTA_MCP_RATE_LIMIT_PER_MINUTE", "3")
        user = UserFactory()
        token, _ = ApiToken.generate(user=user, name="t")
        for _ in range(3):
            enforce_rate_limit(token)
        with pytest.raises(RateLimitExceeded):
            enforce_rate_limit(token)

    def test_separate_tokens_have_separate_buckets(self, monkeypatch):
        monkeypatch.setenv("ACTA_MCP_RATE_LIMIT_PER_MINUTE", "2")
        user = UserFactory()
        a, _ = ApiToken.generate(user=user, name="a")
        b, _ = ApiToken.generate(user=user, name="b")
        enforce_rate_limit(a)
        enforce_rate_limit(a)
        # Token a is now at its cap, but token b is fresh.
        enforce_rate_limit(b)
        enforce_rate_limit(b)
        with pytest.raises(RateLimitExceeded):
            enforce_rate_limit(b)

    def test_invalid_env_value_falls_back_to_default(self, monkeypatch):
        """A bogus env value should fall back to ``DEFAULT_RATE_LIMIT_PER_MINUTE``
        rather than disabling the limiter outright (which would be a security
        hazard if a typo accidentally turned it off)."""
        from apps.mcp.auth import DEFAULT_RATE_LIMIT_PER_MINUTE

        monkeypatch.setenv("ACTA_MCP_RATE_LIMIT_PER_MINUTE", "not-an-int")
        user = UserFactory()
        token, _ = ApiToken.generate(user=user, name="t")
        # Should hit a high default ceiling — call DEFAULT times, then fail.
        for _ in range(DEFAULT_RATE_LIMIT_PER_MINUTE):
            enforce_rate_limit(token)
        with pytest.raises(RateLimitExceeded):
            enforce_rate_limit(token)
