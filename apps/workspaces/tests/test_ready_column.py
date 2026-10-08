"""Retiring the Ready column, and what that costs.

Ready is a replenishment buffer some teams groom into and others never
touch, and a column nobody fills is worse than no column — it splits the
backlog in two and makes every picker one row longer. A workspace may
turn it off; this covers what happens to the work that was in it, and
what happens to writes that still aim at it.
"""

from django.core.exceptions import ValidationError

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.services import retire_ready_status
from apps.workspaces.tests.factories import WorkspaceFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope():
    """A workspace with an admin and one project."""
    user = UserFactory()
    workspace = WorkspaceFactory(owner=user)
    WorkspaceMember.objects.get_or_create(user=user, workspace=workspace, defaults={"role": "admin"})
    return workspace, user, ProjectFactory(workspace=workspace)


class TestTheColumnItself:
    """What the workspace offers, which is not what it can render."""

    def test_a_workspace_keeps_ready_by_default(self, scope):
        workspace, _, _ = scope

        assert workspace.ready_enabled
        assert Task.STATUS_READY in workspace.board_statuses()

    def test_turning_it_off_takes_the_column_out_of_the_board(self, scope):
        workspace, _, _ = scope
        workspace.ready_enabled = False

        assert Task.STATUS_READY not in workspace.board_statuses()
        assert not workspace.offers_status(Task.STATUS_READY)
        # Every other column is untouched — this is one column, not a
        # rewrite of the workflow.
        assert Task.STATUS_TODO in workspace.board_statuses()

    def test_the_label_survives_the_column(self, scope):
        """An old activity entry still has to name the status it names."""
        workspace, _, _ = scope
        workspace.ready_enabled = False

        assert Task.STATUS_LABELS[Task.STATUS_READY]


class TestRetiring:
    """Two hundred tasks changing status is not a silent act (ADR 0011)."""

    def test_the_work_moves_to_planned(self, scope):
        workspace, user, project = scope
        ready = [TaskFactory(project=project, status=Task.STATUS_READY) for _ in range(3)]
        untouched = TaskFactory(project=project, status=Task.STATUS_TODO)

        moved = retire_ready_status(workspace, actor=user)

        assert moved == 3
        assert {Task.objects.get(id=task.id).status for task in ready} == {Task.STATUS_PLANNED}
        untouched.refresh_from_db()
        assert untouched.status == Task.STATUS_TODO

    def test_every_move_is_in_the_log_under_one_act(self, scope):
        workspace, user, project = scope
        for _index in range(3):
            TaskFactory(project=project, status=Task.STATUS_READY)

        retire_ready_status(workspace, actor=user)

        events = ActivityLog.objects.filter(event_type="task.status_changed")
        assert events.count() == 3
        assert {event.actor_id for event in events} == {user.id}
        assert len({event.bulk_id for event in events}) == 1
        assert all(event.payload["to"] == Task.STATUS_PLANNED for event in events)
        assert all(event.payload["reason"] == "ready_column_retired" for event in events)

    def test_nothing_ready_writes_nothing(self, scope):
        workspace, user, project = scope
        TaskFactory(project=project, status=Task.STATUS_TODO)

        assert retire_ready_status(workspace, actor=user) == 0
        assert not ActivityLog.objects.filter(event_type="task.status_changed").exists()

    def test_turning_it_back_on_does_not_undo_the_move(self, scope):
        """A status change happened; pretending otherwise would be a second one."""
        workspace, user, project = scope
        task = TaskFactory(project=project, status=Task.STATUS_READY)
        retire_ready_status(workspace, actor=user)

        workspace.ready_enabled = True
        workspace.save(update_fields=["ready_enabled"])

        task.refresh_from_db()
        assert task.status == Task.STATUS_PLANNED


class TestWritesIntoARetiredColumn:
    """Refused, not quietly rerouted."""

    def test_the_model_refuses(self, scope):
        workspace, _, project = scope
        workspace.ready_enabled = False
        workspace.save(update_fields=["ready_enabled"])
        task = Task(project=project, title="Nowhere to go", status=Task.STATUS_READY)

        with pytest.raises(ValidationError) as caught:
            task.full_clean()

        assert "status" in caught.value.error_dict

    def test_the_api_says_which_field_and_why(self, scope):
        workspace, user, project = scope
        workspace.ready_enabled = False
        workspace.save(update_fields=["ready_enabled"])
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "Nowhere to go", "status": Task.STATUS_READY},
            format="json",
        )

        assert response.status_code == 400
        assert "status" in response.data

    def test_the_other_statuses_still_go_through(self, scope):
        workspace, user, project = scope
        workspace.ready_enabled = False
        workspace.save(update_fields=["ready_enabled"])
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "Fine", "status": Task.STATUS_TODO},
            format="json",
        )

        assert response.status_code == 201


class TestThePagesThatOfferIt:
    """A picker offers what the workspace has, a label renders what it had."""

    def test_the_status_picker_stops_offering_ready(self, client, scope):
        workspace, user, project = scope
        task = TaskFactory(project=project, status=Task.STATUS_TODO)
        client.force_login(user)
        url = f"/{workspace.slug}/projects/{project.slug_prefix}/{task.number}/"

        before = client.get(url).content.decode()
        workspace.ready_enabled = False
        workspace.save(update_fields=["ready_enabled"])
        after = client.get(url).content.decode()

        assert 'name="status" value="ready"' in before
        assert 'name="status" value="ready"' not in after
        # The rest of the menu is untouched.
        assert 'name="status" value="to-do"' in after

    def test_the_board_stops_drawing_the_column(self, client, scope):
        workspace, user, project = scope
        TaskFactory(project=project, status=Task.STATUS_TODO)
        workspace.ready_enabled = False
        workspace.save(update_fields=["ready_enabled"])
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/projects/{project.slug_prefix}/?view=kanban")

        assert resp.status_code == 200
        assert [column["key"] for column in resp.context["columns"]] == [
            Task.STATUS_PLANNED,
            Task.STATUS_TODO,
            Task.STATUS_IN_PROGRESS,
            Task.STATUS_IN_REVIEW,
            Task.STATUS_DONE,
        ]

    def test_the_settings_page_says_what_the_switch_would_cost(self, client, scope):
        workspace, user, project = scope
        for _index in range(4):
            TaskFactory(project=project, status=Task.STATUS_READY)
        client.force_login(user)

        resp = client.get(f"/workspaces/{workspace.slug}/settings/")

        assert resp.context["ready_count"] == 4
        assert "4 tasks would move to Planned." in resp.content.decode()
