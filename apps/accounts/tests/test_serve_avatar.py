"""Tests for the avatar-serving view's graceful handling of missing files."""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory


@pytest.mark.django_db
class TestServeAvatar:
    def test_404_when_no_avatar_set(self, client):
        user = UserFactory()
        client.force_login(user)
        resp = client.get(reverse("accounts:serve_avatar", kwargs={"user_id": user.id}))
        assert resp.status_code == 404

    def test_404_when_avatar_file_missing(self, client):
        # The DB row references an avatar whose file is gone from storage
        # (e.g. the media volume was reset). The view must degrade to 404 so
        # the UI shows its initials fallback — not a hard 500 on every avatar.
        user = UserFactory()
        user.avatar = "avatars/does-not-exist.jpg"
        user.save(update_fields=["avatar"])
        client.force_login(user)
        resp = client.get(reverse("accounts:serve_avatar", kwargs={"user_id": user.id}))
        assert resp.status_code == 404
