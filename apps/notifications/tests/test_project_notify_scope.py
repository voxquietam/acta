"""Per-project notification scope — ``Project.notify_members_only`` flag."""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import Notification
from apps.notifications.services import notify_project_update_created
from apps.projects.models import ProjectUpdate
from apps.projects.tests.factories import ProjectFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def setup(db):
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    return ws, project


@pytest.mark.django_db
class TestFanoutScope:

    def test_default_notifies_whole_workspace(self, setup):
        ws, project = setup
        # Two extra workspace members; neither in project.members.
        m1 = WorkspaceMemberFactory(workspace=ws, role=WorkspaceMember.MEMBER).user
        m2 = WorkspaceMemberFactory(workspace=ws, role=WorkspaceMember.MEMBER).user
        update = ProjectUpdate.objects.create(
            project=project,
            author=ws.owner,
            health=ProjectUpdate.ON_TRACK,
            body="x",
        )
        notify_project_update_created(update=update, actor=ws.owner)
        recipients = set(
            Notification.objects.filter(kind=Notification.Kind.PROJECT_UPDATE).values_list("recipient_id", flat=True),
        )
        # ws.owner is the author and gets self-suppressed; the two members do.
        assert m1.id in recipients
        assert m2.id in recipients
        assert ws.owner_id not in recipients

    def test_members_only_narrows_audience(self, setup):
        ws, project = setup
        in_project = WorkspaceMemberFactory(workspace=ws).user
        outsider = WorkspaceMemberFactory(workspace=ws).user
        project.members.add(in_project)
        project.notify_members_only = True
        project.save(update_fields=["notify_members_only"])
        update = ProjectUpdate.objects.create(
            project=project,
            author=ws.owner,
            health=ProjectUpdate.ON_TRACK,
            body="x",
        )
        notify_project_update_created(update=update, actor=ws.owner)
        recipients = set(
            Notification.objects.filter(kind=Notification.Kind.PROJECT_UPDATE).values_list("recipient_id", flat=True),
        )
        assert in_project.id in recipients
        assert outsider.id not in recipients

    def test_lead_always_included_when_members_only(self, setup):
        ws, project = setup
        lead = WorkspaceMemberFactory(workspace=ws).user
        project.lead = lead
        project.notify_members_only = True
        project.save(update_fields=["lead", "notify_members_only"])
        # Lead is not in project.members, but still gets notified.
        update = ProjectUpdate.objects.create(
            project=project,
            author=ws.owner,
            health=ProjectUpdate.ON_TRACK,
            body="x",
        )
        notify_project_update_created(update=update, actor=ws.owner)
        assert Notification.objects.filter(
            kind=Notification.Kind.PROJECT_UPDATE,
            recipient=lead,
        ).exists()


@pytest.mark.django_db
class TestEndpoint:

    def test_admin_toggles_scope(self, client, setup):
        ws, project = setup
        client.force_login(ws.owner)
        resp = client.post(
            reverse("web:set_project_notify_scope", args=[project.slug_prefix]),
            {"notify_members_only": "on"},
        )
        assert resp.status_code == 200
        project.refresh_from_db()
        assert project.notify_members_only is True

    def test_member_forbidden(self, client, setup):
        ws, project = setup
        other = UserFactory()
        WorkspaceMemberFactory(workspace=ws, user=other, role=WorkspaceMember.MEMBER)
        client.force_login(other)
        resp = client.post(
            reverse("web:set_project_notify_scope", args=[project.slug_prefix]),
            {"notify_members_only": "on"},
        )
        assert resp.status_code == 403
