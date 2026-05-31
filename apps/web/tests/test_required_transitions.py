"""Required-on-transition policy — config + validator + endpoint enforcement."""

from django.urls import reverse

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.tasks.transitions import format_missing_message, validate_status_transition
from apps.workspaces.models import Workspace
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    return ws, project


class TestRequiredFieldsConfig:

    @pytest.mark.django_db
    def test_default_is_all_false(self, setup):
        ws, _project = setup
        cfg = ws.required_fields_config()
        assert cfg["leave_todo"]["assignee"] is False
        assert cfg["leave_todo"]["priority"] is False
        assert cfg["enter_in_review"]["description"] is False

    @pytest.mark.django_db
    def test_stored_flags_round_trip(self, setup):
        ws, _project = setup
        ws.required_fields = {"leave_todo": {"assignee": True}}
        ws.save(update_fields=["required_fields"])
        cfg = ws.required_fields_config()
        assert cfg["leave_todo"]["assignee"] is True
        assert cfg["leave_todo"]["priority"] is False

    @pytest.mark.django_db
    def test_unknown_keys_dropped(self, setup):
        ws, _project = setup
        ws.required_fields = {"leave_todo": {"made_up": True}, "bogus": {"x": True}}
        ws.save(update_fields=["required_fields"])
        cfg = ws.required_fields_config()
        assert "bogus" not in cfg
        assert "made_up" not in cfg["leave_todo"]


@pytest.mark.django_db
class TestValidator:

    def _ws_with_policy(self, **policy):
        ws = WorkspaceFactory()
        ws.required_fields = policy
        ws.save(update_fields=["required_fields"])
        return ws

    def test_no_policy_no_blocking(self, setup):
        ws, project = setup
        task = TaskFactory(project=project, status=Task.STATUS_TODO, priority=0, assignee=None)
        assert validate_status_transition(task, Task.STATUS_IN_PROGRESS, ws) == []

    def test_leave_todo_missing_assignee_blocks(self, setup):
        _ws, project = setup
        ws = self._ws_with_policy(leave_todo={"assignee": True})
        project.workspace = ws
        project.save(update_fields=["workspace"])
        task = TaskFactory(project=project, status=Task.STATUS_TODO, assignee=None)
        assert validate_status_transition(task, Task.STATUS_IN_PROGRESS, ws) == ["assignee"]

    def test_leave_todo_passes_when_field_set(self, setup):
        _ws, project = setup
        ws = self._ws_with_policy(leave_todo={"assignee": True, "priority": True})
        project.workspace = ws
        project.save(update_fields=["workspace"])
        task = TaskFactory(
            project=project,
            status=Task.STATUS_TODO,
            priority=Task.HIGH,
            assignee=ws.owner,
        )
        assert validate_status_transition(task, Task.STATUS_IN_PROGRESS, ws) == []

    def test_enter_in_review_requires_description(self, setup):
        _ws, project = setup
        ws = self._ws_with_policy(enter_in_review={"description": True})
        project.workspace = ws
        project.save(update_fields=["workspace"])
        task = TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS, description="")
        assert validate_status_transition(task, Task.STATUS_IN_REVIEW, ws) == ["description"]

    def test_same_status_self_move_skipped(self, setup):
        _ws, project = setup
        ws = self._ws_with_policy(leave_todo={"assignee": True})
        project.workspace = ws
        project.save(update_fields=["workspace"])
        task = TaskFactory(project=project, status=Task.STATUS_TODO, assignee=None)
        # Already in to-do, "moving" to to-do — gate must not fire.
        assert validate_status_transition(task, Task.STATUS_TODO, ws) == []

    @pytest.mark.parametrize("backward_status", [Task.STATUS_PLANNED, Task.STATUS_READY, Task.STATUS_CANCELLED])
    def test_backward_grooming_skips_gate(self, setup, backward_status):
        """Pushing a card back into backlog / cancelling it is grooming —
        the leave_todo gate must NOT fire even with assignee/priority empty."""
        _ws, project = setup
        ws = self._ws_with_policy(leave_todo={"assignee": True, "priority": True})
        project.workspace = ws
        project.save(update_fields=["workspace"])
        task = TaskFactory(project=project, status=Task.STATUS_TODO, assignee=None, priority=0)
        assert validate_status_transition(task, backward_status, ws) == []


