"""Task detail page (``/projects/<slug_prefix>/<number>/``)."""

from django.urls import reverse

import pytest

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.comments.tests.factories import CommentFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def task_setup(db):
    """Workspace + project + task + member user.

    Returns:
        Tuple ``(user, project, task)``.
    """
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    task = TaskFactory(project=project, reporter=ws.owner)
    return ws.owner, project, task


@pytest.mark.django_db
class TestTaskDetailAccess:
    """Membership-gated access to the task detail page."""

    def test_anonymous_redirected(self, client, task_setup):
        _, project, task = task_setup
        url = reverse(
            "web:task_detail",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )
        resp = client.get(url)
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_member_can_open(self, client, task_setup):
        user, project, task = task_setup
        client.force_login(user)
        url = reverse(
            "web:task_detail",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )
        resp = client.get(url)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert task.title in body
        assert task.slug in body

    def test_foreign_task_returns_404(self, client, task_setup):
        user, _, _ = task_setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        client.force_login(user)
        url = reverse(
            "web:task_detail",
            kwargs={
                "slug_prefix": foreign_project.slug_prefix,
                "number": foreign_task.number,
            },
        )
        resp = client.get(url)
        assert resp.status_code == 404

    def test_unknown_slug_returns_404(self, client, task_setup):
        user, _, task = task_setup
        client.force_login(user)
        url = reverse(
            "web:task_detail",
            kwargs={"slug_prefix": "NOPE", "number": task.number},
        )
        resp = client.get(url)
        assert resp.status_code == 404


@pytest.mark.django_db
class TestTaskDetailContent:
    """Rendered page contains the expected sections."""

    def test_lists_subtasks(self, client, task_setup):
        user, project, parent = task_setup
        sub = TaskFactory(project=project, parent=parent, reporter=user, title="Sub one")
        client.force_login(user)
        resp = client.get(
            reverse(
                "web:task_detail",
                kwargs={"slug_prefix": project.slug_prefix, "number": parent.number},
            ),
        )
        body = resp.content.decode()
        assert "Sub one" in body
        assert sub.slug in body
        assert sub in resp.context["subtasks"]

    def test_lists_comments(self, client, task_setup):
        user, project, task = task_setup
        comment = CommentFactory(task=task, author=user, body="Looks good")
        client.force_login(user)
        resp = client.get(
            reverse(
                "web:task_detail",
                kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
            ),
        )
        assert "Looks good" in resp.content.decode()
        assert comment in resp.context["comments"]

    def test_shows_activity_for_this_task(self, client, task_setup):
        user, project, task = task_setup
        log_event(
            workspace=project.workspace,
            project=project,
            actor=user,
            event_type="task.status_changed",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"from": "to-do", "to": "in-progress"},
        )
        client.force_login(user)
        resp = client.get(
            reverse(
                "web:task_detail",
                kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
            ),
        )
        events = resp.context["activity"]
        assert len(events) == 1
        assert events[0].event_type == "task.status_changed"


@pytest.mark.django_db
class TestTaskDetailQueryCount:
    """Regression guard: detail page stays N+1-free."""

    def test_constant_queries_with_subtasks_and_comments(
        self,
        client,
        task_setup,
        django_assert_max_num_queries,
    ):
        user, project, task = task_setup
        for _ in range(5):
            TaskFactory(project=project, parent=task, reporter=user)
        for _ in range(5):
            CommentFactory(task=task, author=user)
        for i in range(5):
            log_event(
                workspace=project.workspace,
                project=project,
                actor=user,
                event_type="task.updated",
                target_type=ActivityLog.TARGET_TASK,
                target_id=task.id,
                payload={"changes": {"title": {"old": "x", "new": f"x{i}"}}},
            )
        client.force_login(user)
        # +1 over the prior cap for the sidebar inbox-unread badge COUNT
        # added by the context processor (ADR 0021); +2 for the task's own
        # reaction summary and the comment-reaction batch; +1 for the
        # comment-replies prefetch. All single queries regardless of row
        # count — constant, not N+1.
        with django_assert_max_num_queries(24):
            client.get(
                reverse(
                    "web:task_detail",
                    kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
                ),
            )
