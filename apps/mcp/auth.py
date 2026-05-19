"""Authentication helpers for the MCP server.

MCP runs over stdio (local: Claude Desktop / Cursor) and HTTP
(hosted, future). For both transports the same primitive applies:
the calling client sends a token, the server looks up the matching
:class:`ApiToken`, and every tool call runs as that token's owner.

For stdio, the token is supplied via the ``ACTA_API_TOKEN`` env var
in the MCP client's config (e.g. Claude Desktop's
``~/.../claude_desktop_config.json`` ``env`` block). For HTTP it
would arrive in the ``Authorization`` header; that path lives in a
later step.
"""

from __future__ import annotations

from dataclasses import dataclass
import os

from apps.accounts.models import ApiToken, User


class AuthenticationError(Exception):
    """Raised when the supplied token is missing, invalid, or revoked."""


@dataclass(frozen=True)
class AuthenticatedSession:
    """The result of a successful token authentication.

    Tools query this to run as the calling user. Both fields are read
    fresh from the DB on each tool call — no in-memory user-state
    cache, so revocation takes effect on the next call.
    """

    user: User
    token: ApiToken


def authenticate_from_env() -> AuthenticatedSession:
    """Look up the API token from ``ACTA_API_TOKEN`` and return the session.

    Returns:
        AuthenticatedSession with the resolved user + token row.

    Raises:
        AuthenticationError: If the env var is missing / empty / no
            matching token exists / the token is revoked / the user
            is inactive. Each branch carries a distinct message so
            misconfigured MCP clients get a useful error in the
            client log.
    """
    secret = os.environ.get("ACTA_API_TOKEN", "").strip()
    if not secret:
        raise AuthenticationError(
            "ACTA_API_TOKEN env var is missing. "
            "Generate a token in /accounts/settings/ and pass it in your MCP client config.",
        )
    return authenticate_secret(secret)


def authenticate_secret(secret: str) -> AuthenticatedSession:
    """Validate a token secret and return the session.

    Pure function so callers (env var, HTTP header, or test
    fixtures) can supply the secret from any source.

    Args:
        secret: Plain-text token as presented by the client.

    Returns:
        AuthenticatedSession with the resolved user + token row.

    Raises:
        AuthenticationError: If no token matches, the token is
            revoked, or the user account is inactive.
    """
    try:
        token = ApiToken.objects.select_related("user").get(token_hash=ApiToken.hash_secret(secret))
    except ApiToken.DoesNotExist:
        raise AuthenticationError("Invalid token: no matching credential in Acta.")
    if token.revoked_at is not None:
        raise AuthenticationError("Token has been revoked. Generate a new one in /accounts/settings/.")
    if not token.user.is_active:
        raise AuthenticationError("User account is inactive.")
    return AuthenticatedSession(user=token.user, token=token)
