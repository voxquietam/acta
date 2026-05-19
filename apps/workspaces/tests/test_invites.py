"""Tests for the workspace invite model + invite-based signup flow."""

import datetime

from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.accounts.adapters import INVITE_SESSION_KEY, resolve_invite_from_request
from apps.accounts.tests.factories import UserFactory
from apps.workspaces.models import WorkspaceInvite, WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestInviteModel:
    def test_generate_sets_token_email_role_expiry(self):
        ws = WorkspaceFactory()
        admin = UserFactory()
        invite = WorkspaceInvite.generate(
            workspace=ws,
            email=" New.User@Example.com ",
            role=WorkspaceMember.MEMBER,
            created_by=admin,
        )
        assert invite.workspace == ws
        # ``generate`` normalises email to lowercase + strips whitespace
        # so cAsE differences can't double-mint tokens for the same address.
        assert invite.email == "new.user@example.com"
        assert invite.role == WorkspaceMember.MEMBER
        assert invite.token and len(invite.token) >= 32
        assert invite.created_by == admin
        # ``expires_at`` defaults to ``created_at + 7 days``; allow 5s slack.
        gap = invite.expires_at - timezone.now()
        assert datetime.timedelta(days=6, hours=23) < gap < datetime.timedelta(days=7, seconds=5)

    def test_is_active_until_expired_or_consumed(self):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="x@x.x", role=WorkspaceMember.MEMBER)
        assert invite.is_active

        invite.accepted_at = timezone.now()
        assert invite.is_consumed
        assert not invite.is_active

        # Expired path
        invite.accepted_at = None
        invite.expires_at = timezone.now() - datetime.timedelta(seconds=1)
        assert invite.is_expired
        assert not invite.is_active

    def test_signup_url_points_at_landing_view(self):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="a@b.c", role=WorkspaceMember.MEMBER)
        assert invite.signup_url == reverse("accounts:invite_accept", args=[invite.token])


@pytest.mark.django_db
class TestInviteAcceptView:
    def test_valid_token_stashes_session_and_redirects_to_signup(self, client):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="a@b.c", role=WorkspaceMember.MEMBER)

        response = client.get(invite.signup_url)
        assert response.status_code == 302
        assert "/accounts/signup/" in response["Location"]
        assert f"invite={invite.token}" in response["Location"]
        assert client.session[INVITE_SESSION_KEY] == invite.token

    def test_unknown_token_falls_through_to_login(self, client):
        url = reverse("accounts:invite_accept", args=["definitely-not-a-real-token"])
        response = client.get(url, follow=False)
        assert response.status_code == 302
        assert "login" in response["Location"]

    def test_expired_token_falls_through_to_login(self, client):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="a@b.c", role=WorkspaceMember.MEMBER)
        invite.expires_at = timezone.now() - datetime.timedelta(seconds=1)
        invite.save(update_fields=["expires_at"])
        response = client.get(invite.signup_url, follow=False)
        assert response.status_code == 302
        assert "login" in response["Location"]
        # No session-stash for a stale token.
        assert INVITE_SESSION_KEY not in client.session

    def test_consumed_token_falls_through_to_login(self, client):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="a@b.c", role=WorkspaceMember.MEMBER)
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at"])
        response = client.get(invite.signup_url, follow=False)
        assert response.status_code == 302
        assert INVITE_SESSION_KEY not in client.session

    def test_already_authenticated_user_is_redirected_home(self, client):
        user = UserFactory()
        client.force_login(user)
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="a@b.c", role=WorkspaceMember.MEMBER)
        response = client.get(invite.signup_url, follow=False)
        assert response.status_code == 302
        # No invite consumed.
        invite.refresh_from_db()
        assert invite.accepted_at is None


