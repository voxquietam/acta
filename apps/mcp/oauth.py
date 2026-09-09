"""OAuth 2.1 endpoints that let Claude Desktop connect by URL alone.

Desktop's Custom Connectors take one field — the MCP endpoint's URL — and
work out everything else by discovery. That is the whole reason this
module exists: without it Desktop can only reach ``/mcp/`` through the
``mcp-remote`` bridge, which needs Node.js installed on the user's
machine and defeats non-technical colleagues.

The chain a client walks, and where each link lives:

1. ``POST /mcp/`` with no credential → ``401`` carrying
   ``WWW-Authenticate: Bearer resource_metadata="…"`` (in
   :mod:`apps.mcp.views`).
2. :func:`protected_resource_metadata` — names this resource and the
   authorization server that guards it (RFC 9728).
3. :func:`authorization_server_metadata` — names the three endpoints
   below (RFC 8414).
4. :func:`register` — the client introduces itself and gets a
   ``client_id`` (RFC 7591). No secret: desktop apps cannot keep one.
5. :func:`authorize` — the human, already logged into Acta in their
   browser, approves the connection. Returns a one-time code.
6. :func:`token` — the code is exchanged for an access token, proven by
   the PKCE verifier (RFC 7636). Later, refresh tokens mint new ones.

Everything hand-issued keeps working untouched: ``Authorization: Token
<secret>`` is still the path Claude Code and the bridge use, and tokens
pasted by hand never expire.
"""

from __future__ import annotations

import datetime
import json
import secrets
from urllib.parse import urlencode

from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.accounts.models import ApiToken
from apps.mcp.models import OAuthAuthorizationCode, OAuthClient, OAuthRefreshToken, _hash

# How long an issued access token lives. Short enough that a leaked one
# stops mattering quickly, long enough that refreshing is not the client's
# main activity. Refresh tokens carry the long-lived grant instead.
ACCESS_TOKEN_LIFETIME = datetime.timedelta(hours=1)

# The only scope we define. MCP has no meaningful scope split — a token
# either acts as its user across the tool set or it is useless — but the
# metadata has to advertise something, and a named scope leaves room to
# subdivide later without changing the shape of the responses.
SCOPE = "acta"


def _issuer(request: HttpRequest) -> str:
    """Return the absolute origin this server is reached at.

    Derived from the request rather than configured, so the same code
    serves ``localhost:8001`` in development and ``actaspace.com`` in
    production without an environment-specific setting to forget.

    Args:
        request: The incoming request.

    Returns:
        Origin string with no trailing slash, e.g. ``https://actaspace.com``.
    """
    return request.build_absolute_uri("/").rstrip("/")


