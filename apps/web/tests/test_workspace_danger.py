"""Tests for the Danger tab — transfer ownership + delete workspace.

Both are owner-only (ADR 0010). Transfer keeps exactly one owner and
demotes the previous one to admin; delete is a typed-slug-confirmed hard
cascade.
"""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.models import Project
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import Workspace, WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def workspace(db):
    return WorkspaceFactory()


@pytest.fixture
def owner(workspace):
    return workspace.owner


@pytest.fixture
def admin_user(workspace):
    user = UserFactory()
    WorkspaceMemberFactory(workspace=workspace, user=user, role=WorkspaceMember.ADMIN)
    return user


@pytest.fixture
def member(workspace):
    user = UserFactory()
    WorkspaceMemberFactory(workspace=workspace, user=user, role=WorkspaceMember.MEMBER)
    return user


def _transfer_url(ws):
    return reverse("web:transfer_workspace_ownership", kwargs={"slug": ws.slug})


def _delete_url(ws):
    return reverse("web:delete_workspace", kwargs={"slug": ws.slug})


@pytest.mark.django_db
class TestTransferOwnership:

    def test_owner_transfers_and_is_demoted(self, client, workspace, owner, member):
        client.force_login(owner)
        resp = client.post(_transfer_url(workspace), {"new_owner_id": member.id})
        assert resp.status_code == 302
        workspace.refresh_from_db()
        assert workspace.owner_id == member.id
        assert WorkspaceMember.objects.get(workspace=workspace, user=member).role == WorkspaceMember.OWNER
        assert WorkspaceMember.objects.get(workspace=workspace, user=owner).role == WorkspaceMember.ADMIN
        # Exactly one owner remains.
        assert WorkspaceMember.objects.filter(workspace=workspace, role=WorkspaceMember.OWNER).count() == 1

    def test_admin_cannot_transfer(self, client, workspace, owner, admin_user, member):
        client.force_login(admin_user)
        resp = client.post(_transfer_url(workspace), {"new_owner_id": member.id})
        assert resp.status_code == 403
        workspace.refresh_from_db()
        assert workspace.owner_id == owner.id

    def test_transfer_to_non_member_rejected(self, client, workspace, owner):
        outsider = UserFactory()
        client.force_login(owner)
        resp = client.post(_transfer_url(workspace), {"new_owner_id": outsider.id})
        assert resp.status_code == 400
        workspace.refresh_from_db()
        assert workspace.owner_id == owner.id

    def test_transfer_to_self_rejected(self, client, workspace, owner):
        client.force_login(owner)
        resp = client.post(_transfer_url(workspace), {"new_owner_id": owner.id})
        assert resp.status_code == 400

    def test_invalid_id_rejected(self, client, workspace, owner):
        client.force_login(owner)
        resp = client.post(_transfer_url(workspace), {"new_owner_id": "nope"})
        assert resp.status_code == 400


@pytest.mark.django_db
class TestDeleteWorkspace:

    def test_owner_deletes_with_correct_slug_cascades(self, client, workspace, owner):
        project = ProjectFactory(workspace=workspace)
        TaskFactory(project=project, reporter=owner)
        wid, pid = workspace.id, project.id
        client.force_login(owner)
        resp = client.post(_delete_url(workspace), {"confirm_slug": workspace.slug})
        assert resp.status_code == 302
        assert not Workspace.objects.filter(id=wid).exists()
        assert not Project.objects.filter(id=pid).exists()
        assert not Task.objects.filter(project_id=pid).exists()

    def test_active_workspace_nulled_not_user_deleted(self, client, workspace, owner):
        owner.active_workspace = workspace
        owner.save(update_fields=["active_workspace"])
        client.force_login(owner)
        client.post(_delete_url(workspace), {"confirm_slug": workspace.slug})
        owner.refresh_from_db()
        assert owner.pk is not None
        assert owner.active_workspace_id is None

    def test_wrong_slug_rejected(self, client, workspace, owner):
        client.force_login(owner)
        resp = client.post(_delete_url(workspace), {"confirm_slug": "nope"})
        assert resp.status_code == 400
        assert Workspace.objects.filter(id=workspace.id).exists()

    def test_admin_cannot_delete(self, client, workspace, admin_user):
        client.force_login(admin_user)
        resp = client.post(_delete_url(workspace), {"confirm_slug": workspace.slug})
        assert resp.status_code == 403
        assert Workspace.objects.filter(id=workspace.id).exists()

    def test_outsider_404(self, client, workspace):
        client.force_login(UserFactory())
        resp = client.post(_delete_url(workspace), {"confirm_slug": workspace.slug})
        assert resp.status_code == 404
        assert Workspace.objects.filter(id=workspace.id).exists()
