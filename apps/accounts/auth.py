"""Custom DRF authentication backends for Acta.

Currently exposes :class:`ApiTokenAuthentication` — a Bearer-style
token authenticator wired to the :class:`ApiToken` model. Programmatic
clients (curl, scripts, the planned MCP server in
``project_todo_mcp_server``) authenticate by sending
``Authorization: Token <secret>`` instead of relying on the web
session cookie.
"""

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rest_framework import authentication, exceptions

from apps.accounts.models import ApiToken


class ApiTokenAuthentication(authentication.BaseAuthentication):
    """DRF auth backend that validates the ``Authorization: Token <…>`` header.

    The plain secret presented by the client is hashed (SHA-256) and
    looked up against :attr:`ApiToken.token_hash`. Successful auth
    populates ``request.user`` with the token's owner and
    ``request.auth`` with the :class:`ApiToken` instance, so DRF
    permission classes and view code that already work with session
    auth keep working unchanged.

    Side effect: each successful authentication bumps
    :attr:`ApiToken.last_used_at` so the user can see in the
    management UI which tokens are stale (and safe to revoke).
    """

    keyword = "Token"

    def authenticate(self, request):
        """Parse the header and look the token up.

        Returns:
            ``(user, token)`` tuple on success, ``None`` if no token
            header is present (so DRF falls through to the next
            authenticator in the chain — typically SessionAuth).

        Raises:
            AuthenticationFailed: When the header is present but
                malformed, the token is revoked, or no token matches
                the supplied secret. Distinct messages help debugging
                without leaking which side of the comparison failed.
        """
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header:
            return None

        parts = auth_header.split(maxsplit=1)
        if parts[0] != self.keyword:
            # Not our scheme — let other authenticators take a shot
            # (SessionAuthentication ignores Authorization headers
            # entirely, so this doesn't short-circuit it).
            return None
        if len(parts) != 2 or not parts[1]:
            raise exceptions.AuthenticationFailed(_("Invalid token header: missing credentials"))

        secret = parts[1].strip()
        try:
            token = ApiToken.objects.select_related("user").get(token_hash=ApiToken.hash_secret(secret))
        except ApiToken.DoesNotExist:
            raise exceptions.AuthenticationFailed(_("Invalid token"))

        if token.revoked_at is not None:
            raise exceptions.AuthenticationFailed(_("Token has been revoked"))
        if not token.user.is_active:
            raise exceptions.AuthenticationFailed(_("User account is inactive"))

        # Bump last_used_at without touching ``updated_at`` semantics
        # on User. ``update_fields`` keeps this to one column write.
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())

        return (token.user, token)

    def authenticate_header(self, request):
        """Return the ``WWW-Authenticate`` value for 401 responses.

        DRF uses this to populate the challenge header when no
        authentication succeeds, telling the client which scheme to
        use on retry.
        """
        return self.keyword
