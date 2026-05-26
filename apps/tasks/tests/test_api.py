"""Integration tests for :class:`TaskViewSet` through DRF's ``APIClient``.

Covers the CRUD happy path, workspace scoping (cross-workspace access
returns 404 via the queryset, never 403 with a leak), the cross-field
serializer invariants (parent/project agreement, depth cap, label and
assignee workspace membership, size Fibonacci set, status enum), and the
activity events emitted by ``perform_create`` / ``perform_update`` /
``perform_destroy``.
"""

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def member():
    """Return a fresh user."""
    return UserFactory()


@pytest.fixture
def workspace(member):
    """Return a workspace whose owner is ``member``."""
    return WorkspaceFactory(owner=member)


@pytest.fixture
def project(workspace):
    """Return a project inside ``workspace``."""
    return ProjectFactory(workspace=workspace)


@pytest.fixture
def client(member):
    """Return an ``APIClient`` already authenticated as ``member``."""
    c = APIClient()
    c.force_authenticate(member)
    return c


@pytest.mark.django_db
class TestTaskCrud:
    """Create / read / update / delete the happy path."""

    def test_create_task(self, client, member, project):
        resp = client.post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "First task"},
        )
        assert resp.status_code == 201, resp.content
        task = Task.objects.get(id=resp.data["id"])
        assert task.title == "First task"
        assert task.reporter == member
        assert task.number == 1
        assert resp.data["slug"] == f"{project.slug_prefix}-1"

    def test_retrieve_task(self, client, project):
        task = TaskFactory(project=project)
        resp = client.get(f"/api/v1/tasks/{task.id}/")
        assert resp.status_code == 200
        assert resp.data["id"] == task.id

    def test_list_only_returns_own_workspace_tasks(self, client, project):
        mine = TaskFactory(project=project)
        TaskFactory()  # in a foreign workspace
        resp = client.get("/api/v1/tasks/")
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.data["results"]}
        assert ids == {mine.id}

    def test_update_title(self, client, project):
        task = TaskFactory(project=project, title="old")
        resp = client.patch(f"/api/v1/tasks/{task.id}/", {"title": "new"})
        assert resp.status_code == 200, resp.content
        task.refresh_from_db()
        assert task.title == "new"

    def test_delete_task(self, client, project):
        task = TaskFactory(project=project)
        resp = client.delete(f"/api/v1/tasks/{task.id}/")
        assert resp.status_code == 204
        assert not Task.objects.filter(id=task.id).exists()

    def test_anonymous_is_rejected(self, project):
        resp = APIClient().get("/api/v1/tasks/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestTaskWorkspaceScoping:
    """Cross-workspace access must 404, not leak via 403."""

    def test_retrieve_foreign_task_returns_404(self, client):
        foreign = TaskFactory()  # different workspace
        resp = client.get(f"/api/v1/tasks/{foreign.id}/")
        assert resp.status_code == 404

    def test_update_foreign_task_returns_404(self, client):
        foreign = TaskFactory()
        resp = client.patch(f"/api/v1/tasks/{foreign.id}/", {"title": "hijack"})
        assert resp.status_code == 404
        foreign.refresh_from_db()
        assert foreign.title != "hijack"

    def test_delete_foreign_task_returns_404(self, client):
        foreign = TaskFactory()
        resp = client.delete(f"/api/v1/tasks/{foreign.id}/")
        assert resp.status_code == 404
        assert Task.objects.filter(id=foreign.id).exists()

    def test_cannot_create_task_in_foreign_project(self, client):
        foreign_project = ProjectFactory()
        resp = client.post(
            "/api/v1/tasks/",
            {"project": foreign_project.id, "title": "x"},
        )
        assert resp.status_code == 400, resp.content
        assert "project" in resp.data


@pytest.mark.django_db
class TestTaskSerializerInvariants:
    """Cross-field validation in :class:`TaskSerializer`."""

    def test_subtask_must_share_parent_project(self, client, member, workspace):
        p1 = ProjectFactory(workspace=workspace)
        p2 = ProjectFactory(workspace=workspace)
        parent = TaskFactory(project=p1)
        resp = client.post(
            "/api/v1/tasks/",
            {"project": p2.id, "title": "child", "parent": parent.id},
        )
        assert resp.status_code == 400
        assert "parent" in resp.data

    def test_depth_limit_one_level(self, client, project):
        parent = TaskFactory(project=project)
        child = TaskFactory(project=project, parent=parent)
        resp = client.post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "grandchild", "parent": child.id},
        )
        assert resp.status_code == 400
        assert "parent" in resp.data

    def test_label_must_be_same_workspace(self, client, project):
        foreign_label = LabelFactory()  # different workspace
        resp = client.post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "x", "labels": [foreign_label.id]},
        )
        assert resp.status_code == 400
        assert "labels" in resp.data

    def test_assignee_must_be_workspace_member(self, client, project):
        outsider = UserFactory()
        resp = client.post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "x", "assignee": outsider.id},
        )
        assert resp.status_code == 400
        assert "assignee" in resp.data

    def test_assignee_member_accepted(self, client, workspace, project):
        teammate = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=teammate, role=WorkspaceMember.MEMBER)
        resp = client.post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "x", "assignee": teammate.id},
        )
        assert resp.status_code == 201, resp.content

    @pytest.mark.parametrize("bad_size", [4, 6, 7, 9, 10])
    def test_size_rejects_non_fibonacci(self, client, project, bad_size):
        resp = client.post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "x", "size": bad_size},
        )
        assert resp.status_code == 400, resp.content
        assert "size" in resp.data

    @pytest.mark.parametrize("ok_size", [1, 2, 3, 5, 8, 13])
    def test_size_accepts_fibonacci(self, client, project, ok_size):
        resp = client.post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "x", "size": ok_size},
        )
        assert resp.status_code == 201, resp.content

    def test_unknown_status_rejected(self, client, project):
        resp = client.post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "x", "status": "frobnicated"},
        )
        assert resp.status_code == 400
        assert "status" in resp.data


@pytest.mark.django_db
class TestTaskNumberAllocation:
    """``perform_create`` allocates a monotonic project-local number."""

    def test_numbers_are_monotonic_per_project(self, client, project):
        first = client.post("/api/v1/tasks/", {"project": project.id, "title": "a"})
        second = client.post("/api/v1/tasks/", {"project": project.id, "title": "b"})
        assert first.data["number"] == 1
        assert second.data["number"] == 2

    def test_number_is_read_only_from_payload(self, client, project):
        resp = client.post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "a", "number": 999},
        )
        assert resp.status_code == 201
        assert resp.data["number"] == 1


@pytest.mark.django_db
class TestTaskActivityEvents:
    """Every mutation funnels through ``log_event``."""

    def test_create_emits_task_created(self, client, project):
        resp = client.post("/api/v1/tasks/", {"project": project.id, "title": "a"})
        assert ActivityLog.objects.filter(
            event_type="task.created",
            target_id=resp.data["id"],
        ).exists()

    def test_status_change_emits_status_changed(self, client, member, project):
        task = TaskFactory(project=project, status=Task.STATUS_TODO)
        client.patch(f"/api/v1/tasks/{task.id}/", {"status": Task.STATUS_DONE})
        assert ActivityLog.objects.filter(
            event_type="task.status_changed",
            target_id=task.id,
            actor=member,
        ).exists()

    def test_delete_emits_task_deleted(self, client, project):
        task = TaskFactory(project=project)
        client.delete(f"/api/v1/tasks/{task.id}/")
        assert ActivityLog.objects.filter(
            event_type="task.deleted",
            target_id=task.id,
        ).exists()
