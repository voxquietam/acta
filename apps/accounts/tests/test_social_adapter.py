"""Tests for Google/social login: invite-gated signup + email-match guard.

Covers ``NoSignupSocialAccountAdapter`` (the gate that decides whether a
Google login may create a new account), the shared ``claim_invite_for_user``
helper, and the ``google_login_available`` template tag that toggles the
"Continue with Google" button.
"""

from types import SimpleNamespace

from django.test import RequestFactory

import pytest

from apps.accounts.adapters import INVITE_SESSION_KEY, NoSignupSocialAccountAdapter, claim_invite_for_user
from apps.accounts.tests.factories import UserFactory
from apps.workspaces.models import WorkspaceInvite, WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


def _request(*, session_token=None):
    """Build a request with an optional invite token stashed in the session."""
    req = RequestFactory().get("/accounts/google/login/callback/")
    req.session = {INVITE_SESSION_KEY: session_token} if session_token else {}
    return req


def _sociallogin(email):
    """Minimal SocialLogin stand-in — the adapter only reads ``user.email``."""
    return SimpleNamespace(user=SimpleNamespace(email=email))


@pytest.mark.django_db
class TestSocialSignupGate:
    """``is_open_for_signup`` opens only with an invite whose email matches."""

    def test_closed_without_invite(self):
        adapter = NoSignupSocialAccountAdapter()
        assert adapter.is_open_for_signup(_request(), _sociallogin("anyone@team.test")) is False

    def test_open_with_matching_invite_email(self):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="newbie@team.test", role=WorkspaceMember.MEMBER)
        adapter = NoSignupSocialAccountAdapter()
        req = _request(session_token=invite.token)
        assert adapter.is_open_for_signup(req, _sociallogin("newbie@team.test")) is True

    def test_case_insensitive_email_match(self):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="MixedCase@team.test", role=WorkspaceMember.MEMBER)
        adapter = NoSignupSocialAccountAdapter()
        req = _request(session_token=invite.token)
        # ``generate`` lowercases the invite email; Google may report it mixed-case.
        assert adapter.is_open_for_signup(req, _sociallogin("MIXEDCASE@team.test")) is True

    def test_closed_with_mismatched_email(self):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="bound@team.test", role=WorkspaceMember.MEMBER)
        adapter = NoSignupSocialAccountAdapter()
        req = _request(session_token=invite.token)
        assert adapter.is_open_for_signup(req, _sociallogin("someone-else@gmail.com")) is False

    def test_closed_with_empty_social_email(self):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="bound@team.test", role=WorkspaceMember.MEMBER)
        adapter = NoSignupSocialAccountAdapter()
        req = _request(session_token=invite.token)
        assert adapter.is_open_for_signup(req, _sociallogin("")) is False


@pytest.mark.django_db
class TestClaimInviteHelper:
    """``claim_invite_for_user`` consumes the invite + grants membership."""

    def test_claims_invite_and_grants_membership(self):
        ws = WorkspaceFactory()
        invite = WorkspaceInvite.generate(workspace=ws, email="newbie@team.test", role=WorkspaceMember.ADMIN)
        user = UserFactory()
        req = _request(session_token=invite.token)

        claim_invite_for_user(req, user, invite)

        invite.refresh_from_db()
        assert invite.is_consumed
        membership = WorkspaceMember.objects.get(workspace=ws, user=user)
        assert membership.role == WorkspaceMember.ADMIN
        # Session marker cleared so a second concurrent tab can't double-consume.
        assert INVITE_SESSION_KEY not in req.session


@pytest.mark.django_db
class TestGoogleLoginAvailableTag:
    """The button only renders once a Google SocialApp is configured in admin."""

    def test_false_without_socialapp(self):
        from apps.accounts.templatetags.acta_auth import google_login_available

        assert google_login_available() is False

    def test_true_with_google_socialapp(self):
        from allauth.socialaccount.models import SocialApp

        from apps.accounts.templatetags.acta_auth import google_login_available

        SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="dummy-client-id",
            secret="dummy-secret",
        )
        assert google_login_available() is True
