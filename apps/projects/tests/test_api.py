"""Integration tests for ``ProjectViewSet`` and ``ProjectUpdateViewSet``.

Covers workspace scoping, the ``archived`` filter, project creation
membership guard, and the author-or-admin write matrix on project
updates.
"""

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.projects.models import Project, ProjectUpdate
from apps.projects.tests.factories import ProjectFactory, ProjectUpdateFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def member():
    return UserFactory()


@pytest.fixture
def workspace(member):
    return WorkspaceFactory(owner=member)


@pytest.fixture
def client(member):
    c = APIClient()
    c.force_authenticate(member)
    return c


@pytest.mark.django_db
class TestProjectCrud:
    def test_create_project(self, client, workspace):
        resp = client.post(
            "/api/v1/projects/",
            {"workspace": workspace.id, "name": "Roadmap", "slug_prefix": "ROAD"},
        )
        assert resp.status_code == 201, resp.content
        assert Project.objects.filter(id=resp.data["id"], name="Roadmap").exists()

    def test_cannot_create_in_foreign_workspace(self, client):
        foreign = WorkspaceFactory()
        resp = client.post(
            "/api/v1/projects/",
            {"workspace": foreign.id, "name": "x", "slug_prefix": "X"},
        )
        assert resp.status_code == 400
        assert "workspace" in resp.data

    def test_list_scoped_to_membership(self, client, workspace):
        mine = ProjectFactory(workspace=workspace)
        ProjectFactory()  # foreign
        resp = client.get("/api/v1/projects/")
        ids = {row["id"] for row in resp.data["results"]}
        assert ids == {mine.id}

    def test_retrieve_foreign_project_404(self, client):
        foreign = ProjectFactory()
        resp = client.get(f"/api/v1/projects/{foreign.id}/")
        assert resp.status_code == 404

    def test_archived_filter(self, client, workspace):
        active = ProjectFactory(workspace=workspace, archived=False)
        archived = ProjectFactory(workspace=workspace, archived=True)
        resp = client.get("/api/v1/projects/?archived=true")
        ids = {row["id"] for row in resp.data["results"]}
        assert ids == {archived.id}
        assert active.id not in ids

    def test_next_task_number_read_only(self, client, workspace):
        resp = client.post(
            "/api/v1/projects/",
            {"workspace": workspace.id, "name": "x", "slug_prefix": "XX", "next_task_number": 50},
        )
        assert resp.status_code == 201
        assert resp.data["next_task_number"] == 1


@pytest.mark.django_db
class TestProjectUpdateWrites:
    """Author-or-admin matrix on ``ProjectUpdateViewSet``."""

    def test_create_sets_author(self, client, member, workspace):
        project = ProjectFactory(workspace=workspace)
        resp = client.post(
            "/api/v1/project-updates/",
            {"project": project.id, "health": ProjectUpdate.ON_TRACK, "body": "All good"},
        )
        assert resp.status_code == 201, resp.content
        update = ProjectUpdate.objects.get(id=resp.data["id"])
        assert update.author == member

    def test_author_can_edit_own(self, client, member, workspace):
        project = ProjectFactory(workspace=workspace)
        update = ProjectUpdateFactory(project=project, author=member)
        resp = client.patch(f"/api/v1/project-updates/{update.id}/", {"body": "edited"})
        assert resp.status_code == 200, resp.content
        update.refresh_from_db()
        assert update.body == "edited"

    def test_member_cannot_edit_others_update(self, workspace):
        author = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=author, role=WorkspaceMember.MEMBER)
        regular = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=regular, role=WorkspaceMember.MEMBER)
        project = ProjectFactory(workspace=workspace)
        update = ProjectUpdateFactory(project=project, author=author)
        client = APIClient()
        client.force_authenticate(regular)
        resp = client.patch(f"/api/v1/project-updates/{update.id}/", {"body": "hijack"})
        assert resp.status_code == 403
        update.refresh_from_db()
        assert update.body != "hijack"

    def test_admin_can_edit_others_update(self, workspace):
        author = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=author, role=WorkspaceMember.MEMBER)
        admin = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=admin, role=WorkspaceMember.ADMIN)
        project = ProjectFactory(workspace=workspace)
        update = ProjectUpdateFactory(project=project, author=author)
        client = APIClient()
        client.force_authenticate(admin)
        resp = client.patch(f"/api/v1/project-updates/{update.id}/", {"body": "moderated"})
        assert resp.status_code == 200, resp.content
        update.refresh_from_db()
        assert update.body == "moderated"
