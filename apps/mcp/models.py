"""OAuth 2.1 rows backing the MCP endpoint's Custom Connector support.

Claude Code talks to ``/mcp/`` with a hand-pasted
``Authorization: Token <secret>`` and needs none of this. Claude Desktop
cannot: its config file only knows how to spawn a local process, so an
HTTP server is reachable either through the ``mcp-remote`` bridge (which
drags in Node.js — a wall for non-technical colleagues) or through
Desktop's own Custom Connectors, which take a bare URL and authenticate
over OAuth. These models are what make the second path possible.

The flow is the public-client shape the MCP specification settled on:
dynamic client registration (RFC 7591), then Authorization Code with
PKCE (RFC 7636). No client secrets anywhere — a desktop app cannot keep
one — so the proof of possession is the PKCE verifier alone.

Issued access tokens are ordinary :class:`~apps.accounts.models.ApiToken`
rows with an ``expires_at``, which keeps one revocation surface for the
user: everything they granted shows up in one list in settings, whether
they pasted it into Claude Code or approved it for Desktop.
"""

from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def _hash(secret: str) -> str:
    """Return the SHA-256 hex digest used to store a credential.

    Args:
        secret: Plain-text credential as presented by the client.

    Returns:
        Lowercase 64-char hex digest. Matches how ``ApiToken`` stores
        its own secrets, so the two are reasoned about the same way.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class OAuthClient(models.Model):
    """A client that registered itself against this server.

    Registration is open by design — RFC 7591 dynamic registration is
    how Desktop bootstraps from nothing but a URL, and refusing it would
    put us back to hand-edited config files. An unauthenticated party can
    therefore create rows here, which is safe because a registration
    grants nothing on its own: every token still requires a human to
    approve the consent screen while logged in.

    ``client_id`` is a random public identifier. There is no secret —
    these are public clients (RFC 6749 §2.1) and PKCE carries the
    security instead.
    """

    client_id = models.CharField(
        max_length=64,
        unique=True,
        help_text="Public identifier issued at registration; travels in every authorize / token request",
    )
    client_name = models.CharField(
        max_length=120,
        help_text="Name the client supplied at registration, shown on the consent screen",
    )
    redirect_uris = models.JSONField(
        default=list,
        help_text="Exact redirect URIs the client registered; an authorize request may use only these",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the client registered itself",
    )
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time this client completed a token exchange; null until first use",
    )

    class Meta:
        verbose_name = _("OAuth client")
        verbose_name_plural = _("OAuth clients")
        ordering = [
            "-created_at",
        ]

    def __str__(self) -> str:
        """Return the client name plus a short id for admin readability."""
        return f"{self.client_name} ({self.client_id[:8]}…)"

    @classmethod
    def register(cls, *, client_name: str, redirect_uris: list[str]) -> "OAuthClient":
        """Create a registration with a freshly generated ``client_id``.

        Args:
            client_name: Human-readable name from the registration request.
            redirect_uris: The URIs this client may be redirected back to.

        Returns:
            The persisted :class:`OAuthClient`.
        """
        return cls.objects.create(
            client_id=secrets.token_urlsafe(24),
            client_name=client_name[:120],
            redirect_uris=list(redirect_uris),
        )

    def allows_redirect(self, uri: str) -> bool:
        """Return whether ``uri`` is one this client registered.

        Compared exactly, never by prefix: a prefix match is the classic
        open-redirect hole, since ``https://good.example/cb`` would then
        also accept ``https://good.example/cb.attacker.test``.

        Args:
            uri: The ``redirect_uri`` from an authorize request.

        Returns:
            True when the URI was registered verbatim.
        """
        return uri in (self.redirect_uris or [])


class OAuthAuthorizationCode(models.Model):
    """A one-shot code handed to the client after the user consents.

    Exchanged at the token endpoint for an access token, and only once:
    ``consumed_at`` is stamped on first use so a replayed code (from a
    leaked redirect, a shared log, browser history) buys nothing. Codes
    are short-lived by spec; ours live ten minutes.

    The code itself is stored hashed, like every other credential here —
    a database read must not yield anything replayable.
    """

    LIFETIME_SECONDS = 600

    code_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="SHA-256 digest of the one-time code; the code itself is never stored",
    )
    client = models.ForeignKey(
        OAuthClient,
        on_delete=models.CASCADE,
        related_name="authorization_codes",
        help_text="Client the code was issued to; the token exchange must present the same one",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oauth_authorization_codes",
        help_text="User who approved the consent screen; tokens minted from this code act as them",
    )
    redirect_uri = models.CharField(
        max_length=500,
        help_text="Redirect URI used in the authorize request; the token exchange must repeat it exactly",
    )
    code_challenge = models.CharField(
        max_length=128,
        help_text="PKCE challenge (S256) the client committed to; the verifier is checked against it",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the code was issued",
    )
    expires_at = models.DateTimeField(
        help_text="Hard expiry; an exchange after this instant is refused",
    )
    consumed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the code was exchanged; a second exchange is refused",
    )

    class Meta:
        verbose_name = _("OAuth authorization code")
        verbose_name_plural = _("OAuth authorization codes")
        ordering = [
            "-created_at",
        ]
        indexes = [
            models.Index(
                fields=[
                    "code_hash",
                ],
            ),
        ]

    def __str__(self) -> str:
        """Return client + user for admin readability."""
        return f"code for {self.client.client_name} / {self.user}"

    @property
    def is_usable(self) -> bool:
        """Return whether this code may still be exchanged."""
        return self.consumed_at is None and self.expires_at > timezone.now()

    def verify_pkce(self, verifier: str) -> bool:
        """Return whether ``verifier`` matches the stored S256 challenge.

        Args:
            verifier: The ``code_verifier`` from the token request.

        Returns:
            True when the base64url-encoded SHA-256 of the verifier
            equals the challenge recorded at authorize time.
        """
        import base64

        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return secrets.compare_digest(computed, self.code_challenge)


class OAuthRefreshToken(models.Model):
    """Long-lived credential that mints fresh access tokens.

    Access tokens expire in an hour so a leaked one has a short life;
    without a refresh token that would mean re-consenting every hour,
    which nobody would tolerate. Rotation is on: each use issues a new
    refresh token and stamps ``replaced_at`` on the old one, so a stolen
    token is usable at most until the legitimate client next refreshes.
    """

    token_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="SHA-256 digest of the refresh secret; the secret itself is never stored",
    )
    client = models.ForeignKey(
        OAuthClient,
        on_delete=models.CASCADE,
        related_name="refresh_tokens",
        help_text="Client the refresh token belongs to",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oauth_refresh_tokens",
        help_text="User the refreshed access tokens will act as",
    )
    access_token = models.ForeignKey(
        "accounts.ApiToken",
        on_delete=models.CASCADE,
        related_name="oauth_refresh_tokens",
        help_text="Access token most recently issued from this refresh token",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the refresh token was issued",
    )
    replaced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When rotation superseded this token; a replaced token is refused",
    )
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the grant was revoked; revoking the access token revokes this too",
    )

    class Meta:
        verbose_name = _("OAuth refresh token")
        verbose_name_plural = _("OAuth refresh tokens")
        ordering = [
            "-created_at",
        ]
        indexes = [
            models.Index(
                fields=[
                    "token_hash",
                ],
            ),
        ]

    def __str__(self) -> str:
        """Return client + user for admin readability."""
        return f"refresh for {self.client.client_name} / {self.user}"

    @property
    def is_usable(self) -> bool:
        """Return whether this refresh token may still be exchanged."""
        return self.replaced_at is None and self.revoked_at is None
