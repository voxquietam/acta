"""Tests for the user-settings page at ``/accounts/settings/``."""

from django.conf import settings
from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory


@pytest.mark.django_db
class TestUserSettingsAccess:

    def test_anonymous_redirects_to_login(self, client):
        resp = client.get(reverse("accounts:settings"))
        assert resp.status_code in (302, 301)
        assert "/accounts/login/" in resp.url

    def test_authenticated_get_renders_form(self, client):
        user = UserFactory(first_name="Alice", last_name="Smith")
        client.force_login(user)
        resp = client.get(reverse("accounts:settings"))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Alice" in body
        assert "Smith" in body
        assert user.username in body


@pytest.mark.django_db
class TestUserSettingsPost:

    def test_updates_first_and_last_name(self, client):
        user = UserFactory(first_name="Old", last_name="Name")
        client.force_login(user)
        resp = client.post(
            reverse("accounts:settings"),
            {"first_name": "New", "last_name": "Person", "language": ""},
        )
        assert resp.status_code == 302
        user.refresh_from_db()
        assert user.first_name == "New"
        assert user.last_name == "Person"

    def test_updates_language_and_sets_cookie(self, client):
        user = UserFactory()
        client.force_login(user)
        valid_codes = [c for c, _ in settings.LANGUAGES]
        # Pick a language that's not currently selected.
        target = next(c for c in valid_codes if c != user.language)
        resp = client.post(
            reverse("accounts:settings"),
            {"first_name": user.first_name, "last_name": user.last_name, "language": target},
        )
        # Language change asks HTMX for a full reload so the persistent
        # shell (sidebar / topbar) re-renders in the new language.
        assert resp.status_code == 204
        assert resp["HX-Refresh"] == "true"
        user.refresh_from_db()
        assert user.language == target
        # Cookie is set on the response.
        assert settings.LANGUAGE_COOKIE_NAME in resp.cookies
        assert resp.cookies[settings.LANGUAGE_COOKIE_NAME].value == target

    def test_invalid_language_is_ignored(self, client):
        user = UserFactory(language="en")
        client.force_login(user)
        resp = client.post(
            reverse("accounts:settings"),
            {"first_name": "", "last_name": "", "language": "klingon"},
        )
        assert resp.status_code == 302
        user.refresh_from_db()
        # Stays whatever it was.
        assert user.language == "en"

    def test_empty_form_no_op_succeeds(self, client):
        """Submitting unchanged values doesn't crash and doesn't write."""
        user = UserFactory(first_name="Same", last_name="Same")
        client.force_login(user)
        resp = client.post(
            reverse("accounts:settings"),
            {"first_name": "Same", "last_name": "Same", "language": user.language},
        )
        assert resp.status_code == 302
        user.refresh_from_db()
        assert user.first_name == "Same"

    def test_name_overlong_is_truncated_to_150(self, client):
        """``first_name`` slicing keeps the model's max_length safe."""
        user = UserFactory()
        client.force_login(user)
        too_long = "A" * 300
        resp = client.post(
            reverse("accounts:settings"),
            {"first_name": too_long, "last_name": "", "language": ""},
        )
        assert resp.status_code == 302
        user.refresh_from_db()
        assert len(user.first_name) == 150
