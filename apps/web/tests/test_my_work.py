"""Tests for the ``My Work`` page (:class:`apps.web.views.MyWorkView`).

Covers:
* Scope — user sees only their own tasks, even across multiple
  workspaces; foreign tasks excluded.
* Deadline bucketing — overdue / today / week / later / no-deadline.
* Recently-done window — closed tasks within the last 7 days appear,
  older done tasks don't.
* N+1 audit — page query count stays bounded regardless of task count.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """Workspace + project + member user fixture (workspace is active)."""
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    ws.owner.active_workspace = ws
    ws.owner.save(update_fields=["active_workspace"])
    return ws.owner, project


def _deadline_sections(resp):
    """Return the deadline-axis sections list from a My Work response."""
    return resp.context["list_sections_by_axis"]["deadline"]


def _section(sections, key):
    """Look up a section by key in the view context list."""
    for s in sections:
        if s["key"] == key:
            return s
    raise AssertionError(f"section {key!r} not in {[s['key'] for s in sections]}")


def _section_or_empty(sections, key):
    """Look up a section by key, returning an empty task list if absent.

    The grouping helper drops empty deadline buckets (except
    ``recently_done`` which is pinned via ``keep_empty``), so tests that
    expect "no task fell into this bucket" should accept the section
    being missing entirely.
    """
    for s in sections:
        if s["key"] == key:
            return s
    return {"key": key, "tasks": []}


@pytest.mark.django_db
class TestMyWorkScope:
    """Only the requesting user's own open / recently-closed tasks
    surface on the page."""

    def test_only_my_tasks_are_listed(self, client, setup):
        user, project = setup
        TaskFactory(project=project, reporter=user, assignee=user, title="Mine")
        TaskFactory(project=project, reporter=user, assignee=None, title="Unassigned")
        other_user = WorkspaceFactory().owner
        TaskFactory(project=project, reporter=user, assignee=other_user, title="Theirs")
        client.force_login(user)
        resp = client.get(reverse("web:my_work"))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Mine" in body
        assert "Unassigned" not in body
        assert "Theirs" not in body

    def test_scoped_to_active_workspace(self, client, db):
        """My Work shows assigned tasks in the active workspace only;
        assignments in another workspace are hidden until the user
        switches into it."""
        ws1 = WorkspaceFactory()
        ws2 = WorkspaceFactory()
        from apps.workspaces.tests.factories import WorkspaceMemberFactory

        WorkspaceMemberFactory(workspace=ws2, user=ws1.owner)
        p1 = ProjectFactory(workspace=ws1)
        p2 = ProjectFactory(workspace=ws2)
        TaskFactory(project=p1, reporter=ws1.owner, assignee=ws1.owner, title="W1 task")
        TaskFactory(project=p2, reporter=ws2.owner, assignee=ws1.owner, title="W2 task")
        user = ws1.owner
        user.active_workspace = ws1
        user.save(update_fields=["active_workspace"])
        client.force_login(user)
        body = client.get(reverse("web:my_work")).content.decode()
        assert "W1 task" in body
        assert "W2 task" not in body


@pytest.mark.django_db
class TestMyWorkBucketing:
    """Tasks land in the right deadline-aware section."""

    def test_overdue_today_week_later_no_deadline(self, client, setup):
        user, project = setup
        today = timezone.localdate()
        overdue = TaskFactory(
            project=project,
            reporter=user,
            assignee=user,
            title="overdue",
            due_date=today - datetime.timedelta(days=3),
            status=Task.STATUS_TODO,
        )
        today_task = TaskFactory(
            project=project,
            reporter=user,
            assignee=user,
            title="today",
            due_date=today,
            status=Task.STATUS_TODO,
        )
        week_task = TaskFactory(
            project=project,
            reporter=user,
            assignee=user,
            title="week",
            due_date=today + datetime.timedelta(days=3),
            status=Task.STATUS_TODO,
        )
        later_task = TaskFactory(
            project=project,
            reporter=user,
            assignee=user,
            title="later",
            due_date=today + datetime.timedelta(days=30),
            status=Task.STATUS_TODO,
        )
        no_due = TaskFactory(
            project=project,
            reporter=user,
            assignee=user,
            title="nodue",
            due_date=None,
            status=Task.STATUS_TODO,
        )
        client.force_login(user)
        resp = client.get(reverse("web:my_work"))
        ctx = _deadline_sections(resp)
        assert overdue in _section(ctx, "overdue")["tasks"]
        assert today_task in _section(ctx, "today")["tasks"]
        assert week_task in _section(ctx, "week")["tasks"]
        assert later_task in _section(ctx, "later")["tasks"]
        assert no_due in _section(ctx, "no_deadline")["tasks"]

    def test_recently_done_window(self, client, setup):
        user, project = setup
        now = timezone.now()
        recent = TaskFactory(
            project=project,
            reporter=user,
            assignee=user,
            title="recent_done",
            status=Task.STATUS_DONE,
        )
        old = TaskFactory(
            project=project,
            reporter=user,
            assignee=user,
            title="old_done",
            status=Task.STATUS_DONE,
        )
        # Push old.updated_at past the 7-day cutoff.
        Task.objects.filter(pk=old.pk).update(updated_at=now - datetime.timedelta(days=10))
        Task.objects.filter(pk=recent.pk).update(updated_at=now - datetime.timedelta(days=1))
        client.force_login(user)
        resp = client.get(reverse("web:my_work"))
        ctx = _deadline_sections(resp)
        recently = _section(ctx, "recently_done")["tasks"]
        recent_titles = {t.title for t in recently}
        assert "recent_done" in recent_titles
        assert "old_done" not in recent_titles

    def test_status_filter_excludes_recently_done(self, client, setup):
        """Picking specific statuses in the sidebar narrows the page —
        ``?status=to-do`` keeps only to-do tasks, dropping the
        recently-done section.
        """
        user, project = setup
        TaskFactory(
            project=project,
            reporter=user,
            assignee=user,
            title="recent_done",
            status=Task.STATUS_DONE,
        )
        TaskFactory(
            project=project,
            reporter=user,
            assignee=user,
            title="open_todo",
            status=Task.STATUS_TODO,
        )
        client.force_login(user)
        resp = client.get(reverse("web:my_work"), {"status": "to-do"})
        ctx = _deadline_sections(resp)
        recently = {t.title for t in _section(ctx, "recently_done")["tasks"]}
        assert "recent_done" not in recently

    def test_overdue_excludes_done_tasks(self, client, setup):
        """A task past its due date but already closed shouldn't sit in
        Overdue — done goes to ``recently_done`` or out of view entirely."""
        user, project = setup
        today = timezone.localdate()
        done_old_due = TaskFactory(
            project=project,
            reporter=user,
            assignee=user,
            title="done_old_due",
            status=Task.STATUS_DONE,
            due_date=today - datetime.timedelta(days=2),
        )
        client.force_login(user)
        resp = client.get(reverse("web:my_work"))
        ctx = _deadline_sections(resp)
        overdue_titles = {t.title for t in _section_or_empty(ctx, "overdue")["tasks"]}
        assert "done_old_due" not in overdue_titles
        assert done_old_due in _section(ctx, "recently_done")["tasks"]


@pytest.mark.django_db
class TestMyWorkEmpty:
    """Empty state when no tasks are assigned to the user."""

    def test_empty_state_message(self, client, setup):
        user, _ = setup
        client.force_login(user)
        resp = client.get(reverse("web:my_work"))
        body = resp.content.decode()
        assert "Nothing assigned to you" in body
        assert resp.context["has_any_tasks"] is False


@pytest.mark.django_db
class TestMyWorkQueryCount:
    """Page query count must stay bounded — independent of task count."""

    def test_no_n_plus_one(self, client, setup):
        user, project = setup
        # 15 tasks split across the buckets via varying due dates.
        today = timezone.localdate()
        for i in range(15):
            TaskFactory(
                project=project,
                reporter=user,
                assignee=user,
                title=f"task{i}",
                due_date=today + datetime.timedelta(days=i - 7),
                status=Task.STATUS_TODO,
            )
        client.force_login(user)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(reverse("web:my_work"))
            assert resp.status_code == 200
        # Hard ceiling: well under "one query per task". The exact
        # count fluctuates with middleware (session, auth, i18n) but
        # 30 is a generous safe margin and catches accidental
        # serializer / template N+1 regressions.
        assert len(ctx.captured_queries) < 30, f"Got {len(ctx.captured_queries)} queries for 15 tasks — N+1 regression."