@pytest.mark.django_db
class TestInviteResolverHelper:
    """``resolve_invite_from_request`` is the workhorse the adapter calls."""

    def _request(self, *, querystring=None, session_token=None):
        from django.test import RequestFactory

        rf = RequestFactory()
        url = "/accounts/signup/"
        if querystring:
            url += f"?invite={querystring}"
        req = rf.get(url)
        # Fake session shim — RequestFactory doesn't populate one.
        req.session = {INVITE_SESSION_KEY: session_token} if session_token else {}
        return req

    def test_returns_none_without_invite(self):
        assert resolve_invite_from_request(self._request()) is None

    def test_returns_invite_from_querystring(self):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="a@b.c", role=WorkspaceMember.MEMBER)
        result = resolve_invite_from_request(self._request(querystring=invite.token))
        assert result is not None and result.pk == invite.pk

    def test_returns_invite_from_session(self):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="a@b.c", role=WorkspaceMember.MEMBER)
        result = resolve_invite_from_request(self._request(session_token=invite.token))
        assert result is not None and result.pk == invite.pk

    def test_skips_expired_invite(self):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="a@b.c", role=WorkspaceMember.MEMBER)
        invite.expires_at = timezone.now() - datetime.timedelta(seconds=1)
        invite.save(update_fields=["expires_at"])
        assert resolve_invite_from_request(self._request(querystring=invite.token)) is None

    def test_skips_consumed_invite(self):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="a@b.c", role=WorkspaceMember.MEMBER)
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at"])
        assert resolve_invite_from_request(self._request(querystring=invite.token)) is None


@pytest.mark.django_db
class TestAdapterSignupGate:
    """``is_open_for_signup`` flips open only when an active invite is in flight."""

    def test_closed_without_invite(self, client):
        # Hit the signup form raw — allauth's "signup closed" page comes
        # back instead of the form.
        response = client.get(reverse("account_signup"))
        # Allauth's signup_closed template uses status 200 with no form;
        # easier to assert behaviour at the adapter level.
        from django.test import RequestFactory

        from apps.accounts.adapters import NoSignupAccountAdapter

        req = RequestFactory().get("/accounts/signup/")
        req.session = {}
        assert NoSignupAccountAdapter().is_open_for_signup(req) is False
        # Sanity: the GET succeeds either way (200 closed-page or form).
        assert response.status_code in (200, 302)

    def test_open_with_valid_invite_querystring(self):
        from django.test import RequestFactory

        from apps.accounts.adapters import NoSignupAccountAdapter

        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="a@b.c", role=WorkspaceMember.MEMBER)
        req = RequestFactory().get(f"/accounts/signup/?invite={invite.token}")
        req.session = {}
        assert NoSignupAccountAdapter().is_open_for_signup(req) is True


@pytest.mark.django_db
class TestSignupConsumesInvite:
    """End-to-end: clicking the invite link + submitting the form lands the user in the workspace."""

    def test_full_flow_creates_member_and_consumes_invite(self):
        ws = WorkspaceFactory()
        admin = UserFactory()
        invite = WorkspaceInvite.generate(
            workspace=ws,
            email="newbie@team.test",
            role=WorkspaceMember.ADMIN,
            created_by=admin,
        )

        client = Client()
        # 1. Recipient clicks the invite link.
        landing = client.get(invite.signup_url, follow=False)
        assert landing.status_code == 302

        # 2. They submit the signup form. allauth's default form fields:
        #    username (we use AUTH_USER_MODEL with username), email, password.
        signup_payload = {
            "username": "newbie",
            "email": invite.email,
            "password1": "verysecret-passw0rd",
            "password2": "verysecret-passw0rd",
        }
        # Pass ``?invite=`` in the action URL — the redirect target
        # landed there in step 1 and the adapter expects either query
        # or session anyway (session was set already).
        signup_url = f"{reverse('account_signup')}?invite={invite.token}"
        response = client.post(signup_url, signup_payload, follow=False)
        # Allauth redirects on successful signup (302). If the form
        # had errors it would 200 with the form re-rendered; bail with
        # the form body in the assertion message so the failure is debuggable.
        assert response.status_code == 302, getattr(response, "content", b"")[:500]

        # 3. Invite is consumed.
        invite.refresh_from_db()
        assert invite.is_consumed
        assert invite.accepted_at is not None

        # 4. New user is a WorkspaceMember with the invite's role.
        membership = WorkspaceMember.objects.get(workspace=ws, user__username="newbie")
        assert membership.role == WorkspaceMember.ADMIN