@require_http_methods(["GET"])
def protected_resource_metadata(request: HttpRequest) -> JsonResponse:
    """Describe ``/mcp/`` and name the server that authorizes it (RFC 9728).

    This is the document the ``WWW-Authenticate`` header on an
    unauthenticated MCP call points to — the first thing a client reads
    when it knows nothing but the endpoint URL.

    Args:
        request: The incoming request; only its origin is used.

    Returns:
        ``200`` with the resource metadata document.
    """
    issuer = _issuer(request)
    return JsonResponse(
        {
            "resource": f"{issuer}/mcp/",
            "authorization_servers": [issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [SCOPE],
        }
    )


@require_http_methods(["GET"])
def authorization_server_metadata(request: HttpRequest) -> JsonResponse:
    """Advertise the authorization server's endpoints (RFC 8414).

    ``token_endpoint_auth_methods_supported: ["none"]`` and
    ``code_challenge_methods_supported: ["S256"]`` together say what this
    server is: public clients only, PKCE mandatory. There is no client
    secret to leak because none is ever issued.

    Args:
        request: The incoming request; only its origin is used.

    Returns:
        ``200`` with the authorization-server metadata document.
    """
    issuer = _issuer(request)
    return JsonResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": issuer + reverse("mcp:oauth_authorize"),
            "token_endpoint": issuer + reverse("mcp:oauth_token"),
            "registration_endpoint": issuer + reverse("mcp:oauth_register"),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE],
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def register(request: HttpRequest) -> JsonResponse:
    """Register a client and issue it a ``client_id`` (RFC 7591).

    Deliberately open to unauthenticated callers. That sounds alarming
    and is not: a registration is only a name and a set of redirect URIs,
    and it grants nothing. Every token still requires a signed-in human
    to approve the consent screen. Closing this endpoint would mean
    hand-registering every client, which is exactly the hand-edited
    config we are trying to get rid of.

    Args:
        request: POST carrying the RFC 7591 registration JSON.

    Returns:
        ``201`` with the issued ``client_id``, or ``400`` when the body is
        unparseable or names no usable redirect URI.
    """
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_client_metadata", "error_description": "Body is not JSON."}, status=400)

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JsonResponse(
            {"error": "invalid_redirect_uri", "error_description": "redirect_uris must be a non-empty list."},
            status=400,
        )
    if not all(isinstance(uri, str) and uri for uri in redirect_uris):
        return JsonResponse(
            {"error": "invalid_redirect_uri", "error_description": "Every redirect_uri must be a non-empty string."},
            status=400,
        )

    client = OAuthClient.register(
        client_name=str(body.get("client_name") or "Unnamed MCP client"),
        redirect_uris=redirect_uris,
    )
    return JsonResponse(
        {
            "client_id": client.client_id,
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            # RFC 7591 wants this even when the id never expires.
            "client_id_issued_at": int(client.created_at.timestamp()),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status=201,
    )


def _authorize_error(redirect_uri: str, state: str, code: str, description: str) -> HttpResponse:
    """Bounce an OAuth error back to the client's redirect URI.

    Only used once ``redirect_uri`` has been validated against the
    client's registration — an error sent to an unverified URI would be
    an open redirect, so those are rendered as a plain ``400`` instead.

    Args:
        redirect_uri: The client's verified redirect URI.
        state: Opaque value to echo back, if the client sent one.
        code: OAuth error code, e.g. ``invalid_request``.
        description: Human-readable detail.

    Returns:
        A redirect carrying the error in the querystring.
    """
    params = {"error": code, "error_description": description}
    if state:
        params["state"] = state
    joiner = "&" if "?" in redirect_uri else "?"
    return redirect(f"{redirect_uri}{joiner}{urlencode(params)}")


@login_required
@require_http_methods(["GET", "POST"])
def authorize(request: HttpRequest) -> HttpResponse:
    """Ask the signed-in user to approve a client, then hand it a code.

    ``GET`` renders the consent screen; ``POST`` is the approval. The
    ``login_required`` wrapper is what ties the grant to a real person:
    an unauthenticated visitor is sent to the login page and returns here
    afterwards, so a code can only ever be minted by someone holding a
    valid Acta session.

    Validation order matters. The client and its redirect URI are checked
    *first*, and a failure there renders a ``400`` rather than redirecting
    — bouncing an error to an unverified URI is how open redirects are
    built. Everything after that is safe to report to the client.

    Args:
        request: GET to view the consent screen, POST to approve.

    Returns:
        The consent page, a redirect carrying ``code`` and ``state``, or
        a ``400`` when the client or redirect URI does not check out.
    """
    params = request.POST if request.method == "POST" else request.GET
    client_id = (params.get("client_id") or "").strip()
    redirect_uri = (params.get("redirect_uri") or "").strip()
    state = (params.get("state") or "").strip()
    challenge = (params.get("code_challenge") or "").strip()
    challenge_method = (params.get("code_challenge_method") or "").strip()
    response_type = (params.get("response_type") or "code").strip()

    client = OAuthClient.objects.filter(client_id=client_id).first()
    if client is None:
        return HttpResponseBadRequest("Unknown client_id. Register the client first.")
    if not client.allows_redirect(redirect_uri):
        return HttpResponseBadRequest("redirect_uri does not match this client's registration.")

    if response_type != "code":
        return _authorize_error(redirect_uri, state, "unsupported_response_type", "Only 'code' is supported.")
    # PKCE is not optional here. A public client has no secret, so without
    # a verifier an intercepted code could be redeemed by anyone.
    if challenge_method != "S256" or not challenge:
        return _authorize_error(
            redirect_uri,
            state,
            "invalid_request",
            "code_challenge with code_challenge_method=S256 is required.",
        )

    if request.method == "GET":
        return render(
            request,
            "mcp/oauth_consent.html",
            {
                "client": client,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": challenge_method,
                "response_type": response_type,
            },
        )

    # POST — the user pressed Approve.
    plain_code = secrets.token_urlsafe(32)
    OAuthAuthorizationCode.objects.create(
        code_hash=_hash(plain_code),
        client=client,
        user=request.user,
        redirect_uri=redirect_uri,
        code_challenge=challenge,
        expires_at=timezone.now() + datetime.timedelta(seconds=OAuthAuthorizationCode.LIFETIME_SECONDS),
    )
    out = {"code": plain_code}
    if state:
        out["state"] = state
    joiner = "&" if "?" in redirect_uri else "?"
    return redirect(f"{redirect_uri}{joiner}{urlencode(out)}")


@require_http_methods(["POST"])
def switch_account(request: HttpRequest) -> HttpResponse:
    """Sign the visitor out and re-enter the same authorize request.

    A client opens the consent screen in the default browser, so without
    this the grant is bound to whichever account already holds a session
    there — and the screen names that account but cannot change it. This
    carries the authorize parameters through a logout, so the login page
    returns the visitor to the same consent screen as somebody else.

    The parameters are echoed into a URL on this host only, and
    ``authorize`` re-validates every one of them from scratch, so none of
    them is trusted on the way through.

    Args:
        request: POST carrying the authorize request's parameters.

    Returns:
        A redirect to ``authorize``, which sends the now-anonymous
        visitor on to the login page.
    """
    params = {
        key: value
        for key in (
            "client_id",
            "redirect_uri",
            "state",
            "code_challenge",
            "code_challenge_method",
            "response_type",
        )
        if (value := (request.POST.get(key) or "").strip())
    }
    logout(request)
    return redirect(f"{reverse('mcp:oauth_authorize')}?{urlencode(params)}")


def _token_error(code: str, description: str, status: int = 400) -> JsonResponse:
    """Return an OAuth token-endpoint error document.

    Args:
        code: OAuth error code, e.g. ``invalid_grant``.
        description: Human-readable detail.
        status: HTTP status to use.

    Returns:
        The error as JSON.
    """
    return JsonResponse({"error": code, "error_description": description}, status=status)


def _issue_access_token(*, user, client: OAuthClient) -> tuple[ApiToken, str]:
    """Mint an expiring access token owned by ``user``.

    Stored as an ordinary :class:`ApiToken` so the user sees and revokes
    it in the same settings list as the ones they pasted by hand — one
    place to answer "what has access to my account".

    Args:
        user: The account the token acts as.
        client: The client the token was issued to; names the row.

    Returns:
        ``(token_row, plain_secret)``.
    """
    token, plain = ApiToken.generate(user=user, name=client.client_name[:80])
    token.expires_at = timezone.now() + ACCESS_TOKEN_LIFETIME
    token.save(update_fields=["expires_at"])
    return token, plain


@csrf_exempt
@require_http_methods(["POST"])
def token(request: HttpRequest) -> JsonResponse:
    """Exchange an authorization code or a refresh token for an access token.

    CSRF-exempt because this is a machine-to-machine endpoint reached
    without a browser session; the credential in the body is the
    authentication.

    Args:
        request: POST with form-encoded OAuth parameters.

    Returns:
        ``200`` with the token document, or an OAuth error.
    """
    grant_type = (request.POST.get("grant_type") or "").strip()
    if grant_type == "authorization_code":
        return _exchange_code(request)
    if grant_type == "refresh_token":
        return _exchange_refresh(request)
    return _token_error("unsupported_grant_type", "Expected 'authorization_code' or 'refresh_token'.")


@transaction.atomic
def _exchange_code(request: HttpRequest) -> JsonResponse:
    """Redeem a one-time authorization code.

    The code row is locked for the duration so two simultaneous
    redemptions of the same code cannot both find it unconsumed — the
    replay window is closed by the database, not by hope.

    Args:
        request: The token request.

    Returns:
        The token document, or an OAuth error.
    """
    code = (request.POST.get("code") or "").strip()
    verifier = (request.POST.get("code_verifier") or "").strip()
    client_id = (request.POST.get("client_id") or "").strip()
    redirect_uri = (request.POST.get("redirect_uri") or "").strip()

    row = (
        OAuthAuthorizationCode.objects.select_for_update()
        .select_related("client", "user")
        .filter(code_hash=_hash(code))
        .first()
    )
    if row is None:
        return _token_error("invalid_grant", "Unknown authorization code.")
    if not row.is_usable:
        return _token_error("invalid_grant", "Authorization code has expired or was already used.")
    if row.client.client_id != client_id:
        return _token_error("invalid_grant", "Code was issued to a different client.")
    if row.redirect_uri != redirect_uri:
        return _token_error("invalid_grant", "redirect_uri does not match the authorize request.")
    if not verifier or not row.verify_pkce(verifier):
        return _token_error("invalid_grant", "PKCE verification failed.")

    row.consumed_at = timezone.now()
    row.save(update_fields=["consumed_at"])

    access, plain_access = _issue_access_token(user=row.user, client=row.client)
    plain_refresh = secrets.token_urlsafe(32)
    OAuthRefreshToken.objects.create(
        token_hash=_hash(plain_refresh),
        client=row.client,
        user=row.user,
        access_token=access,
    )
    row.client.last_used_at = timezone.now()
    row.client.save(update_fields=["last_used_at"])

    return JsonResponse(
        {
            "access_token": plain_access,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
            "refresh_token": plain_refresh,
            "scope": SCOPE,
        }
    )


@transaction.atomic
def _exchange_refresh(request: HttpRequest) -> JsonResponse:
    """Rotate a refresh token and issue a fresh access token.

    Rotation is the point: the presented refresh token is retired and a
    new one returned, so a copy stolen from disk stops working the moment
    the legitimate client next refreshes.

    Args:
        request: The token request.

    Returns:
        The token document, or an OAuth error.
    """
    presented = (request.POST.get("refresh_token") or "").strip()
    client_id = (request.POST.get("client_id") or "").strip()

    row = (
        OAuthRefreshToken.objects.select_for_update()
        .select_related("client", "user", "access_token")
        .filter(token_hash=_hash(presented))
        .first()
    )
    if row is None:
        return _token_error("invalid_grant", "Unknown refresh token.")
    if not row.is_usable:
        return _token_error("invalid_grant", "Refresh token was rotated or revoked.")
    if row.client.client_id != client_id:
        return _token_error("invalid_grant", "Refresh token was issued to a different client.")
    if not row.user.is_active:
        return _token_error("invalid_grant", "User account is inactive.", status=403)
    # Revoking the access token in settings is how a user says "stop" —
    # honour it here too, or the refresh token would quietly mint a
    # replacement for the credential they just killed.
    if row.access_token.revoked_at is not None:
        return _token_error("invalid_grant", "This grant was revoked.", status=403)

    # Rotate the secret on the SAME row rather than minting another: one
    # grant should be one revocable line in the user's settings, not a new
    # "Claude Desktop" entry every hour. Rotating retires the old secret
    # for free — the stored hash stops matching it.
    plain_access = row.access_token.rotate_secret(lifetime=ACCESS_TOKEN_LIFETIME)
    plain_refresh = secrets.token_urlsafe(32)
    OAuthRefreshToken.objects.create(
        token_hash=_hash(plain_refresh),
        client=row.client,
        user=row.user,
        access_token=row.access_token,
    )
    row.replaced_at = timezone.now()
    row.save(update_fields=["replaced_at"])

    return JsonResponse(
        {
            "access_token": plain_access,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
            "refresh_token": plain_refresh,
            "scope": SCOPE,
        }
    )
