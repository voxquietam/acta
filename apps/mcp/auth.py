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
import time

from django.core.cache import cache

from apps.accounts.models import ApiToken, User


class AuthenticationError(Exception):
    """Raised when the supplied token is missing, invalid, or revoked."""


class RateLimitExceeded(Exception):
    """Raised when a token exceeds its per-minute request quota."""


# Per-token requests-per-minute ceiling. Conservative default —
# enough headroom for normal LLM-driven flows (~10-30 tool calls per
# agent turn) but blocks runaway loops / accidental DDoS. Override
# via the ``ACTA_MCP_RATE_LIMIT_PER_MINUTE`` env var if needed.
DEFAULT_RATE_LIMIT_PER_MINUTE = 60


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


def _rate_limit_per_minute() -> int:
    """Resolve the per-minute quota at call time.

    Reads ``ACTA_MCP_RATE_LIMIT_PER_MINUTE`` from the env each time so
    operators can bump the ceiling without redeploying. Falls back to
    the conservative ``DEFAULT_RATE_LIMIT_PER_MINUTE`` if the env var
    is missing or unparseable.
    """
    raw = os.environ.get("ACTA_MCP_RATE_LIMIT_PER_MINUTE", "").strip()
    if not raw:
        return DEFAULT_RATE_LIMIT_PER_MINUTE
    try:
        n = int(raw)
        return n if n > 0 else DEFAULT_RATE_LIMIT_PER_MINUTE
    except ValueError:
        return DEFAULT_RATE_LIMIT_PER_MINUTE


def enforce_rate_limit(token: ApiToken) -> None:
    """Bump the per-token, per-minute counter and reject if over quota.

    Implementation: a Django cache key keyed by ``token_hash`` and the
    current minute bucket. Cheap to read (LocMem cache for single-
    worker setups, Redis for multi-worker — same API). The counter is
    incremented atomically via ``cache.incr`` with a one-time
    initialiser fallback in case the key just expired.

    Args:
        token: The authenticated :class:`ApiToken` row.

    Raises:
        RateLimitExceeded: When the token has issued more than the
            quota within the current 60-second window.
    """
    limit = _rate_limit_per_minute()
    bucket = int(time.time() // 60)
    key = f"mcp:rl:{token.token_hash}:{bucket}"
    try:
        count = cache.incr(key)
    except ValueError:
        # Key didn't exist yet — initialise to 1 with a 2-minute TTL
        # so the bucket survives slight clock skew without leaking
        # entries forever.
        cache.set(key, 1, timeout=120)
        count = 1
    if count > limit:
        raise RateLimitExceeded(
            f"Rate limit exceeded: {limit} requests per minute. "
            "Wait a moment, then retry. Override the cap via the "
            "ACTA_MCP_RATE_LIMIT_PER_MINUTE env var on the server.",
        )
