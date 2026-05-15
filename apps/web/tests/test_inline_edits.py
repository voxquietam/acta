"""Inline-edit endpoints on the task detail page.

Covers status / priority / due-date / assignee / labels / title
inline edits and comment posting (:mod:`apps.web.views`).
"""

import datetime

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.comments.models import Comment
from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
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

    def test_response_fragment_with_oob_activity(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.post(self._url(project, task), {"description": "New body"})
        body = resp.content.decode()
        assert "<html" not in body
        assert 'id="description-cell"' in body
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
