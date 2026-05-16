"""``set_language`` view — language switch + open-redirect defence."""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory


@pytest.mark.django_db
class TestSetLanguageOpenRedirect:
    """The view redirects back to ``Referer`` but ONLY when it points
    at the same host. Without that check it would be a free open
    redirect: a crafted ``Referer`` would bounce the user off-site.
    """

    def test_off_site_referer_falls_back_to_dashboard(self, client):
        user = UserFactory()
        client.force_login(user)
        resp = client.post(
            reverse("accounts:set_language"),
            {"language": "uk"},
            HTTP_REFERER="https://attacker.example/",
        )
        assert resp.status_code == 302
        assert resp["Location"] == reverse("web:dashboard")

    def test_same_origin_referer_is_kept(self, client):
        user = UserFactory()
        client.force_login(user)
        same_origin = "http://testserver/projects/"
        resp = client.post(
            reverse("accounts:set_language"),
            {"language": "uk"},
            HTTP_REFERER=same_origin,
        )
        assert resp.status_code == 302
        assert resp["Location"] == same_origin

    def test_missing_referer_falls_back_to_dashboard(self, client):
        user = UserFactory()
        client.force_login(user)
        resp = client.post(reverse("accounts:set_language"), {"language": "uk"})
        assert resp.status_code == 302
        assert resp["Location"] == reverse("web:dashboard")

    def test_unknown_language_rejected(self, client):
        user = UserFactory()
        client.force_login(user)
        resp = client.post(reverse("accounts:set_language"), {"language": "xx"})
        assert resp.status_code == 400

    def test_user_language_is_persisted(self, client):
        user = UserFactory()
        client.force_login(user)
        client.post(reverse("accounts:set_language"), {"language": "uk"})
        user.refresh_from_db()
        assert user.language == "uk"
