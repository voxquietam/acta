"""Tests for the All Tasks page (:class:`apps.web.views.AllTasksView`)."""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def setup(db):
    """Two workspaces both owned by the same user + one project each."""
    user = UserFactory()
    ws1 = WorkspaceFactory(owner=user)
    ws2 = WorkspaceFactory(owner=user)
    WorkspaceMemberFactory(workspace=ws2, user=user)
    p1 = ProjectFactory(workspace=ws1)
    p2 = ProjectFactory(workspace=ws2)
    return user, ws1, ws2, p1, p2


@pytest.mark.django_db
class TestAllTasksScope:
    """Only the user's workspaces' tasks are listed."""

    def test_lists_tasks_across_workspaces(self, client, setup):
        user, ws1, ws2, p1, p2 = setup
        TaskFactory(project=p1, reporter=user, title="From WS1", status=Task.STATUS_TODO)
        TaskFactory(project=p2, reporter=user, title="From WS2", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks"))
        body = resp.content.decode()
        assert "From WS1" in body
        assert "From WS2" in body

    def test_foreign_workspace_tasks_excluded(self, client, setup):
        user, _, _, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        TaskFactory(project=foreign_project, reporter=foreign_ws.owner, title="Foreign")
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks"))
        assert "Foreign" not in resp.content.decode()


@pytest.mark.django_db
class TestAllTasksFilters:
    """Querystring filters narrow the result set."""

    def test_status_filter(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-todo", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="t-prog", status=Task.STATUS_IN_PROGRESS)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?status=to-do")
        body = resp.content.decode()
        assert "t-todo" in body
        assert "t-prog" not in body

    def test_done_hidden_by_default(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-done", status=Task.STATUS_DONE)
        TaskFactory(project=p1, reporter=user, title="t-todo", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks"))
        body = resp.content.decode()
        assert "t-todo" in body
        assert "t-done" not in body

    def test_show_done_param_includes_done(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-done", status=Task.STATUS_DONE)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?show_done=1")
        assert "t-done" in resp.content.decode()

    def test_assignee_me(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, assignee=user, title="mine", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, assignee=None, title="nobody", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?assignee=me")
        body = resp.content.decode()
        assert "mine" in body
        assert "nobody" not in body

    def test_project_filter(self, client, setup):
        user, _, _, p1, p2 = setup
        TaskFactory(project=p1, reporter=user, title="in-p1", status=Task.STATUS_TODO)
        TaskFactory(project=p2, reporter=user, title="in-p2", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + f"?project={p1.id}")
        body = resp.content.decode()
        assert "in-p1" in body
        assert "in-p2" not in body

    def test_search_query(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="Refactor auth", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="Wire up SSE", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?q=refactor")
        body = resp.content.decode()
        assert "Refactor auth" in body
        assert "Wire up SSE" not in body

    def test_assignee_by_user_id(self, client, setup):
        """``?assignee=<id>`` filters to that specific user's tasks."""
        user, ws1, _, p1, _ = setup
        other = UserFactory()
        WorkspaceMemberFactory(workspace=ws1, user=other)
        TaskFactory(project=p1, reporter=user, assignee=user, title="mine", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, assignee=other, title="theirs", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + f"?assignee={other.id}")
        body = resp.content.decode()
        assert "theirs" in body
        assert "mine" not in body

    def test_assignee_multi_value(self, client, setup):
        """Multiple ``?assignee=`` values combine as OR (incl. ``unassigned``)."""
        user, ws1, _, p1, _ = setup
        other = UserFactory()
        WorkspaceMemberFactory(workspace=ws1, user=other)
        TaskFactory(project=p1, reporter=user, assignee=other, title="for-other", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, assignee=None, title="nobody", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, assignee=user, title="mine", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + f"?assignee={other.id}&assignee=unassigned")
        body = resp.content.decode()
        assert "for-other" in body
        assert "nobody" in body
        assert "mine" not in body

    def test_label_filter(self, client, setup):
        """``?label=<id>`` keeps only tasks tagged with that label."""
        user, _, _, p1, _ = setup
        keep_label = LabelFactory(workspace=p1.workspace, name="keep")
        drop_label = LabelFactory(workspace=p1.workspace, name="drop")
        t_keep = TaskFactory(project=p1, reporter=user, title="has-keep", status=Task.STATUS_TODO)
        t_keep.labels.add(keep_label)
        t_drop = TaskFactory(project=p1, reporter=user, title="has-drop", status=Task.STATUS_TODO)
        t_drop.labels.add(drop_label)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + f"?label={keep_label.id}")
        body = resp.content.decode()
        assert "has-keep" in body
        assert "has-drop" not in body

    def test_workspace_filter(self, client, setup):
        """``?workspace=<id>`` restricts to tasks in that workspace."""
        user, ws1, ws2, p1, p2 = setup
        TaskFactory(project=p1, reporter=user, title="in-ws1", status=Task.STATUS_TODO)
        TaskFactory(project=p2, reporter=user, title="in-ws2", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + f"?workspace={ws1.id}")
        body = resp.content.decode()
        assert "in-ws1" in body
        assert "in-ws2" not in body


@pytest.mark.django_db
class TestAllTasksQueryCount:
    """N+1 audit — large filtered list stays bounded."""

    def test_no_n_plus_one(self, client, setup):
        user, _, _, p1, _ = setup
        label = LabelFactory(workspace=p1.workspace)
        for i in range(30):
            t = TaskFactory(project=p1, reporter=user, title=f"t{i}", status=Task.STATUS_TODO)
            t.labels.add(label)
        client.force_login(user)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(reverse("web:all_tasks"))
            assert resp.status_code == 200
        assert len(ctx.captured_queries) < 30, f"Got {len(ctx.captured_queries)} queries for 30 tasks — N+1 regression."
