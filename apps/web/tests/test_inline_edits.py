"""Inline-edit endpoints on the task detail page.

Covers status / priority / due-date / assignee / labels / title
inline edits and comment posting (:mod:`apps.web.views`).
"""

import datetime

from django.urls import reverse
from django.utils import timezone

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.comments.models import Comment
from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web.views import _build_timeline
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def setup(db):
    """Workspace + project + task + member user fixture.

    Returns:
        Tuple ``(user, project, task)``.
    """
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    task = TaskFactory(project=project, reporter=ws.owner, status=Task.STATUS_TODO)
    return ws.owner, project, task


@pytest.mark.django_db
class TestSetTaskStatus:
    """``POST /projects/<slug>/<number>/status/`` updates the task in place."""

    def _url(self, project, task):
        return reverse(
            "web:set_task_status",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_valid_change_returns_fragment_and_emits_event(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"status": Task.STATUS_DONE})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.status == Task.STATUS_DONE
        # Fragment, not full page.
        body = resp.content.decode()
        assert "<html" not in body
        # Status badge included.
        assert 'id="status-cell"' in body
        # Activity event written with the right type.
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.status_changed")
        assert events.count() == 1
        assert events.get().payload == {"from": "to-do", "to": "done"}

    def test_dropdown_panel_opts_into_force_apply_self_event(self, client, setup):
        """The teleported dropdown must call ``actaForceApplySelfEvent``
        for this task on ``htmx:before-request`` — without it the SSE
        self-filter would drop the change and the surrounding table /
        kanban / list row would stay stale until the page is reloaded."""
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"status": Task.STATUS_TODO})
        assert resp.status_code == 200
        body = resp.content.decode()
        # The dropdown panel carries the hook with the task id baked in.
        assert f"actaForceApplySelfEvent({task.id})" in body
        # And it's wired to ``htmx:before-request`` (so the SSE force-apply
        # is set up before the broadcast can race the response).
        assert "@htmx:before-request" in body or "x-on:htmx:before-request" in body

    def test_invalid_status_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"status": "lolwut"})
        assert resp.status_code == 400
        task.refresh_from_db()
        assert task.status == Task.STATUS_TODO

    def test_foreign_task_returns_404(self, client, setup):
        user, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        client.force_login(user)
        resp = client.post(
            reverse(
                "web:set_task_status",
                kwargs={
                    "slug_prefix": foreign_project.slug_prefix,
                    "number": foreign_task.number,
                },
            ),
            {"status": Task.STATUS_DONE},
        )
        assert resp.status_code == 404
        foreign_task.refresh_from_db()
        assert foreign_task.status == Task.STATUS_TODO

    def test_anonymous_redirected(self, client, setup):
        _, project, task = setup
        resp = client.post(self._url(project, task), {"status": Task.STATUS_DONE})
        # The view is @require_POST + manual auth check returning 400 when
        # unauthenticated. We accept any non-200 here as long as nothing
        # changes.
        assert resp.status_code in (302, 400, 403)
        task.refresh_from_db()
        assert task.status == Task.STATUS_TODO


@pytest.mark.django_db
class TestSetTaskPriority:
    """``POST /projects/<slug>/<number>/priority/`` updates priority."""

    def _url(self, project, task):
        return reverse(
            "web:set_task_priority",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_valid_change(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"priority": Task.URGENT})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.priority == Task.URGENT
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.priority_changed")
        assert events.count() == 1

    def test_out_of_range_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"priority": "9"})
        assert resp.status_code == 400
        task.refresh_from_db()
        assert task.priority == Task.NO_PRIORITY

    def test_non_int_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"priority": "abc"})
        assert resp.status_code == 400

    def test_dropdown_panel_opts_into_force_apply_self_event(self, client, setup):
        """Mirror of the status-cell opt-in — see TestSetTaskStatus.

        Without it the SSE self-filter drops the change and the row in
        the kanban / table behind the modal stays stale until reload.
        """
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"priority": Task.URGENT})
        assert resp.status_code == 200
        body = resp.content.decode()
        assert f"actaForceApplySelfEvent({task.id})" in body
        assert "@htmx:before-request" in body or "x-on:htmx:before-request" in body


@pytest.mark.django_db
class TestSetTaskSize:
    """``POST /projects/<slug>/<number>/size/`` sets/clears the story-point size."""

    def _url(self, project, task):
        return reverse("web:set_task_size", kwargs={"slug_prefix": project.slug_prefix, "number": task.number})

    def test_valid_change(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"size": "5"})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.size == 5

    def test_clear_size(self, client, setup):
        user, project, task = setup
        task.size = 8
        task.save(update_fields=["size"])
        client.force_login(user)
        resp = client.post(self._url(project, task), {"size": ""})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.size is None

    def test_non_fibonacci_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"size": "7"})
        assert resp.status_code == 400
        task.refresh_from_db()
        assert task.size is None

    def test_dropdown_panel_opts_into_force_apply_self_event(self, client, setup):
        """Mirror of the status-cell opt-in — see TestSetTaskStatus."""
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"size": "5"})
        assert resp.status_code == 200
        body = resp.content.decode()
        assert f"actaForceApplySelfEvent({task.id})" in body
        assert "@htmx:before-request" in body or "x-on:htmx:before-request" in body

    def test_non_int_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        assert client.post(self._url(project, task), {"size": "abc"}).status_code == 400