@pytest.mark.django_db
class TestDrfPatchPath:
    """Kanban DnD goes through the DRF ``PATCH /api/v1/tasks/<id>/`` route —
    the policy must gate it too, not just the web ``set_task_status`` view."""

    def test_drf_patch_blocks_when_missing(self, client, setup):
        ws, project = setup
        ws.required_fields = {"leave_todo": {"assignee": True}}
        ws.save(update_fields=["required_fields"])
        task = TaskFactory(project=project, status=Task.STATUS_TODO, assignee=None, reporter=ws.owner)
        client.force_login(ws.owner)
        resp = client.patch(
            f"/api/v1/tasks/{task.id}/",
            data='{"status": "in-progress"}',
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "Assignee" in resp.content.decode()
        task.refresh_from_db()
        assert task.status == Task.STATUS_TODO

    def test_drf_patch_passes_when_filled(self, client, setup):
        ws, project = setup
        ws.required_fields = {"leave_todo": {"assignee": True}}
        ws.save(update_fields=["required_fields"])
        task = TaskFactory(project=project, status=Task.STATUS_TODO, assignee=ws.owner, reporter=ws.owner)
        client.force_login(ws.owner)
        resp = client.patch(
            f"/api/v1/tasks/{task.id}/",
            data='{"status": "in-progress"}',
            content_type="application/json",
        )
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.status == Task.STATUS_IN_PROGRESS


@pytest.mark.django_db
class TestSetTaskStatusEndpoint:

    def test_blocked_when_missing_returns_422_with_toast(self, client, setup):
        ws, project = setup
        ws.required_fields = {"leave_todo": {"assignee": True}}
        ws.save(update_fields=["required_fields"])
        task = TaskFactory(project=project, status=Task.STATUS_TODO, assignee=None, reporter=ws.owner)
        client.force_login(ws.owner)
        resp = client.post(
            reverse("web:set_task_status", args=[project.slug_prefix, task.number]),
            {"status": Task.STATUS_IN_PROGRESS},
        )
        assert resp.status_code == 422
        task.refresh_from_db()
        assert task.status == Task.STATUS_TODO  # unchanged
        assert "acta:toast" in resp.headers.get("HX-Trigger", "")
        assert "Assignee" in resp.headers["HX-Trigger"]

    def test_passes_when_field_filled(self, client, setup):
        ws, project = setup
        ws.required_fields = {"leave_todo": {"assignee": True}}
        ws.save(update_fields=["required_fields"])
        task = TaskFactory(project=project, status=Task.STATUS_TODO, assignee=ws.owner, reporter=ws.owner)
        client.force_login(ws.owner)
        resp = client.post(
            reverse("web:set_task_status", args=[project.slug_prefix, task.number]),
            {"status": Task.STATUS_IN_PROGRESS},
        )
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.status == Task.STATUS_IN_PROGRESS


class TestFormatMissingMessage:

    def test_empty_returns_empty(self):
        assert format_missing_message([]) == ""

    def test_single_field_singular(self):
        assert "Assignee is required" in format_missing_message(["assignee"])

    def test_multiple_fields_plural(self):
        msg = format_missing_message(["assignee", "priority"])
        assert "Assignee" in msg and "Priority" in msg and "are required" in msg


@pytest.mark.django_db
class TestSettingsEndpoint:

    def test_admin_saves_policy(self, client, setup):
        ws, _project = setup
        client.force_login(ws.owner)
        resp = client.post(
            reverse("web:set_workspace_required_fields", args=[ws.slug]),
            {"required_leave_todo_assignee": "on", "required_enter_in_review_description": "on"},
        )
        assert resp.status_code in (200, 302)
        ws.refresh_from_db()
        cfg = ws.required_fields_config()
        assert cfg["leave_todo"]["assignee"] is True
        assert cfg["leave_todo"]["priority"] is False
        assert cfg["enter_in_review"]["description"] is True

    def test_member_forbidden(self, client, setup):
        from apps.workspaces.tests.factories import WorkspaceMemberFactory

        ws, _project = setup
        member = WorkspaceFactory().owner
        WorkspaceMemberFactory(
            workspace=ws,
            user=member,
            role=Workspace._meta.get_field("members").related_model.MEMBER if False else "member",
        )
        client.force_login(member)
        resp = client.post(
            reverse("web:set_workspace_required_fields", args=[ws.slug]),
            {"required_leave_todo_assignee": "on"},
        )
        assert resp.status_code == 403
