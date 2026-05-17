"""End-to-end tests for the ``create_task`` web endpoint.

Covers GET (form rendering with pre-fills) and POST (validation,
membership enforcement, activity event, HTMX redirect).
"""

import datetime

from django.urls import reverse

import pytest

from apps.activity.models import ActivityLog
from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def setup(db):
    """Workspace + project + member user fixture."""
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    return ws, project, ws.owner


@pytest.mark.django_db
class TestCreateTaskGet:
    """GET ``/tasks/new/`` renders the modal with pre-fills."""

    def test_renders_form_for_member(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "New task" in body
        # Project should appear as an option.
        assert project.slug_prefix in body

    def test_project_prefill_selects_dropdown(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"), {"project": project.slug_prefix})
        body = resp.content.decode()
        # The selected option marker should land on the prefilled project.
        assert f'value="{project.slug_prefix}"\n                    selected' in body or "selected" in body

    def test_invalid_status_prefill_falls_back_to_planned(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"), {"status": "garbage"})
        body = resp.content.decode()
        # ``planned`` is the default backlog status (Vox: status default
        # = Planned everywhere except the kanban + which passes ?status=).
        assert 'value="planned"' in body

    def test_assignee_prefills_to_current_user(self, client, setup):
        """When the logged-in user is a member of the selected project's
        workspace, the form pre-selects them as assignee."""
        ws, project, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"))
        body = resp.content.decode()
        # The user's own option should be rendered with ``selected``.
        assert f'value="{user.id}"' in body
        assert "selected" in body

    def test_unauthenticated_redirects(self, client):
        resp = client.get(reverse("web:create_task"))
        assert resp.status_code in (302, 301)


@pytest.mark.django_db
class TestCreateTaskPost:
    """POST creates the task, logs an event, and tells HTMX to redirect."""

    def test_minimal_form_creates_task(self, client, setup):
        """Default flow: no ``open_after_create`` flag → 204 + HX-Trigger.

        The status defaults to ``planned`` (backlog) per the post-rework
        product spec; clients pre-fill ``?status=`` from the kanban
        column when they want the new task to land in something else.
        """
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "First task"},
        )
        assert resp.status_code == 204
        # ``open_after_create`` defaults off → no HX-Redirect, panel
        # refreshes via the ``acta:task-created`` HX-Trigger.
        assert "HX-Redirect" not in resp.headers
        assert resp.headers.get("HX-Trigger") == "acta:task-created"
        task = Task.objects.get(project=project, title="First task")
        assert task.reporter == user
        assert task.status == Task.STATUS_PLANNED

    def test_open_after_create_returns_redirect(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={
                "project": project.slug_prefix,
                "title": "Open me",
                "open_after_create": "1",
            },
        )
        assert resp.status_code == 204
        assert "HX-Redirect" in resp.headers
        task = Task.objects.get(title="Open me")
        assert task.project.slug_prefix in resp.headers["HX-Redirect"]
        assert str(task.number) in resp.headers["HX-Redirect"]

    def test_creates_activity_event(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "Logged"},
        )
        task = Task.objects.get(title="Logged")
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.created")
        assert events.count() == 1
        assert events.first().actor_id == user.id

    def test_full_form_persists_every_field(self, client, setup):
        ws, project, user = setup
        label = LabelFactory(workspace=ws)
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={
                "project": project.slug_prefix,
                "title": "Big task",
                "description": "details here",
                "status": Task.STATUS_IN_PROGRESS,
                "priority": Task.HIGH,
                "due_date": "2026-06-01",
                "assignee": user.id,
                "labels": [label.id],
            },
        )
        assert resp.status_code == 204
        task = Task.objects.get(title="Big task")
        assert task.description == "details here"
        assert task.status == Task.STATUS_IN_PROGRESS
        assert task.priority == Task.HIGH
        assert task.due_date == datetime.date(2026, 6, 1)
        assert task.assignee_id == user.id
        assert set(task.labels.values_list("id", flat=True)) == {label.id}

    def test_rejects_empty_title(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "   "},
        )
        assert resp.status_code == 400
        assert not Task.objects.filter(project=project).exists()

    def test_rejects_missing_project(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(reverse("web:create_task"), data={"title": "no project"})
        assert resp.status_code == 400

    def test_foreign_project_returns_404(self, client, setup):
        """A project the user doesn't belong to is invisible — 404, not 403."""
        _, _, user = setup
        other_ws = WorkspaceFactory()
        other_project = ProjectFactory(workspace=other_ws)
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": other_project.slug_prefix, "title": "sneaky"},
        )
        assert resp.status_code == 404
        assert not Task.objects.filter(project=other_project).exists()

    def test_assignee_must_be_workspace_member(self, client, setup):
        ws, project, user = setup
        outsider = WorkspaceFactory().owner
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={
                "project": project.slug_prefix,
                "title": "x",
                "assignee": outsider.id,
            },
        )
        assert resp.status_code == 400
        assert not Task.objects.filter(project=project).exists()

    def test_assignee_who_is_workspace_member_is_accepted(self, client, setup):
        ws, project, user = setup
        teammate = WorkspaceMemberFactory(workspace=ws).user
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={
                "project": project.slug_prefix,
                "title": "x",
                "assignee": teammate.id,
            },
        )
        assert resp.status_code == 204
        task = Task.objects.get(title="x")
        assert task.assignee_id == teammate.id

    def test_invalid_status_rejected(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "x", "status": "bogus"},
        )
        assert resp.status_code == 400

    def test_invalid_priority_rejected(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "x", "priority": "9"},
        )
        assert resp.status_code == 400

    def test_invalid_due_date_rejected(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "x", "due_date": "not a date"},
        )
        assert resp.status_code == 400

    def test_foreign_workspace_label_rejected(self, client, setup):
        ws, project, user = setup
        foreign_label = LabelFactory(workspace=WorkspaceFactory())
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={
                "project": project.slug_prefix,
                "title": "x",
                "labels": [foreign_label.id],
            },
        )
        assert resp.status_code == 400
        assert not Task.objects.filter(project=project).exists()

    def test_title_max_length_enforced(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "x" * 201},
        )
        assert resp.status_code == 400

    def test_unauthenticated_post_redirects(self, client, setup):
        ws, project, _ = setup
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "x"},
        )
        assert resp.status_code in (302, 301)