@pytest.mark.django_db
class TestSetTaskDueDate:
    """``POST /projects/<slug>/<number>/due-date/`` updates the deadline."""

    def _url(self, project, task):
        return reverse(
            "web:set_task_due_date",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_set_due_date_from_empty(self, client, setup):
        user, project, task = setup
        assert task.due_date is None
        client.force_login(user)
        resp = client.post(self._url(project, task), {"due_date": "2026-12-31"})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.due_date == datetime.date(2026, 12, 31)
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.due_changed")
        assert events.count() == 1
        assert events.get().payload == {"from": None, "to": "2026-12-31"}

    def test_update_existing_due_date(self, client, setup):
        user, project, task = setup
        task.due_date = datetime.date(2026, 1, 1)
        task.save()
        client.force_login(user)
        resp = client.post(self._url(project, task), {"due_date": "2026-06-15"})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.due_date == datetime.date(2026, 6, 15)
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.due_changed")
        assert events.get().payload == {"from": "2026-01-01", "to": "2026-06-15"}

    def test_clear_due_date(self, client, setup):
        user, project, task = setup
        task.due_date = datetime.date(2026, 1, 1)
        task.save()
        client.force_login(user)
        resp = client.post(self._url(project, task), {"due_date": ""})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.due_date is None
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.due_changed")
        assert events.get().payload == {"from": "2026-01-01", "to": None}

    def test_invalid_format_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"due_date": "31/12/2026"})
        assert resp.status_code == 400
        task.refresh_from_db()
        assert task.due_date is None

    def test_response_is_fragment_with_oob_activity(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"due_date": "2026-12-31"})
        body = resp.content.decode()
        assert "<html" not in body
        assert 'id="due-date-cell"' in body
        # OOB activity swap is included.
        assert "hx-swap-oob" in body

    def test_fires_task_changed_so_panels_refetch(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"due_date": "2026-12-31"})
        # The view panel (timeline Gantt, date-sorted lists) refetches on this
        # trigger so a deadline edit redraws without a reload.
        assert resp["HX-Trigger"] == "acta:task-changed"

    def test_cross_workspace_user_gets_404(self, client, setup):
        user, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        client.force_login(user)
        resp = client.post(
            reverse(
                "web:set_task_due_date",
                kwargs={
                    "slug_prefix": foreign_project.slug_prefix,
                    "number": foreign_task.number,
                },
            ),
            {"due_date": "2026-12-31"},
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestSetTaskStartDate:
    """``POST /projects/<slug>/<number>/start-date/`` sets the start date.

    Exercised by the timeline drag-resize handler (raw ``fetch``); the view
    returns an empty 200 plus the panel-refetch trigger.
    """

    def _url(self, project, task):
        return reverse(
            "web:set_task_start_date",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_set_start_date(self, client, setup):
        user, project, task = setup
        assert task.start_date is None
        client.force_login(user)
        resp = client.post(self._url(project, task), {"start_date": "2026-03-01"})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.start_date == datetime.date(2026, 3, 1)

    def test_clear_start_date(self, client, setup):
        user, project, task = setup
        task.start_date = datetime.date(2026, 1, 1)
        task.save()
        client.force_login(user)
        resp = client.post(self._url(project, task), {"start_date": ""})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.start_date is None

    def test_invalid_format_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"start_date": "01/03/2026"})
        assert resp.status_code == 400

    def test_fires_task_changed_so_panels_refetch(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"start_date": "2026-03-01"})
        assert resp["HX-Trigger"] == "acta:task-changed"


