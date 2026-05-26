"""Tests for setting / changing the password at ``/accounts/settings/password/``.

The form lives in a modal (loaded via ``GET``); the modal submits over HTMX
(204 + ``acta:password-changed`` trigger on success, re-render with inline
errors on failure). A non-HTMX ``POST`` falls back to a redirect with flash
messages.
"""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory

CHANGE_URL = reverse("accounts:change_password")
SETTINGS_URL = reverse("accounts:settings")
OLD = "0ldP@ssw0rd!x"
NEW = "Sup3rSecret!42"


def _oauth_user():
    # allauth gives social signups an unusable password; mirror that.
    user = UserFactory()
    user.set_unusable_password()
    user.save()
    return user


def _user_with_password():
    user = UserFactory()
    user.set_password(OLD)
    user.save()
    return user


@pytest.mark.django_db
class TestSetPassword:
    """Accounts with no usable password (e.g. Google OAuth) can set one."""

    def test_oauth_account_starts_without_usable_password(self):
        assert _oauth_user().has_usable_password() is False

    def test_set_password_succeeds(self, client):
        user = _oauth_user()
        client.force_login(user)
        resp = client.post(CHANGE_URL, {"new_password1": NEW, "new_password2": NEW})
        assert resp.status_code == 302
        user.refresh_from_db()
        assert user.has_usable_password() is True
        assert user.check_password(NEW)

    def test_mismatched_confirmation_rejected(self, client):
        user = _oauth_user()
        client.force_login(user)
        resp = client.post(CHANGE_URL, {"new_password1": NEW, "new_password2": NEW + "x"})
        assert resp.status_code == 302
        user.refresh_from_db()
        assert user.has_usable_password() is False


@pytest.mark.django_db
class TestChangePassword:
    """Accounts that already have a password must supply the current one."""

    def test_change_succeeds_with_correct_current(self, client):
        user = _user_with_password()
        client.force_login(user)
        resp = client.post(CHANGE_URL, {"old_password": OLD, "new_password1": NEW, "new_password2": NEW})
        assert resp.status_code == 302
        user.refresh_from_db()
        assert user.check_password(NEW)

    def test_wrong_current_password_rejected(self, client):
        user = _user_with_password()
        client.force_login(user)
        resp = client.post(CHANGE_URL, {"old_password": "wrong-one", "new_password1": NEW, "new_password2": NEW})
        assert resp.status_code == 302
        user.refresh_from_db()
        assert user.check_password(OLD)  # unchanged

    def test_session_survives_change(self, client):
        """``update_session_auth_hash`` keeps the user logged in after a change."""
        user = _user_with_password()
        client.force_login(user)
        client.post(CHANGE_URL, {"old_password": OLD, "new_password1": NEW, "new_password2": NEW})
        assert client.get(SETTINGS_URL).status_code == 200


@pytest.mark.django_db
class TestChangePasswordHtmx:
    """The modal submits over HTMX: 204 + trigger on success, re-render on error."""

    def test_htmx_success_returns_204_and_close_trigger(self, client):
        user = _user_with_password()
        client.force_login(user)
        resp = client.post(
            CHANGE_URL,
            {"old_password": OLD, "new_password1": NEW, "new_password2": NEW},
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 204
        assert "acta:password-changed" in resp["HX-Trigger"]
        user.refresh_from_db()
        assert user.check_password(NEW)

    def test_htmx_error_rerenders_modal(self, client):
        user = _user_with_password()
        client.force_login(user)
        resp = client.post(
            CHANGE_URL,
            {"old_password": "wrong-one", "new_password1": NEW, "new_password2": NEW},
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 200
        assert 'name="new_password1"' in resp.content.decode()  # modal re-rendered
        user.refresh_from_db()
        assert user.check_password(OLD)  # unchanged


@pytest.mark.django_db
class TestPasswordModal:
    """``GET`` renders the modal with the variant matching the account."""

    def test_get_renders_change_variant(self, client):
        user = _user_with_password()
        client.force_login(user)
        body = client.get(CHANGE_URL).content.decode()
        assert "Change password" in body
        assert 'name="old_password"' in body  # current-password field present

    def test_get_renders_set_variant_for_oauth(self, client):
        user = _oauth_user()
        client.force_login(user)
        body = client.get(CHANGE_URL).content.decode()
        assert "Set password" in body
        assert 'name="old_password"' not in body  # no current-password field
        assert 'name="new_password1"' in body


@pytest.mark.django_db
class TestSettingsPasswordButton:
    """The Settings page shows a button that opens the modal — not the form."""

    def test_password_account_sees_change_button(self, client):
        user = _user_with_password()
        client.force_login(user)
        body = client.get(SETTINGS_URL).content.decode()
        assert "Change password" in body
        assert CHANGE_URL in body  # the button hx-gets the modal
        assert 'name="old_password"' not in body  # form is in the modal, not the page

    def test_oauth_account_sees_set_button(self, client):
        user = _oauth_user()
        client.force_login(user)
        body = client.get(SETTINGS_URL).content.decode()
        assert "Set password" in body
        assert CHANGE_URL in body


@pytest.mark.django_db
class TestChangePasswordAccess:

    def test_anonymous_redirected(self, client):
        resp = client.get(CHANGE_URL)
        assert resp.status_code in (301, 302)
        assert "/accounts/login/" in resp.url
