"""The OAuth 2.1 flow that lets Claude Desktop connect by URL alone.

These cover the security surface rather than the happy path alone: a
public client with no secret means PKCE, one-shot codes and exact
redirect matching are the *only* things standing between a stolen code
and someone's account.
"""

import base64
import datetime
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

from django.urls import reverse
from django.utils import timezone

import pytest

from apps.accounts.models import ApiToken
from apps.accounts.tests.factories import UserFactory
from apps.mcp.models import OAuthAuthorizationCode, OAuthClient, OAuthRefreshToken, _hash

REGISTER_URL = "/mcp/oauth/register/"
AUTHORIZE_URL = "/mcp/oauth/authorize/"
TOKEN_URL = "/mcp/oauth/token/"
SWITCH_URL = "/mcp/oauth/switch-account/"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _pkce() -> tuple[str, str]:
    """Return a ``(verifier, S256 challenge)`` pair."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _register(client, name="Claude Desktop", uris=(REDIRECT,)):
    """Register a client through the endpoint and return its ``client_id``."""
    resp = client.post(
        REGISTER_URL,
        data=json.dumps({"client_name": name, "redirect_uris": list(uris)}),
        content_type="application/json",
    )
    assert resp.status_code == 201
    return resp.json()["client_id"]


def _authorize(client, client_id, challenge, state="xyz", redirect_uri=REDIRECT):
    """Approve the consent screen and return the issued code."""
    resp = client.post(
        AUTHORIZE_URL,
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "response_type": "code",
        },
    )
    assert resp.status_code == 302
    return parse_qs(urlparse(resp["Location"]).query)


@pytest.mark.django_db
class TestDiscovery:
    """A client that knows only the endpoint URL must find everything else."""

    def test_unauthenticated_mcp_call_points_at_the_metadata(self, client):
        resp = client.post("/mcp/", data="{}", content_type="application/json")
        assert resp.status_code == 401
        assert "oauth-protected-resource" in resp["WWW-Authenticate"]

    def test_protected_resource_metadata(self, client):
        body = client.get("/.well-known/oauth-protected-resource").json()
        assert body["resource"].endswith("/mcp/")
        # Absolute, and derived from the request — the same code has to
        # serve localhost and the deployed domain.
        assert body["authorization_servers"][0].startswith("http")
        assert body["bearer_methods_supported"] == ["header"]

    def test_path_suffixed_spelling_also_answers(self, client):
        """Some clients append the resource path to the well-known URI."""
        assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200

    def test_authorization_server_metadata_names_real_endpoints(self, client):
        body = client.get("/.well-known/oauth-authorization-server").json()
        assert body["authorization_endpoint"].endswith(reverse("mcp:oauth_authorize"))
        assert body["token_endpoint"].endswith(reverse("mcp:oauth_token"))
        assert body["registration_endpoint"].endswith(reverse("mcp:oauth_register"))
        # Public clients + mandatory PKCE is the whole security posture.
        assert body["code_challenge_methods_supported"] == ["S256"]
        assert body["token_endpoint_auth_methods_supported"] == ["none"]


@pytest.mark.django_db
class TestRegistration:
    def test_registers_and_issues_a_client_id(self, client):
        resp = client.post(
            REGISTER_URL,
            data=json.dumps({"client_name": "Claude Desktop", "redirect_uris": [REDIRECT]}),
            content_type="application/json",
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["client_id"]
        assert body["token_endpoint_auth_method"] == "none"
        assert OAuthClient.objects.filter(client_id=body["client_id"]).exists()

    def test_no_secret_is_ever_issued(self, client):
        """A desktop app cannot keep a secret, so we must not pretend it can."""
        body = client.post(
            REGISTER_URL,
            data=json.dumps({"client_name": "x", "redirect_uris": [REDIRECT]}),
            content_type="application/json",
        ).json()
        assert "client_secret" not in body

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"redirect_uris": []},
            {"redirect_uris": "not-a-list"},
            {"redirect_uris": [""]},
            {"redirect_uris": [123]},
        ],
    )
    def test_unusable_redirect_uris_are_refused(self, client, payload):
        resp = client.post(REGISTER_URL, data=json.dumps(payload), content_type="application/json")
        assert resp.status_code == 400

    def test_non_json_body_is_refused(self, client):
        resp = client.post(REGISTER_URL, data="not json", content_type="application/json")
        assert resp.status_code == 400


@pytest.mark.django_db
class TestAuthorize:
    def test_anonymous_visitor_is_sent_to_login(self, client):
        cid = _register(client)
        resp = client.get(AUTHORIZE_URL, {"client_id": cid, "redirect_uri": REDIRECT})
        assert resp.status_code == 302
        assert "login" in resp["Location"]

    def test_unknown_client_is_refused_without_redirecting(self, client):
        """An error must never be bounced to an unverified URI — that is an
        open redirect."""
        client.force_login(UserFactory())
        resp = client.get(AUTHORIZE_URL, {"client_id": "nope", "redirect_uri": "https://evil.test/"})
        assert resp.status_code == 400

    def test_unregistered_redirect_uri_is_refused_without_redirecting(self, client):
        cid = _register(client)
        client.force_login(UserFactory())
        resp = client.get(AUTHORIZE_URL, {"client_id": cid, "redirect_uri": "https://evil.test/"})
        assert resp.status_code == 400

    def test_redirect_uri_is_matched_exactly_not_by_prefix(self, client):
        """``https://good.test/cb`` must not accept ``…/cb.evil.test``."""
        cid = _register(client, uris=["https://good.test/cb"])
        client.force_login(UserFactory())
        resp = client.get(AUTHORIZE_URL, {"client_id": cid, "redirect_uri": "https://good.test/cb.evil.test"})
        assert resp.status_code == 400

    def test_missing_pkce_challenge_is_refused(self, client):
        cid = _register(client)
        client.force_login(UserFactory())
        resp = client.get(AUTHORIZE_URL, {"client_id": cid, "redirect_uri": REDIRECT, "state": "s"})
        assert resp.status_code == 302
        assert "error=invalid_request" in resp["Location"]

    def test_plain_pkce_method_is_refused(self, client):
        """Only S256. ``plain`` puts the verifier on the wire in the clear."""
        cid = _register(client)
        client.force_login(UserFactory())
        resp = client.get(
            AUTHORIZE_URL,
            {
                "client_id": cid,
                "redirect_uri": REDIRECT,
                "code_challenge": "abc",
                "code_challenge_method": "plain",
            },
        )
        assert "error=invalid_request" in resp["Location"]

    def test_consent_screen_names_the_client(self, client):
        cid = _register(client, name="Claude Desktop")
        client.force_login(UserFactory())
        _, challenge = _pkce()
        resp = client.get(
            AUTHORIZE_URL,
            {
                "client_id": cid,
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Claude Desktop" in body
        # The name is the client's own claim, so the copy must hedge.
        assert "says it is" in body

    def test_approval_issues_a_code_and_echoes_state(self, client):
        cid = _register(client)
        user = UserFactory()
        client.force_login(user)
        _, challenge = _pkce()
        params = _authorize(client, cid, challenge, state="opaque-123")
        assert params["state"] == ["opaque-123"]
        row = OAuthAuthorizationCode.objects.get(code_hash=_hash(params["code"][0]))
        assert row.user == user
        assert row.is_usable

    def test_code_is_stored_hashed(self, client):
        cid = _register(client)
        client.force_login(UserFactory())
        _, challenge = _pkce()
        params = _authorize(client, cid, challenge)
        plain = params["code"][0]
        assert not OAuthAuthorizationCode.objects.filter(code_hash=plain).exists()


@pytest.mark.django_db
class TestAccountSwitch:
    """Consent binds to the browser's session, so it must be swappable.

    A client opens this page in the default browser, where some other
    account of the same person is usually already signed in. Naming that
    account is half the fix; being able to change it without abandoning
    the flow is the other half.
    """

    def _params(self, client_id, challenge):
        """Return a complete set of authorize parameters."""
        return {
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "state": "opaque-123",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "response_type": "code",
        }

    def test_consent_screen_names_the_signed_in_account(self, client):
        cid = _register(client)
        user = UserFactory(first_name="Kateryna", last_name="Levashova")
        client.force_login(user)
        _, challenge = _pkce()
        resp = client.get(AUTHORIZE_URL, self._params(cid, challenge))
        body = resp.content.decode()
        assert "Kateryna Levashova" in body
        assert user.email in body

    def test_switch_signs_out_and_returns_to_the_same_request(self, client):
        cid = _register(client)
        client.force_login(UserFactory())
        _, challenge = _pkce()
        resp = client.post(SWITCH_URL, self._params(cid, challenge))
        assert resp.status_code == 302
        assert "_auth_user_id" not in client.session
        assert resp["Location"].startswith(AUTHORIZE_URL)
        query = parse_qs(urlparse(resp["Location"]).query)
        assert query["client_id"] == [cid]
        assert query["state"] == ["opaque-123"]
        assert query["code_challenge"] == [challenge]
        assert query["redirect_uri"] == [REDIRECT]

    def test_the_grant_follows_the_second_account(self, client):
        cid = _register(client)
        first, second = UserFactory(), UserFactory()
        client.force_login(first)
        _, challenge = _pkce()
        client.post(SWITCH_URL, self._params(cid, challenge))
        client.force_login(second)
        params = _authorize(client, cid, challenge, state="opaque-123")
        row = OAuthAuthorizationCode.objects.get(code_hash=_hash(params["code"][0]))
        assert row.user == second

    def test_switch_never_redirects_off_site(self, client):
        client.force_login(UserFactory())
        resp = client.post(SWITCH_URL, {"client_id": "x", "redirect_uri": "https://evil.test/"})
        assert resp["Location"].startswith(AUTHORIZE_URL)

    def test_switch_refuses_get(self, client):
        client.force_login(UserFactory())
        assert client.get(SWITCH_URL).status_code == 405


@pytest.mark.django_db
class TestTokenExchange:
    def _granted(self, client):
        """Walk register → authorize and return ``(client_id, code, verifier, user)``."""
        cid = _register(client)
        user = UserFactory()
        client.force_login(user)
        verifier, challenge = _pkce()
        params = _authorize(client, cid, challenge)
        client.logout()
        return cid, params["code"][0], verifier, user

    def test_code_exchanges_for_an_expiring_access_token(self, client):
        cid, code, verifier, user = self._granted(client)
        resp = client.post(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": cid,
                "redirect_uri": REDIRECT,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == 3600
        assert body["refresh_token"]
        issued = ApiToken.objects.get(token_hash=ApiToken.hash_secret(body["access_token"]))
        assert issued.user == user
        assert issued.expires_at is not None

    def test_issued_token_works_on_the_mcp_endpoint(self, client):
        """The whole point: a Bearer token from this flow is a real credential."""
        cid, code, verifier, _ = self._granted(client)
        access = client.post(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": cid,
                "redirect_uri": REDIRECT,
            },
        ).json()["access_token"]
        resp = client.post(
            "/mcp/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        assert resp.status_code == 200
        assert resp.json()["result"]["tools"]

    def test_wrong_verifier_is_refused(self, client):
        cid, code, _, _ = self._granted(client)
        resp = client.post(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": secrets.token_urlsafe(48),
                "client_id": cid,
                "redirect_uri": REDIRECT,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_grant"

    def test_code_cannot_be_replayed(self, client):
        cid, code, verifier, _ = self._granted(client)
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": cid,
            "redirect_uri": REDIRECT,
        }
        assert client.post(TOKEN_URL, payload).status_code == 200
        second = client.post(TOKEN_URL, payload)
        assert second.status_code == 400
        assert "already used" in second.json()["error_description"]

    def test_expired_code_is_refused(self, client):
        cid, code, verifier, _ = self._granted(client)
        row = OAuthAuthorizationCode.objects.get(code_hash=_hash(code))
        row.expires_at = timezone.now() - datetime.timedelta(seconds=1)
        row.save(update_fields=["expires_at"])
        resp = client.post(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": cid,
                "redirect_uri": REDIRECT,
            },
        )
        assert resp.status_code == 400

    def test_code_cannot_be_redeemed_by_another_client(self, client):
        cid, code, verifier, _ = self._granted(client)
        other = _register(client, name="Impostor")
        resp = client.post(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": other,
                "redirect_uri": REDIRECT,
            },
        )
        assert resp.status_code == 400

    def test_redirect_uri_must_match_the_authorize_request(self, client):
        cid, code, verifier, _ = self._granted(client)
        resp = client.post(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": cid,
                "redirect_uri": "https://claude.ai/other",
            },
        )
        assert resp.status_code == 400

    def test_unknown_grant_type_is_refused(self, client):
        resp = client.post(TOKEN_URL, {"grant_type": "password", "username": "x", "password": "y"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "unsupported_grant_type"


@pytest.mark.django_db
class TestRefresh:
    def _tokens(self, client):
        """Return the first token document plus its client id."""
        cid = _register(client)
        user = UserFactory()
        client.force_login(user)
        verifier, challenge = _pkce()
        params = _authorize(client, cid, challenge)
        client.logout()
        body = client.post(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": params["code"][0],
                "code_verifier": verifier,
                "client_id": cid,
                "redirect_uri": REDIRECT,
            },
        ).json()
        return cid, body, user

    def test_refresh_issues_a_new_pair(self, client):
        cid, first, _ = self._tokens(client)
        resp = client.post(
            TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": cid}
        )
        assert resp.status_code == 200
        second = resp.json()
        assert second["access_token"] != first["access_token"]
        assert second["refresh_token"] != first["refresh_token"]

    def test_used_refresh_token_is_retired(self, client):
        """Rotation: a copy stolen from disk dies at the next legitimate refresh."""
        cid, first, _ = self._tokens(client)
        client.post(
            TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": cid}
        )
        replay = client.post(
            TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": cid}
        )
        assert replay.status_code == 400
        assert "rotated" in replay.json()["error_description"]

    def test_superseded_access_token_stops_working(self, client):
        """Otherwise every refresh would grow the set of live credentials."""
        cid, first, _ = self._tokens(client)
        client.post(
            TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": cid}
        )
        resp = client.post(
            "/mcp/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {first['access_token']}",
        )
        assert resp.status_code == 401

    def test_revoking_the_access_token_kills_the_grant(self, client):
        """Revoking in settings must mean revoked — not 'until it refreshes'."""
        cid, first, _ = self._tokens(client)
        issued = ApiToken.objects.get(token_hash=ApiToken.hash_secret(first["access_token"]))
        issued.revoked_at = timezone.now()
        issued.save(update_fields=["revoked_at"])
        resp = client.post(
            TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": cid}
        )
        assert resp.status_code == 403

    def test_refresh_token_is_bound_to_its_client(self, client):
        cid, first, _ = self._tokens(client)
        other = _register(client, name="Impostor")
        resp = client.post(
            TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": other}
        )
        assert resp.status_code == 400

    def test_inactive_user_cannot_refresh(self, client):
        cid, first, user = self._tokens(client)
        user.is_active = False
        user.save(update_fields=["is_active"])
        resp = client.post(
            TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": cid}
        )
        assert resp.status_code == 403

    def test_refresh_token_is_stored_hashed(self, client):
        _, first, _ = self._tokens(client)
        assert not OAuthRefreshToken.objects.filter(token_hash=first["refresh_token"]).exists()
        assert OAuthRefreshToken.objects.filter(token_hash=_hash(first["refresh_token"])).exists()