@pytest.mark.django_db
class TestSetTaskAssignee:
    """``POST /projects/<slug>/<number>/assignee/`` sets the assignee."""

    def _url(self, project, task):
        return reverse(
            "web:set_task_assignee",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_assign_member(self, client, setup):
        user, project, task = setup
        member = UserFactory()
        WorkspaceMemberFactory(workspace=project.workspace, user=member)
        client.force_login(user)
        resp = client.post(self._url(project, task), {"assignee_id": member.id})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.assignee == member
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.assigned")
        assert events.count() == 1
        assert events.get().payload == {"from_user_id": None, "to_user_id": member.id}

    def test_reassign_to_different_member(self, client, setup):
        user, project, task = setup
        first = UserFactory()
        second = UserFactory()
        WorkspaceMemberFactory(workspace=project.workspace, user=first)
        WorkspaceMemberFactory(workspace=project.workspace, user=second)
        task.assignee = first
        task.save()
        client.force_login(user)
        resp = client.post(self._url(project, task), {"assignee_id": second.id})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.assignee == second
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.assigned")
        assert events.get().payload == {"from_user_id": first.id, "to_user_id": second.id}

    def test_unassign(self, client, setup):
        user, project, task = setup
        member = UserFactory()
        WorkspaceMemberFactory(workspace=project.workspace, user=member)
        task.assignee = member
        task.save()
        client.force_login(user)
        resp = client.post(self._url(project, task), {"assignee_id": ""})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.assignee is None
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.assigned")
        assert events.get().payload == {"from_user_id": member.id, "to_user_id": None}

    def test_non_member_user_returns_400(self, client, setup):
        user, project, task = setup
        stranger = UserFactory()
        client.force_login(user)
        resp = client.post(self._url(project, task), {"assignee_id": stranger.id})
        assert resp.status_code == 400
        task.refresh_from_db()
        assert task.assignee is None

    def test_nonexistent_user_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"assignee_id": "999999"})
        assert resp.status_code == 400

    def test_non_int_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"assignee_id": "lol"})
        assert resp.status_code == 400

    def test_response_fragment_with_oob_activity(self, client, setup):
        user, project, task = setup
        member = UserFactory()
        WorkspaceMemberFactory(workspace=project.workspace, user=member)
        client.force_login(user)
        resp = client.post(self._url(project, task), {"assignee_id": member.id})
        body = resp.content.decode()
        assert "<html" not in body
        assert 'id="assignee-cell"' in body
        assert "hx-swap-oob" in body
        # Activity OOB block shows the resolved usernames for from→to.
        assert member.username in body

    def test_cross_workspace_user_gets_404(self, client, setup):
        user, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        client.force_login(user)
        resp = client.post(
            reverse(
                "web:set_task_assignee",
                kwargs={
                    "slug_prefix": foreign_project.slug_prefix,
                    "number": foreign_task.number,
                },
            ),
            {"assignee_id": foreign_ws.owner.id},
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestSetTaskTitle:
    """``POST /projects/<slug>/<number>/title/`` renames the task."""

    def _url(self, project, task):
        return reverse(
            "web:set_task_title",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_rename_emits_task_updated_event(self, client, setup):
        user, project, task = setup
        original = task.title
        client.force_login(user)
        resp = client.post(self._url(project, task), {"title": "Renamed via inline edit"})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.title == "Renamed via inline edit"
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.updated")
        assert events.count() == 1
        payload = events.get().payload
        assert payload["changes"]["title"] == {"old": original, "new": "Renamed via inline edit"}

    def test_title_is_stripped(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"title": "   spaced out   "})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.title == "spaced out"

    def test_empty_title_returns_400(self, client, setup):
        user, project, task = setup
        original = task.title
        client.force_login(user)
        resp = client.post(self._url(project, task), {"title": ""})
        assert resp.status_code == 400
        task.refresh_from_db()
        assert task.title == original

    def test_whitespace_only_title_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"title": "   "})
        assert resp.status_code == 400

    def test_overlong_title_returns_400(self, client, setup):
        user, project, task = setup
        original = task.title
        client.force_login(user)
        resp = client.post(self._url(project, task), {"title": "x" * 201})
        assert resp.status_code == 400
        task.refresh_from_db()
        assert task.title == original

    def test_response_fragment_with_oob_activity(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"title": "New title"})
        body = resp.content.decode()
        assert "<html" not in body
        assert 'id="title-cell"' in body
        assert "hx-swap-oob" in body
        assert "New title" in body

    def test_cross_workspace_user_gets_404(self, client, setup):
        user, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        client.force_login(user)
        resp = client.post(
            reverse(
                "web:set_task_title",
                kwargs={
                    "slug_prefix": foreign_project.slug_prefix,
                    "number": foreign_task.number,
                },
            ),
            {"title": "Leaked"},
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestSetTaskDescription:
    """``POST /projects/<slug>/<number>/description/`` updates description."""

    def _url(self, project, task):
        return reverse(
            "web:set_task_description",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_set_description_from_empty(self, client, setup):
        user, project, task = setup
        assert task.description == ""
        client.force_login(user)
        resp = client.post(self._url(project, task), {"description": "## Heading\n\nText"})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.description == "## Heading\n\nText"
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.updated")
        assert events.count() == 1
        payload = events.get().payload
        assert "description" in payload["changes"]
        assert payload["changes"]["description"]["new_len"] == len("## Heading\n\nText")

    def test_clear_description(self, client, setup):
        user, project, task = setup
        task.description = "Original markdown"
        task.save()
        client.force_login(user)
        resp = client.post(self._url(project, task), {"description": ""})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.description == ""

    def test_response_oob_activity_only(self, client, setup):
        """Description save returns *only* the OOB activity fragment.

        Re-rendering the description cell would force TipTap to
        unmount and re-mount, causing a visible scroll hop on a long
        task page (see the endpoint docstring for the rationale).
        The editor JS bumps ``data-baseline`` client-side so the cell
        stays in sync without a swap.
        """
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"description": "New body"})
        body = resp.content.decode()
        assert "<html" not in body
        # Description cell HTML must NOT be in the response — only
        # the OOB activity list is returned now.
        assert 'id="description-cell"' not in body
        assert "hx-swap-oob" in body

    def test_cross_workspace_user_gets_404(self, client, setup):
        user, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        client.force_login(user)
        resp = client.post(
            reverse(
                "web:set_task_description",
                kwargs={
                    "slug_prefix": foreign_project.slug_prefix,
                    "number": foreign_task.number,
                },
            ),
            {"description": "leaked"},
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestToggleTaskLabel:
    """``POST /projects/<slug>/<number>/labels/toggle/`` attaches/detaches."""

    def _url(self, project, task):
        return reverse(
            "web:toggle_task_label",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_attach_new_label(self, client, setup):
        user, project, task = setup
        label = LabelFactory(workspace=project.workspace)
        client.force_login(user)
        resp = client.post(self._url(project, task), {"label_id": label.id})
        assert resp.status_code == 200
        assert list(task.labels.values_list("id", flat=True)) == [label.id]
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.labels_changed")
        assert events.count() == 1
        assert events.get().payload == {"added_ids": [label.id], "removed_ids": []}

    def test_detach_existing_label(self, client, setup):
        user, project, task = setup
        label = LabelFactory(workspace=project.workspace)
        task.labels.add(label)
        client.force_login(user)
        resp = client.post(self._url(project, task), {"label_id": label.id})
        assert resp.status_code == 200
        assert task.labels.count() == 0
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.labels_changed")
        assert events.get().payload == {"added_ids": [], "removed_ids": [label.id]}

    def test_attach_second_label_keeps_first(self, client, setup):
        user, project, task = setup
        first = LabelFactory(workspace=project.workspace)
        second = LabelFactory(workspace=project.workspace)
        task.labels.add(first)
        client.force_login(user)
        resp = client.post(self._url(project, task), {"label_id": second.id})
        assert resp.status_code == 200
        assert set(task.labels.values_list("id", flat=True)) == {first.id, second.id}

    def test_foreign_workspace_label_returns_400(self, client, setup):
        user, project, task = setup
        foreign_ws = WorkspaceFactory()
        foreign_label = LabelFactory(workspace=foreign_ws)
        client.force_login(user)
        resp = client.post(self._url(project, task), {"label_id": foreign_label.id})
        assert resp.status_code == 400
        assert task.labels.count() == 0

    def test_nonexistent_label_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"label_id": "999999"})
        assert resp.status_code == 400

    def test_non_int_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"label_id": "abc"})
        assert resp.status_code == 400

    def test_response_fragment_with_oob_activity(self, client, setup):
        user, project, task = setup
        label = LabelFactory(workspace=project.workspace, name="backend")
        client.force_login(user)
        resp = client.post(self._url(project, task), {"label_id": label.id})
        body = resp.content.decode()
        assert "<html" not in body
        # Primary swap target: the trigger inner content, NOT the whole
        # cell (so the outer Alpine wrapper survives consecutive clicks).
        assert 'id="labels-trigger-inner"' in body
        # OOB: dropdown rows refresh ✓ marks, and the activity timeline.
        assert 'id="labels-dropdown-inner"' in body
        assert "hx-swap-oob" in body
        assert "backend" in body

    def test_cross_workspace_task_returns_404(self, client, setup):
        user, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        foreign_label = LabelFactory(workspace=foreign_ws)
        client.force_login(user)
        resp = client.post(
            reverse(
                "web:toggle_task_label",
                kwargs={
                    "slug_prefix": foreign_project.slug_prefix,
                    "number": foreign_task.number,
                },
            ),
            {"label_id": foreign_label.id},
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestPostComment:
    """``POST /projects/<slug>/<number>/comments/`` creates a comment."""

    def _url(self, project, task):
        return reverse(
            "web:post_comment",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_creates_comment_and_event(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"body": "looks great"})
        assert resp.status_code == 200
        comment = Comment.objects.get(task=task)
        assert comment.author == user
        assert comment.body == "looks great"
        body = resp.content.decode()
        assert "looks great" in body
        events = ActivityLog.objects.filter(
            target_type=ActivityLog.TARGET_COMMENT,
            target_id=comment.id,
            event_type="comment.created",
        )
        assert events.count() == 1

    def test_empty_body_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"body": "   "})
        assert resp.status_code == 400
        assert Comment.objects.filter(task=task).count() == 0

    def test_foreign_task_returns_404(self, client, setup):
        user, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        client.force_login(user)
        resp = client.post(
            reverse(
                "web:post_comment",
                kwargs={
                    "slug_prefix": foreign_project.slug_prefix,
                    "number": foreign_task.number,
                },
            ),
            {"body": "leaked"},
        )
        assert resp.status_code == 404
        assert Comment.objects.filter(task=foreign_task).count() == 0

    def test_reply_sets_parent(self, client, setup):
        user, project, task = setup
        top = Comment.objects.create(task=task, author=user, body="top")
        client.force_login(user)
        resp = client.post(self._url(project, task) + f"?parent={top.id}", {"body": "a reply"})
        assert resp.status_code == 200
        reply = Comment.objects.get(task=task, parent=top)
        assert reply.task_id == task.id
        assert reply.body == "a reply"
        assert "a reply" in resp.content.decode()

    def test_reply_to_reply_rejected(self, client, setup):
        user, project, task = setup
        top = Comment.objects.create(task=task, author=user, body="top")
        reply = Comment.objects.create(task=task, author=user, parent=top, body="r1")
        client.force_login(user)
        resp = client.post(self._url(project, task) + f"?parent={reply.id}", {"body": "nested"})
        assert resp.status_code == 400

    def test_reply_to_foreign_task_comment_rejected(self, client, setup):
        user, project, task = setup
        other_task = TaskFactory(project=project, reporter=user)
        foreign_parent = Comment.objects.create(task=other_task, author=user, body="elsewhere")
        client.force_login(user)
        resp = client.post(self._url(project, task) + f"?parent={foreign_parent.id}", {"body": "x"})
        assert resp.status_code == 400


