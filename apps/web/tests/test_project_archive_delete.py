"""Tests for project archive / unarchive / delete (web views).

Covers the owner/admin gate, the soft-archive toggle, the typed-slug
confirmation on the hard delete + its cascade, and that ``ProjectListView``
hides archived projects unless ``?archived=1``.
"""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.models import Project
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def workspace(db):
    return WorkspaceFactory()


@pytest.fixture
def owner(workspace):
    return workspace.owner


@pytest.fixture
def member(workspace):
    user = UserFactory()
    WorkspaceMemberFactory(workspace=workspace, user=user, role=WorkspaceMember.MEMBER)
    return user


@pytest.fixture
def project(workspace):
    return ProjectFactory(workspace=workspace)


def _archive_url(p):
    return reverse("web:set_project_archived", kwargs={"slug_prefix": p.slug_prefix})


def _delete_url(p):
    return reverse("web:delete_project", kwargs={"slug_prefix": p.slug_prefix})


@pytest.mark.django_db
class TestArchiveProject:

    def test_admin_archives(self, client, owner, project):
        client.force_login(owner)
        resp = client.post(_archive_url(project), {"archived": "1"})
        assert resp.status_code == 302
        project.refresh_from_db()
        assert project.archived is True

    def test_admin_unarchives(self, client, owner, project):
        project.archived = True
        project.save(update_fields=["archived"])
        client.force_login(owner)
        resp = client.post(_archive_url(project), {"archived": "0"})
        assert resp.status_code == 302
        project.refresh_from_db()
        assert project.archived is False

    def test_member_forbidden(self, client, member, project):
        client.force_login(member)
        resp = client.post(_archive_url(project), {"archived": "1"})
        assert resp.status_code == 403
        project.refresh_from_db()
        assert project.archived is False

    def test_outsider_404(self, client, project):
        client.force_login(UserFactory())
        resp = client.post(_archive_url(project), {"archived": "1"})
        assert resp.status_code == 404


@pytest.mark.django_db
class TestDeleteProject:

    def test_admin_deletes_with_correct_slug_cascades(self, client, owner, project):
        TaskFactory(project=project, reporter=owner)
        pid = project.id
        client.force_login(owner)
        resp = client.post(_delete_url(project), {"confirm_slug": project.slug_prefix})
        assert resp.status_code == 302
        assert not Project.objects.filter(id=pid).exists()
        assert not Task.objects.filter(project_id=pid).exists()

    def test_wrong_slug_rejected(self, client, owner, project):
        client.force_login(owner)
        resp = client.post(_delete_url(project), {"confirm_slug": "WRONG"})
        assert resp.status_code == 400
        assert Project.objects.filter(id=project.id).exists()

    def test_empty_slug_rejected(self, client, owner, project):
        client.force_login(owner)
        resp = client.post(_delete_url(project), {})
        assert resp.status_code == 400
        assert Project.objects.filter(id=project.id).exists()

    def test_member_forbidden(self, client, member, project):
        client.force_login(member)
        resp = client.post(_delete_url(project), {"confirm_slug": project.slug_prefix})
        assert resp.status_code == 403
        assert Project.objects.filter(id=project.id).exists()


@pytest.mark.django_db
class TestProjectListArchivedFilter:

    def test_archived_hidden_by_default(self, client, owner, workspace):
        active = ProjectFactory(workspace=workspace, archived=False)
        arch = ProjectFactory(workspace=workspace, archived=True)
        client.force_login(owner)
        resp = client.get(reverse("web:project_list"))
        assert resp.status_code == 200
        ids = {p.id for p in resp.context["projects"]}
        assert active.id in ids
        assert arch.id not in ids
        assert resp.context["archived_count"] == 1
        assert resp.context["show_archived"] is False

    def test_show_archived_reveals(self, client, owner, workspace):
        arch = ProjectFactory(workspace=workspace, archived=True)
        client.force_login(owner)
        resp = client.get(reverse("web:project_list") + "?archived=1")
        assert resp.status_code == 200
        ids = {p.id for p in resp.context["projects"]}
        assert arch.id in ids
        assert resp.context["show_archived"] is True
