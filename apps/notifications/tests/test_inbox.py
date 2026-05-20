"""Inbox page + read/unread/archive/bulk endpoints."""

from django.urls import reverse

import pytest

from apps.notifications.models import Notification
from apps.workspaces.tests.factories import WorkspaceFactory

from .factories import NotificationFactory


@pytest.fixture
def user_ws(db):
    """A workspace and its owner (a member, so the chrome renders)."""
    ws = WorkspaceFactory()
    return ws.owner, ws


@pytest.mark.django_db
class TestInboxPage:
    def test_renders_own_notifications(self, client, user_ws):
        user, ws = user_ws
        NotificationFactory(recipient=user, workspace=ws, preview="hello inbox")
        client.force_login(user)
        resp = client.get(reverse("web:inbox"))
        assert resp.status_code == 200
        assert "hello inbox" in resp.content.decode()

    def test_excludes_other_users_notifications(self, client, user_ws):
        user, ws = user_ws
        other = WorkspaceFactory().owner
        NotificationFactory(recipient=other, workspace=ws, preview="not yours")
        client.force_login(user)
        resp = client.get(reverse("web:inbox"))
        assert "not yours" not in resp.content.decode()

    def test_unread_filter(self, client, user_ws):
        user, ws = user_ws
        NotificationFactory(recipient=user, workspace=ws, is_read=False, preview="freshping")
        NotificationFactory(recipient=user, workspace=ws, is_read=True, preview="alreadyseen")
        client.force_login(user)
        resp = client.get(reverse("web:inbox"), {"filter": "unread"})
        body = resp.content.decode()
        assert "freshping" in body
        assert "alreadyseen" not in body

    def test_archived_excluded(self, client, user_ws):
        user, ws = user_ws
        from django.utils import timezone

        NotificationFactory(recipient=user, workspace=ws, preview="archived one", archived_at=timezone.now())
        client.force_login(user)
        resp = client.get(reverse("web:inbox"))
        assert "archived one" not in resp.content.decode()

    def test_unread_count_in_context(self, client, user_ws):
        user, ws = user_ws
        NotificationFactory.create_batch(3, recipient=user, workspace=ws, is_read=False)
        client.force_login(user)
        resp = client.get(reverse("web:inbox"))
        assert resp.context["inbox_unread"] == 3


@pytest.mark.django_db
class TestInboxEndpoints:
    def test_open_marks_read(self, client, user_ws):
        user, ws = user_ws
        n = NotificationFactory(recipient=user, workspace=ws, is_read=False)
        client.force_login(user)
        resp = client.post(reverse("web:notification_open", args=[n.pk]))
        assert resp.status_code == 200
        n.refresh_from_db()
        assert n.is_read is True

    def test_toggle_read_then_unread(self, client, user_ws):
        user, ws = user_ws
        n = NotificationFactory(recipient=user, workspace=ws, is_read=False)
        client.force_login(user)
        client.post(reverse("web:notification_read", args=[n.pk]), {"read": "1"})
        n.refresh_from_db()
        assert n.is_read is True
        client.post(reverse("web:notification_read", args=[n.pk]), {"read": "0"})
        n.refresh_from_db()
        assert n.is_read is False

    def test_archive(self, client, user_ws):
        user, ws = user_ws
        n = NotificationFactory(recipient=user, workspace=ws)
        client.force_login(user)
        resp = client.post(reverse("web:notification_archive", args=[n.pk]))
        assert resp.status_code == 200
        n.refresh_from_db()
        assert n.archived_at is not None

    def test_cannot_touch_foreign_notification(self, client, user_ws):
        user, ws = user_ws
        other = WorkspaceFactory().owner
        n = NotificationFactory(recipient=other, workspace=ws)
        client.force_login(user)
        resp = client.post(reverse("web:notification_archive", args=[n.pk]))
        assert resp.status_code == 404

    def test_bulk_read(self, client, user_ws):
        user, ws = user_ws
        ids = [NotificationFactory(recipient=user, workspace=ws, is_read=False).pk for _ in range(3)]
        client.force_login(user)
        resp = client.post(reverse("web:notifications_bulk"), {"action": "read", "ids": ids})
        assert resp.status_code == 200
        assert Notification.objects.filter(pk__in=ids, is_read=True).count() == 3

    def test_bulk_archive(self, client, user_ws):
        user, ws = user_ws
        ids = [NotificationFactory(recipient=user, workspace=ws).pk for _ in range(2)]
        client.force_login(user)
        client.post(reverse("web:notifications_bulk"), {"action": "archive", "ids": ids})
        assert Notification.objects.filter(pk__in=ids, archived_at__isnull=False).count() == 2

    def test_read_all(self, client, user_ws):
        user, ws = user_ws
        NotificationFactory.create_batch(4, recipient=user, workspace=ws, is_read=False)
        client.force_login(user)
        client.post(reverse("web:notifications_read_all"))
        assert Notification.objects.filter(recipient=user, is_read=False).count() == 0