@pytest.mark.django_db
class TestCommentReplyForm:
    """``GET .../comments/<id>/reply-form/`` returns the lazy reply composer."""

    def _url(self, project, task, comment):
        return reverse(
            "web:comment_reply_form",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number, "comment_id": comment.id},
        )

    def test_returns_composer(self, client, setup):
        user, project, task = setup
        top = Comment.objects.create(task=task, author=user, body="top")
        client.force_login(user)
        resp = client.get(self._url(project, task, top))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "data-description-editor" in body
        assert f"parent={top.id}" in body

    def test_non_top_level_parent_404(self, client, setup):
        user, project, task = setup
        top = Comment.objects.create(task=task, author=user, body="top")
        reply = Comment.objects.create(task=task, author=user, parent=top, body="r1")
        client.force_login(user)
        resp = client.get(self._url(project, task, reply))
        assert resp.status_code == 404

    def test_foreign_task_404(self, client, setup):
        user, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        foreign_comment = Comment.objects.create(task=foreign_task, author=foreign_ws.owner, body="x")
        client.force_login(user)
        resp = client.get(self._url(foreign_project, foreign_task, foreign_comment))
        assert resp.status_code == 404


@pytest.mark.django_db
class TestEditDeleteComment:
    """Edit + delete on task comments (author or workspace admin/owner)."""

    def _edit_url(self, project, task, comment):
        return reverse("web:edit_comment", kwargs={"comment_id": comment.id})

    def _delete_url(self, project, task, comment):
        return reverse("web:delete_comment", kwargs={"comment_id": comment.id})

    def test_author_edits_comment(self, client, setup):
        user, project, task = setup
        c = Comment.objects.create(task=task, author=user, body="orig")
        client.force_login(user)
        resp = client.post(self._edit_url(project, task, c), {"body": "updated body"})
        assert resp.status_code == 200
        c.refresh_from_db()
        assert c.body == "updated body"
        assert "updated body" in resp.content.decode()
        assert ActivityLog.objects.filter(event_type="comment.edited", target_id=c.id).count() == 1

    def test_edit_empty_body_400(self, client, setup):
        user, project, task = setup
        c = Comment.objects.create(task=task, author=user, body="orig")
        client.force_login(user)
        resp = client.post(self._edit_url(project, task, c), {"body": "  "})
        assert resp.status_code == 400
        c.refresh_from_db()
        assert c.body == "orig"

    def test_non_author_member_cannot_edit(self, client, setup):
        user, project, task = setup
        c = Comment.objects.create(task=task, author=user, body="orig")
        member = UserFactory()
        WorkspaceMemberFactory(workspace=project.workspace, user=member)
        client.force_login(member)
        resp = client.post(self._edit_url(project, task, c), {"body": "hax"})
        assert resp.status_code == 403
        c.refresh_from_db()
        assert c.body == "orig"

    def test_workspace_admin_can_edit_others_comment(self, client, setup):
        user, project, task = setup
        c = Comment.objects.create(task=task, author=user, body="orig")
        admin = UserFactory()
        WorkspaceMemberFactory(workspace=project.workspace, user=admin, role=WorkspaceMember.ADMIN)
        client.force_login(admin)
        resp = client.post(self._edit_url(project, task, c), {"body": "moderated"})
        assert resp.status_code == 200
        c.refresh_from_db()
        assert c.body == "moderated"

    def test_author_deletes_comment(self, client, setup):
        user, project, task = setup
        c = Comment.objects.create(task=task, author=user, body="bye")
        client.force_login(user)
        resp = client.post(self._delete_url(project, task, c))
        assert resp.status_code == 200
        assert not Comment.objects.filter(id=c.id).exists()
        assert ActivityLog.objects.filter(event_type="comment.deleted", target_id=c.id).count() == 1

    def test_non_author_member_cannot_delete(self, client, setup):
        user, project, task = setup
        c = Comment.objects.create(task=task, author=user, body="keep")
        member = UserFactory()
        WorkspaceMemberFactory(workspace=project.workspace, user=member)
        client.force_login(member)
        resp = client.post(self._delete_url(project, task, c))
        assert resp.status_code == 403
        assert Comment.objects.filter(id=c.id).exists()

    def test_deleted_comment_appears_in_timeline(self, client, setup):
        user, project, task = setup
        c = Comment.objects.create(task=task, author=user, body="trace me")
        client.force_login(user)
        client.post(self._delete_url(project, task, c))
        timeline = _build_timeline(task, user.id)
        deleted_events = [item for kind, item in timeline if kind == "event" and item.event_type == "comment.deleted"]
        assert len(deleted_events) == 1

    def test_deleted_comment_keeps_original_timeline_position(self, client, setup):
        user, project, task = setup
        old = Comment.objects.create(task=task, author=user, body="old")
        old_time = timezone.now() - datetime.timedelta(days=7)
        Comment.objects.filter(pk=old.id).update(created_at=old_time)
        # A newer activity event posted "now", after the old comment.
        log_event(
            workspace=project.workspace,
            project=project,
            actor=user,
            event_type="task.status_changed",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"from": "to-do", "to": "done"},
        )
        client.force_login(user)
        client.post(self._delete_url(project, task, old))
        timeline = _build_timeline(task, user.id)
        types = [item.event_type for kind, item in timeline if kind == "event"]
        # The deletion marker sorts to ~7 days ago (the comment's original
        # time), so it lands BEFORE the recent status change, not at the end.
        assert types.index("comment.deleted") < types.index("task.status_changed")

    def test_edit_form_prefilled(self, client, setup):
        user, project, task = setup
        c = Comment.objects.create(task=task, author=user, body="orig text")
        client.force_login(user)
        resp = client.get(reverse("web:comment_edit_form", kwargs={"comment_id": c.id}))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "orig text" in body
        assert "data-description-editor" in body


