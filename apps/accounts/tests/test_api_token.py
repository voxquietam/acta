"""Tests for the ``ApiToken`` model and its DRF auth backend.

Covered:

* Plain secret is shown ONCE on ``generate``, never stored or returned later.
* ``token_hash`` storage is the SHA-256 digest of the plain secret.
* ``ApiTokenAuthentication`` accepts a valid ``Authorization: Token …``
  header and populates ``request.user``.
* Revoked tokens fail authentication with a clear error.
* Invalid / missing tokens fall through (no auth, not an exception)
  so SessionAuthentication can still apply for browser clients.
* Successful auth bumps ``last_used_at``.
* Inactive users can't auth even with a valid (un-revoked) token.
"""

from django.utils import timezone

import pytest
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from apps.accounts.auth import ApiTokenAuthentication
from apps.accounts.models import ApiToken
from apps.accounts.tests.factories import UserFactory


def _auth(request_factory, header_value):
    """Build a request with the given Authorization header + run auth.

    Returns the authentication backend's return value
    (``(user, token)`` tuple or ``None``) or re-raises if it failed.
    """
    req = request_factory.get("/api/v1/tasks/", HTTP_AUTHORIZATION=header_value)
    return ApiTokenAuthentication().authenticate(req)


@pytest.fixture
def rf():
    return APIRequestFactory()


@pytest.mark.django_db
class TestApiTokenModel:
    def test_generate_returns_plain_only_once(self):
        user = UserFactory()
        token, plain = ApiToken.generate(user=user, name="Claude Desktop")
        # Plain is a non-empty urlsafe string of reasonable entropy.
        assert plain
        assert len(plain) >= 32
        # The instance stores ONLY the hash, never the plain.
        assert token.token_hash == ApiToken.hash_secret(plain)
        assert plain not in token.token_hash
        # Prefix is the first 8 chars of the plain secret for UI
        # identification (e.g. ``Claude Desktop (a1b2c3d4…)``).
        assert token.prefix == plain[:8]

    def test_hash_is_deterministic(self):
        assert ApiToken.hash_secret("abc") == ApiToken.hash_secret("abc")
        assert ApiToken.hash_secret("abc") != ApiToken.hash_secret("ABC")

    def test_is_active_flips_on_revoke(self):
        user = UserFactory()
        token, _ = ApiToken.generate(user=user, name="t")
        assert token.is_active
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        assert not token.is_active

    def test_str_includes_name_and_prefix(self):
        user = UserFactory()
        token, _ = ApiToken.generate(user=user, name="Claude Desktop")
        rendered = str(token)
        assert "Claude Desktop" in rendered
        assert token.prefix in rendered


@pytest.mark.django_db
class TestApiTokenAuthentication:
    def test_valid_token_authenticates(self, rf):
        user = UserFactory()
        _, plain = ApiToken.generate(user=user, name="t")
        result = _auth(rf, f"Token {plain}")
        assert result is not None
        authed_user, token = result
        assert authed_user == user
        assert token.user == user

    def test_no_header_returns_none(self, rf):
        # No ``Authorization`` header at all — DRF falls through to
        # the next authenticator (SessionAuth) instead of raising.
        result = ApiTokenAuthentication().authenticate(rf.get("/api/v1/tasks/"))
        assert result is None

    def test_non_token_scheme_returns_none(self, rf):
        # ``Bearer`` (OAuth-style) is not our scheme — let other
        # authenticators try, don't raise.
        result = _auth(rf, "Bearer abc123")
        assert result is None

    def test_malformed_header_raises(self, rf):
        # ``Token`` keyword present but no credential after it.
        with pytest.raises(AuthenticationFailed):
            _auth(rf, "Token ")

    def test_unknown_token_raises(self, rf):
        with pytest.raises(AuthenticationFailed):
            _auth(rf, "Token thisisnotarealtokenatall")

    def test_revoked_token_rejected(self, rf):
        user = UserFactory()
        token, plain = ApiToken.generate(user=user, name="t")
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        with pytest.raises(AuthenticationFailed):
            _auth(rf, f"Token {plain}")

    def test_inactive_user_rejected(self, rf):
        user = UserFactory(is_active=False)
        _, plain = ApiToken.generate(user=user, name="t")
        with pytest.raises(AuthenticationFailed):
            _auth(rf, f"Token {plain}")

    def test_last_used_at_bumps_on_successful_auth(self, rf):
        user = UserFactory()
        token, plain = ApiToken.generate(user=user, name="t")
        assert token.last_used_at is None
        _auth(rf, f"Token {plain}")
        token.refresh_from_db()
        assert token.last_used_at is not None

    def test_authenticate_header_is_token_scheme(self, rf):
        # ``WWW-Authenticate`` challenge for 401 responses.
        assert ApiTokenAuthentication().authenticate_header(rf.get("/")) == "Token"
