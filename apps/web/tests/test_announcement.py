"""Broadcast announcement composer — permission gate + fan-out."""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import Notification
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.mark.django_db
class TestPostAnnouncement:
    def _url(self):
        return reverse("web:post_announcement")

    def test_owner_broadcasts_to_members(self, client):
        ws = WorkspaceFactory()
        member = WorkspaceMemberFactory(workspace=ws).user
        client.force_login(ws.owner)
        resp = client.post(self._url(), {"title": "Heads up", "body": "maintenance sat"})
        assert resp.status_code == 204
        assert "acta:announcement-sent" in resp["HX-Trigger"]
        assert Notification.objects.filter(recipient=member, kind=Notification.Kind.ANNOUNCEMENT).exists()
        # the sender keeps a copy too, pre-read
        own = Notification.objects.get(recipient=ws.owner, kind=Notification.Kind.ANNOUNCEMENT)
        assert own.is_read is True

    def test_admin_can_broadcast(self, client):
        ws = WorkspaceFactory()
        admin = UserFactory()
        WorkspaceMemberFactory(workspace=ws, user=admin, role=WorkspaceMember.ADMIN)
        client.force_login(admin)
        assert client.post(self._url(), {"title": "t", "body": "b"}).status_code == 204

    def test_member_blocked_by_default(self, client):
        ws = WorkspaceFactory()
        member = UserFactory()
        WorkspaceMemberFactory(workspace=ws, user=member, role=WorkspaceMember.MEMBER)
        client.force_login(member)
        resp = client.post(self._url(), {"title": "t", "body": "b"})
        assert resp.status_code == 403
        assert not Notification.objects.filter(kind=Notification.Kind.ANNOUNCEMENT).exists()

    def test_member_allowed_when_workspace_opts_in(self, client):
        ws = WorkspaceFactory(allow_member_announcements=True)
        member = UserFactory()
        WorkspaceMemberFactory(workspace=ws, user=member, role=WorkspaceMember.MEMBER)
        client.force_login(member)
        resp = client.post(self._url(), {"title": "t", "body": "b"})
        assert resp.status_code == 204
        assert Notification.objects.filter(recipient=ws.owner, kind=Notification.Kind.ANNOUNCEMENT).exists()

    def test_empty_title_or_body_rejected(self, client):
        ws = WorkspaceFactory()
        client.force_login(ws.owner)
        assert client.post(self._url(), {"title": "", "body": "b"}).status_code == 400
        assert client.post(self._url(), {"title": "t", "body": "   "}).status_code == 400
        assert not Notification.objects.filter(kind=Notification.Kind.ANNOUNCEMENT).exists()

    def test_get_not_allowed(self, client):
        ws = WorkspaceFactory()
        client.force_login(ws.owner)
        assert client.get(self._url()).status_code == 405

    def test_announcements_inbox_filter(self, client):
        ws = WorkspaceFactory()
        user = ws.owner
        Notification.objects.create(
            recipient=user,
            workspace=ws,
            kind=Notification.Kind.ANNOUNCEMENT,
            preview="annpreview",
            payload={"title": "t"},
        )
        Notification.objects.create(recipient=user, workspace=ws, kind=Notification.Kind.COMMENT, preview="cmtpreview")
        client.force_login(user)
        body = client.get(reverse("web:inbox") + "?filter=announcements").content.decode()
        assert "annpreview" in body
        assert "cmtpreview" not in body