@pytest.mark.django_db
class TestTaskRowFragment:
    """``/tasks/<id>/row/?as=table|list`` — per-row SSE refresh endpoint.

    The client-side SSE handler calls this when a peer edits a task,
    swapping just the matching ``<tr>`` / ``<a>`` element on every
    page the task appears on. Returns the table partial by default;
    ``?as=list`` returns the list-row partial.
    """

    def _url(self, task):
        return reverse("web:task_row_fragment", kwargs={"task_id": task.id})

    def test_table_fragment_is_a_tr(self, client, setup):
        user, _, task = setup
        client.force_login(user)
        resp = client.get(self._url(task))
        body = resp.content.decode().strip()
        assert resp.status_code == 200
        assert body.startswith("<tr")
        assert f'data-task-id="{task.id}"' in body
        # ``data-status`` etc. come from ``task_filter_attrs`` — client
        # filter relies on them being present after a per-row swap.
        assert f'data-status="{task.status}"' in body

    def test_list_fragment_is_an_anchor(self, client, setup):
        user, _, task = setup
        client.force_login(user)
        resp = client.get(self._url(task) + "?as=list")
        body = resp.content.decode().strip()
        assert resp.status_code == 200
        assert body.startswith("<a")
        assert f'data-task-id="{task.id}"' in body

    def test_foreign_workspace_task_returns_404(self, client, setup):
        user, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        client.force_login(user)
        resp = client.get(reverse("web:task_row_fragment", kwargs={"task_id": foreign_task.id}))
        assert resp.status_code == 404

    def test_unauthenticated_redirects(self, client, setup):
        _, _, task = setup
        resp = client.get(self._url(task))
        assert resp.status_code in (301, 302)


@pytest.mark.django_db
class TestArchiveTask:
    """Archive / unarchive endpoint — flips ``archived_at`` orthogonal
    to status, emits activity event."""

    def _url(self, project, task):
        return reverse(
            "web:archive_task",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_archive_sets_timestamp_and_logs_event(self, client, setup):
        user, project, task = setup
        assert task.archived_at is None
        client.force_login(user)
        resp = client.post(self._url(project, task))
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.archived_at is not None
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.archived")
        assert events.count() == 1

    def test_archive_preserves_status(self, client, setup):
        """Archive is orthogonal — the prior status (here ``to-do``) survives."""
        user, project, task = setup
        client.force_login(user)
        client.post(self._url(project, task))
        task.refresh_from_db()
        assert task.status == Task.STATUS_TODO

    def test_unarchive_clears_timestamp_and_logs_event(self, client, setup):
        from django.utils import timezone as tz

        user, project, task = setup
        task.archived_at = tz.now()
        task.save(update_fields=["archived_at"])
        client.force_login(user)
        resp = client.post(self._url(project, task), {"unarchive": "1"})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.archived_at is None
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.unarchived")
        assert events.count() == 1

    def test_double_archive_returns_400(self, client, setup):
        from django.utils import timezone as tz

        user, project, task = setup
        task.archived_at = tz.now()
        task.save(update_fields=["archived_at"])
        client.force_login(user)
        resp = client.post(self._url(project, task))
        assert resp.status_code == 400

    def test_unarchive_when_active_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"unarchive": "1"})
        assert resp.status_code == 400


