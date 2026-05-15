"""Inline-edit endpoints on the task detail page.

Covers status quick-change, priority quick-change, and comment posting
(:mod:`apps.web.views`).
"""

from django.urls import reverse

import pytest

from apps.activity.models import ActivityLog
from apps.comments.models import Comment
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


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