@pytest.mark.django_db
class TestCancelTask:
    """Cancel / reopen endpoint — sets the terminal ``cancelled`` status
    (and back to ``to-do``) via the standard status-change diff path."""

    def _url(self, project, task):
        return reverse(
            "web:cancel_task",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_cancel_sets_status_and_logs_status_change(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task))
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.status == Task.STATUS_CANCELLED
        evt = ActivityLog.objects.get(target_id=task.id, event_type="task.status_changed")
        assert evt.payload["to"] == Task.STATUS_CANCELLED

    def test_reopen_returns_to_todo(self, client, setup):
        user, project, task = setup
        task.status = Task.STATUS_CANCELLED
        task.save(update_fields=["status"])
        client.force_login(user)
        resp = client.post(self._url(project, task), {"reopen": "1"})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.status == Task.STATUS_TODO

    def test_double_cancel_returns_400(self, client, setup):
        user, project, task = setup
        task.status = Task.STATUS_CANCELLED
        task.save(update_fields=["status"])
        client.force_login(user)
        assert client.post(self._url(project, task)).status_code == 400

    def test_reopen_when_active_returns_400(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        assert client.post(self._url(project, task), {"reopen": "1"}).status_code == 400


@pytest.mark.django_db
class TestSetTaskProject:
    """Move-task endpoint — reassigns project, renumbers, cascades subtasks."""

    def _url(self, project, task):
        return reverse(
            "web:set_task_project",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_move_reassigns_and_renumbers(self, client, setup):
        user, project, task = setup
        dst = ProjectFactory(workspace=project.workspace)
        client.force_login(user)
        resp = client.post(self._url(project, task), {"project_id": str(dst.id)})
        assert resp.status_code == 204
        assert f"/projects/{dst.slug_prefix}/" in resp["HX-Location"]
        task.refresh_from_db()
        assert task.project_id == dst.id
        evt = ActivityLog.objects.get(target_id=task.id, event_type="task.project_changed")
        assert evt.payload["to_project_id"] == dst.id

    def test_move_cascades_subtasks(self, client, setup):
        user, project, task = setup
        child = TaskFactory(project=project, parent=task, reporter=user)
        dst = ProjectFactory(workspace=project.workspace)
        client.force_login(user)
        resp = client.post(self._url(project, task), {"project_id": str(dst.id)})
        assert resp.status_code == 204
        child.refresh_from_db()
        assert child.project_id == dst.id
        assert child.parent_id == task.id

    def test_subtask_cannot_move_alone(self, client, setup):
        user, project, task = setup
        child = TaskFactory(project=project, parent=task, reporter=user)
        dst = ProjectFactory(workspace=project.workspace)
        client.force_login(user)
        resp = client.post(self._url(project, child), {"project_id": str(dst.id)})
        assert resp.status_code == 400

    def test_cross_workspace_target_404(self, client, setup):
        user, project, task = setup
        other_ws = WorkspaceFactory()
        foreign = ProjectFactory(workspace=other_ws)
        client.force_login(user)
        resp = client.post(self._url(project, task), {"project_id": str(foreign.id)})
        assert resp.status_code == 404


@pytest.mark.django_db
class TestDeleteTask:
    """Hard-delete endpoint — removes the task + subtasks, logs task.deleted."""

    def _url(self, project, task):
        return reverse(
            "web:delete_task",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_delete_removes_task_and_logs_event(self, client, setup):
        user, project, task = setup
        task_id = task.id
        client.force_login(user)
        resp = client.post(self._url(project, task))
        assert resp.status_code == 204
        assert not Task.objects.filter(id=task_id).exists()
        evt = ActivityLog.objects.get(target_id=task_id, event_type="task.deleted")
        assert evt.payload["snapshot"]["number"] == task.number

    def test_delete_cascades_subtasks(self, client, setup):
        user, project, task = setup
        child = TaskFactory(project=project, parent=task, reporter=user)
        child_id = child.id
        client.force_login(user)
        resp = client.post(self._url(project, task))
        assert resp.status_code == 204
        assert not Task.objects.filter(id=child_id).exists()
        assert ActivityLog.objects.filter(target_id=child_id, event_type="task.deleted").exists()


@pytest.mark.django_db
class TestContextMenu:
    """The right-click menu fragment renders for a task with its actions."""

    def _url(self, project, task):
        return reverse(
            "web:task_context_menu",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_renders_actions(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.get(self._url(project, task))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert task.slug in body
        assert "Set status" in body
        assert "Delete" in body
        assert "Move to project" in body  # top-level task → move row present

    def test_subtask_hides_move(self, client, setup):
        user, project, task = setup
        child = TaskFactory(project=project, parent=task, reporter=user)
        client.force_login(user)
        resp = client.get(self._url(project, child))
        assert resp.status_code == 200
        assert "Move to project" not in resp.content.decode()

    def test_no_n_plus_one(self, client, setup, django_assert_max_num_queries):
        """Menu render stays constant-query as members / projects / labels grow."""
        user, project, task = setup
        for _ in range(6):
            WorkspaceMemberFactory(workspace=project.workspace)
        for _ in range(6):
            ProjectFactory(workspace=project.workspace)
        for _ in range(6):
            LabelFactory(workspace=project.workspace)
        client.force_login(user)
        # task + members + projects + labels + attached-ids + auth/session/
        # workspace-context queries — all constant, none scaling with rows.
        with django_assert_max_num_queries(15):
            resp = client.get(self._url(project, task))
        assert resp.status_code == 200


@pytest.mark.django_db
class TestBulkContextMenu:
    """The selection (bulk) menu fragment renders with workspace pickers."""

    def test_renders_bulk_actions(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.get(reverse("web:bulk_context_menu"))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Set status" in body
        assert "Move to project" in body
        assert "Delete" in body
        # project from the active workspace surfaces in the move picker
        assert project.slug_prefix in body

    def test_no_n_plus_one(self, client, setup, django_assert_max_num_queries):
        user, project, task = setup
        for _ in range(6):
            WorkspaceMemberFactory(workspace=project.workspace)
        for _ in range(6):
            ProjectFactory(workspace=project.workspace)
        for _ in range(6):
            LabelFactory(workspace=project.workspace)
        client.force_login(user)
        with django_assert_max_num_queries(15):
            resp = client.get(reverse("web:bulk_context_menu"))
        assert resp.status_code == 200


@pytest.mark.django_db
class TestWorkspaceWip:
    """Workspace-level WIP policy: save endpoint + column / personal math."""

    def _url(self, ws):
        return reverse("web:set_workspace_wip", kwargs={"slug": ws.slug})

    def test_save_policy_admin(self, client, setup):
        user, project, task = setup  # ws.owner == user, an admin
        ws = project.workspace
        client.force_login(user)
        resp = client.post(self._url(ws), {"mode": "personal", "limit_in-progress": "2", "limit_to-do": "0"})
        assert resp.status_code == 302
        ws.refresh_from_db()
        assert ws.wip_limits == {"mode": "personal", "limits": {"in-progress": 2}}
        assert ws.wip_config() == ("personal", {"in-progress": 2})

    def test_save_policy_via_htmx_swaps_in_place(self, client, setup):
        """An HTMX save returns the WIP card partial + a toast, not a redirect."""
        user, project, _task = setup
        ws = project.workspace
        client.force_login(user)
        resp = client.post(
            self._url(ws),
            {"mode": "personal", "limit_in-progress": "2"},
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 200
        assert b'id="workspace-wip"' in resp.content
        assert "acta:toast" in resp.headers.get("HX-Trigger", "")
        ws.refresh_from_db()
        assert ws.wip_config() == ("personal", {"in-progress": 2})

    def test_non_admin_forbidden(self, client, setup):
        _user, project, _task = setup
        ws = project.workspace
        intruder = UserFactory()
        WorkspaceMemberFactory(workspace=ws, user=intruder)  # plain member
        client.force_login(intruder)
        assert client.post(self._url(ws), {"mode": "personal", "limit_in-progress": "2"}).status_code == 403

    def test_column_mode_over_limit(self):
        from apps.web.views import _build_kanban_columns

        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        tasks = [TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS, reporter=ws.owner) for _ in range(6)]
        cols = {c["key"]: c for c in _build_kanban_columns(tasks, wip_mode="column", wip_limits={"in-progress": 5})}
        assert cols["in-progress"]["over_limit"] is True
        assert cols["in-progress"]["fill_pct"] == 100
        assert cols["to-do"]["over_limit"] is False

    def test_personal_over_warning_renders_on_board(self, client):
        """End-to-end: the kanban shows the over-WIP warning for the column."""
        ws = WorkspaceFactory()
        ws.wip_limits = {"mode": "personal", "limits": {"in-progress": 2}}
        ws.save(update_fields=["wip_limits"])
        project = ProjectFactory(workspace=ws)
        for _ in range(3):
            TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS, assignee=ws.owner, reporter=ws.owner)
        client.force_login(ws.owner)
        resp = client.get(reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}) + "?view=kanban")
        assert resp.status_code == 200
        assert "over WIP" in resp.content.decode()

    def test_personal_mode_over_members(self):
        from apps.web.views import _build_kanban_columns, _wip_context

        ws = WorkspaceFactory()
        ws.wip_limits = {"mode": "personal", "limits": {"in-progress": 2}}
        ws.save(update_fields=["wip_limits"])
        project = ProjectFactory(workspace=ws)
        # owner gets 3 in-progress → over the cap of 2
        for _ in range(3):
            TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS, assignee=ws.owner, reporter=ws.owner)
        mode, limits, over = _wip_context(ws)
        assert mode == "personal"
        assert over["in-progress"][ws.owner.id] == 3
        cols = {
            c["key"]: c
            for c in _build_kanban_columns(
                list(Task.objects.filter(project=project)),
                wip_mode=mode,
                over_by_status=over,
            )
        }
        assert cols["in-progress"]["over_member_count"] == 1


@pytest.mark.django_db
class TestArchivedFilter:
    """``apply_task_filters`` hides archived rows unless
    ``?show_archived=1``."""

    def test_archived_hidden_by_default(self, client, setup):
        from django.utils import timezone as tz

        user, project, _ = setup
        TaskFactory(project=project, reporter=user, title="active-task", status=Task.STATUS_TODO)
        archived = TaskFactory(project=project, reporter=user, title="dusty-task", status=Task.STATUS_DONE)
        archived.archived_at = tz.now()
        archived.save(update_fields=["archived_at"])
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks"))
        body = resp.content.decode()
        assert "active-task" in body
        assert "dusty-task" not in body

    def test_show_archived_param_includes_archived(self, client, setup):
        from django.utils import timezone as tz

        user, project, _ = setup
        archived = TaskFactory(project=project, reporter=user, title="dusty-task", status=Task.STATUS_DONE)
        archived.archived_at = tz.now()
        archived.save(update_fields=["archived_at"])
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?show_archived=1")
        assert "dusty-task" in resp.content.decode()

    def test_show_archived_persists_to_cookie(self, client, setup):
        """Toggling Show archived ON sets the cookie so subsequent
        navigation keeps archived rows visible."""
        user, project, _ = setup
        from django.utils import timezone as tz

        archived = TaskFactory(project=project, reporter=user, title="dusty-task", status=Task.STATUS_DONE)
        archived.archived_at = tz.now()
        archived.save(update_fields=["archived_at"])
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?show_archived=1")
        assert resp.cookies["acta_show_archived"].value == "1"
        # Next request without the querystring — cookie carries it.
        resp2 = client.get(reverse("web:all_tasks"))
        assert "dusty-task" in resp2.content.decode()

    def test_show_archived_can_be_turned_off_when_cookie_is_on(self, client, setup):
        """Toggling OFF (form sends ``?show_archived=0`` via the hidden
        input) must override an existing ``=1`` cookie. Regression for
        the bug where the toggle became one-way."""
        from django.utils import timezone as tz

        user, project, _ = setup
        archived = TaskFactory(project=project, reporter=user, title="dusty-task", status=Task.STATUS_DONE)
        archived.archived_at = tz.now()
        archived.save(update_fields=["archived_at"])
        client.cookies["acta_show_archived"] = "1"
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?show_archived=0")
        assert "dusty-task" not in resp.content.decode()
        assert resp.cookies["acta_show_archived"].value == "0"

    def test_active_filter_count_badge_oob_included_in_htmx_response(self, client, setup):
        """HTMX responses must carry the OOB-marked filter-count badges
        so the sidebar header refreshes without re-rendering the whole
        sidebar. Counter increments visibly when a filter activates."""
        import re

        user, project, _ = setup
        TaskFactory(project=project, reporter=user, title="t1", status=Task.STATUS_TODO)
        client.force_login(user)

        def find_badge(body, badge_id):
            """Return the matched ``<span id=badge_id …>…</span>`` element."""
            return re.search(
                r'<span\s+id="' + re.escape(badge_id) + r'"[^>]*>\s*([^<]*?)\s*</span>',
                body,
                re.S,
            )

        # No active filters: both badge spans exist (for OOB targeting)
        # but carry the ``hidden`` class and empty content.
        resp_empty = client.get(reverse("web:all_tasks"), HTTP_HX_REQUEST="true")
        body_empty = resp_empty.content.decode()
        assert 'hx-swap-oob="outerHTML"' in body_empty
        collapsed_empty = find_badge(body_empty, "filter-count-collapsed")
        expanded_empty = find_badge(body_empty, "filter-count-expanded")
        assert collapsed_empty and "hidden" in collapsed_empty.group(0)
        assert expanded_empty and "hidden" in expanded_empty.group(0)
        assert collapsed_empty.group(1) == ""
        assert expanded_empty.group(1) == ""

        # Activate one filter — badges drop ``hidden`` and show the count.
        resp = client.get(
            reverse("web:all_tasks") + "?status=to-do",
            HTTP_HX_REQUEST="true",
        )
        body = resp.content.decode()
        collapsed = find_badge(body, "filter-count-collapsed")
        expanded = find_badge(body, "filter-count-expanded")
        assert collapsed and "hidden" not in collapsed.group(0)
        assert expanded and "hidden" not in expanded.group(0)
        assert collapsed.group(1) == "1"
        assert expanded.group(1) == "1"

    def test_show_archived_hidden_input_and_checkbox_both_sent(self, client, setup):
        """Form layout sends ``show_archived`` twice — the hidden ``0``
        first, then the checked ``1``. Server must take the trailing
        ``1`` as the truth.

        Direct simulation of how ``_filters_sidebar.html`` posts.
        """
        from django.utils import timezone as tz

        user, project, _ = setup
        archived = TaskFactory(project=project, reporter=user, title="dusty-task", status=Task.STATUS_DONE)
        archived.archived_at = tz.now()
        archived.save(update_fields=["archived_at"])
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?show_archived=0&show_archived=1")
        assert "dusty-task" in resp.content.decode()
        assert resp.cookies["acta_show_archived"].value == "1"


@pytest.mark.django_db
class TestDateEditPermission:
    """Only the assignee (or anyone on an unassigned task) may set start/end."""

    def _ws(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        assignee = ws.owner
        other = WorkspaceMemberFactory(workspace=ws).user
        return ws, project, assignee, other

    def _start_url(self, task):
        return reverse(
            "web:set_task_start_date",
            kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
        )

    def _end_url(self, task):
        return reverse(
            "web:set_task_end_date",
            kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
        )

    def test_non_assignee_cannot_set_start_date(self, client):
        ws, project, assignee, other = self._ws()
        task = TaskFactory(project=project, assignee=assignee)
        client.force_login(other)
        resp = client.post(self._start_url(task), {"start_date": "2026-06-01"})
        assert resp.status_code == 403
        task.refresh_from_db()
        assert task.start_date is None

    def test_non_assignee_cannot_set_end_date(self, client):
        ws, project, assignee, other = self._ws()
        task = TaskFactory(project=project, assignee=assignee)
        client.force_login(other)
        resp = client.post(self._end_url(task), {"end_date": "2026-06-01"})
        assert resp.status_code == 403
        task.refresh_from_db()
        assert task.end_date is None

    def test_assignee_can_set_start_date(self, client):
        ws, project, assignee, other = self._ws()
        task = TaskFactory(project=project, assignee=assignee)
        client.force_login(assignee)
        resp = client.post(self._start_url(task), {"start_date": "2026-06-01"})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.start_date == datetime.date(2026, 6, 1)

    def test_anyone_can_set_dates_on_unassigned_task(self, client):
        ws, project, assignee, other = self._ws()
        task = TaskFactory(project=project, assignee=None)
        client.force_login(other)
        resp = client.post(self._end_url(task), {"end_date": "2026-06-02"})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.end_date == datetime.date(2026, 6, 2)


@pytest.mark.django_db
class TestInlineCellPropagationOptIn:
    """Every inline cell that mutates a task must opt the task into the
    self-event force-apply set so the SSE swap reaches the surrounding
    row / card / list item (otherwise the modal change leaves the row
    behind stale until a hard reload).

    Reference pattern: ``_status_cell.html`` (covered by
    ``TestSetTaskStatus.test_dropdown_panel_opts_into_force_apply_self_event``).
    This class covers the remaining cells from the sweep — see
    ``project_todo_inline_cells_propagation``.

    Each test posts the cell endpoint and asserts the response carries
    both the call site ``actaForceApplySelfEvent(<id>)`` and an
    Alpine-bound ``htmx:before-request`` event handler.
    """

    def _assert_opt_in(self, body, task_id):
        assert f"actaForceApplySelfEvent({task_id})" in body, "missing opt-in call"
        assert "@htmx:before-request" in body or "x-on:htmx:before-request" in body, "missing htmx:before-request hook"

    def _due_url(self, project, task):
        return reverse(
            "web:set_task_due_date",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def _start_url(self, project, task):
        return reverse(
            "web:set_task_start_date",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def _end_url(self, project, task):
        return reverse(
            "web:set_task_end_date",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def _cycle_url(self, project, task):
        return reverse(
            "web:set_task_cycle",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def _project_url(self, project, task):
        return reverse(
            "web:set_task_project",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def _assignee_url(self, project, task):
        return reverse(
            "web:set_task_assignee",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_due_date_form_opts_in(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._due_url(project, task), {"due_date": "2026-12-31"})
        assert resp.status_code == 200
        self._assert_opt_in(resp.content.decode(), task.id)

    def test_start_date_form_opts_in(self, client, setup):
        user, project, task = setup
        # Start/End cells gate on assignee — claim it to render the editable
        # form rather than the read-only sibling branch.
        task.assignee = user
        task.save(update_fields=["assignee"])
        client.force_login(user)
        resp = client.post(self._start_url(project, task), {"start_date": "2026-06-01"})
        assert resp.status_code == 200
        self._assert_opt_in(resp.content.decode(), task.id)

    def test_end_date_form_opts_in(self, client, setup):
        user, project, task = setup
        task.assignee = user
        task.save(update_fields=["assignee"])
        client.force_login(user)
        resp = client.post(self._end_url(project, task), {"end_date": "2026-06-30"})
        assert resp.status_code == 200
        self._assert_opt_in(resp.content.decode(), task.id)

    def test_assignee_dropdown_opts_in(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._assignee_url(project, task), {"assignee_id": user.id})
        assert resp.status_code == 200
        self._assert_opt_in(resp.content.decode(), task.id)

    def test_cycle_dropdown_opts_in(self, client, setup):
        user, project, task = setup
        # Cycle picker is only rendered on non-backlog statuses (the planned /
        # ready branch returns a read-only "Backlog" span without the
        # dropdown). Move into to-do first to exercise the dropdown render
        # path.
        task.status = Task.STATUS_TODO
        task.save(update_fields=["status"])
        client.force_login(user)
        resp = client.post(self._cycle_url(project, task), {"cycle_id": ""})
        assert resp.status_code == 200
        self._assert_opt_in(resp.content.decode(), task.id)

    def test_project_dropdown_opts_in(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        # Re-target to the same project — endpoint still re-renders the cell
        # with the opt-in markup; we don't need an actual move to verify the
        # template wiring.
        resp = client.post(self._project_url(project, task), {"project_id": project.id})
        # Move to the same project is a no-op success — the cell still
        # re-renders. Status code may be 200 (same project) or a 30x to a
        # new URL on a real move; we just need the rendered cell.
        assert resp.status_code in (200, 204, 302)
        if resp.status_code == 200:
            self._assert_opt_in(resp.content.decode(), task.id)
