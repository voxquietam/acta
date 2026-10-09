"""Insights at both scopes, and the three blocks that say why work waits.

The numbers these draw are replayed from the activity log, so the cases
that matter are the ones where the log says something the page must not
take at face value: an administrative rewrite that looks like a hundred
rejections, a blocking link whose other end has long since shipped, and
imported history where a task was finished before it was created.
"""

import datetime

from django.urls import reverse
from django.utils import timezone

import pytest

from apps.activity.models import ActivityLog
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web import flow
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory

pytestmark = pytest.mark.django_db


def move(task, to_status, ago_days, *, from_status=Task.STATUS_TODO, reason=None):
    """Log one status change at a chosen distance in the past.

    Args:
        task: The task that moved.
        to_status: Where it moved to.
        ago_days: How many days back to date the move.
        from_status: Where it moved from.
        reason: The marker a writer stamps on a rewrite, if any.

    Returns:
        The written row.
    """
    payload = {"from": from_status, "to": to_status}
    if reason:
        payload["reason"] = reason
    row = ActivityLog.objects.create(
        workspace=task.project.workspace,
        project=task.project,
        target_type=ActivityLog.TARGET_TASK,
        target_id=task.id,
        event_type="task.status_changed",
        payload=payload,
    )
    ActivityLog.objects.filter(pk=row.pk).update(created_at=timezone.now() - datetime.timedelta(days=ago_days))
    return row


@pytest.fixture
def scope():
    """A member, two projects in one workspace."""
    user = WorkspaceFactory().owner
    workspace = user.owned_workspaces.first() if hasattr(user, "owned_workspaces") else None
    workspace = workspace or WorkspaceMember.objects.filter(user=user).first().workspace
    backend = ProjectFactory(workspace=workspace, slug_prefix="BCK")
    web = ProjectFactory(workspace=workspace, slug_prefix="WEB")
    return user, workspace, backend, web


class TestTheScope:
    """One view, two readings, and the URL decides which."""

    def test_both_scopes_render(self, client, scope):
        user, _ws, backend, _web = scope
        client.force_login(user)

        assert client.get(reverse("web:insights")).status_code == 200
        assert (
            client.get(
                reverse("web:project_insights", kwargs={"slug_prefix": backend.slug_prefix}),
            ).status_code
            == 200
        )

    def test_the_project_scope_counts_only_that_project(self, client, scope):
        user, _ws, backend, web = scope
        for _index in range(3):
            TaskFactory(project=backend)
        TaskFactory(project=web)
        client.force_login(user)

        whole = client.get(reverse("web:insights"))
        one = client.get(reverse("web:project_insights", kwargs={"slug_prefix": backend.slug_prefix}))

        assert whole.context["task_count"] == 4
        assert one.context["task_count"] == 3

    def test_an_unknown_window_falls_back_rather_than_failing(self, client, scope):
        user, _ws, _backend, _web = scope
        client.force_login(user)

        resp = client.get(reverse("web:insights"), {"weeks": "all of it"})

        assert resp.context["weeks"] == flow.DEFAULT_WEEKS


class TestWaitVsWork:
    """Where a finished task's life actually went."""

    def test_it_refuses_rather_than_averaging_three_tasks(self, scope):
        _user, workspace, backend, _web = scope
        for _index in range(3):
            task = TaskFactory(project=backend, status=Task.STATUS_DONE)
            move(task, Task.STATUS_DONE, 2, from_status=Task.STATUS_IN_PROGRESS)

        result = flow.wait_vs_work(workspace, backend, weeks=8)

        assert result["ok"] is False
        assert result["need"] == flow.NEED_CLOSED

    def test_waiting_and_working_are_told_apart(self, scope):
        """Four days queued, one day worked — a fifth of the life."""
        _user, workspace, backend, _web = scope
        for _index in range(flow.NEED_CLOSED):
            task = TaskFactory(project=backend, status=Task.STATUS_DONE)
            Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - datetime.timedelta(days=10))
            move(task, Task.STATUS_TODO, 10, from_status=Task.STATUS_PLANNED)
            move(task, Task.STATUS_IN_PROGRESS, 6, from_status=Task.STATUS_TODO)
            move(task, Task.STATUS_DONE, 5, from_status=Task.STATUS_IN_PROGRESS)

        result = flow.wait_vs_work(workspace, backend, weeks=8)

        assert result["ok"] is True
        assert result["counted"] == flow.NEED_CLOSED
        # One day in progress out of five days alive.
        assert 15 <= result["efficiency"] <= 25
        assert result["longest"]["status"] == Task.STATUS_TODO


class TestBlockedRadar:
    """What is held up right now — a photograph, not a window."""

    def test_a_blocker_that_shipped_is_not_holding_anything(self, scope):
        _user, workspace, backend, _web = scope
        blocker = TaskFactory(project=backend, status=Task.STATUS_DONE)
        blocked = TaskFactory(project=backend, status=Task.STATUS_TODO)
        blocker.blocks.add(blocked)

        result = flow.blocked_radar(workspace, backend)

        assert result["ok"] is False
        assert result["any_links"] is True

    def test_a_live_block_is_priced_in_days(self, scope):
        _user, workspace, backend, _web = scope
        blocker = TaskFactory(project=backend, status=Task.STATUS_IN_PROGRESS)
        blocked = TaskFactory(project=backend, status=Task.STATUS_TODO)
        for task in (blocker, blocked):
            Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - datetime.timedelta(days=12))
        blocker.blocks.add(blocked)

        result = flow.blocked_radar(workspace, backend)

        assert result["ok"] is True
        assert result["blocked_now"] == 1
        assert result["rows"][0]["days"] == 12
        # No link event exists, so the age is an estimate and says so.
        assert result["estimated_links"] == 1

    def test_a_project_sees_the_blocker_that_sits_outside_it(self, scope):
        """Being held up by another team is still this project's wait."""
        _user, workspace, backend, web = scope
        blocker = TaskFactory(project=web, status=Task.STATUS_TODO)
        blocked = TaskFactory(project=backend, status=Task.STATUS_TODO)
        blocker.blocks.add(blocked)

        result = flow.blocked_radar(workspace, backend)

        assert result["ok"] is True
        assert result["rows"][0]["task"].project_id == web.id


class TestBounces:
    """Work going backwards — and what only looks like it."""

    def test_retiring_the_ready_column_is_not_a_hundred_rejections(self, scope):
        """The admin flipped a switch; the team did not reject anything.

        ``retire_ready_status`` rewrites every ready task to planned and
        logs each move, correctly. Counting those as bounces made the
        block read 32% on real data where the true figure was 4%.
        """
        _user, workspace, backend, _web = scope
        for _index in range(flow.NEED_MOVES + 5):
            task = TaskFactory(project=backend)
            move(
                task,
                Task.STATUS_PLANNED,
                1,
                from_status=Task.STATUS_READY,
                reason="ready_column_retired",
            )

        result = flow.bounces(workspace, backend, weeks=8)

        assert result["ok"] is True
        assert result["total"] == 0

    def test_a_real_move_back_still_counts(self, scope):
        _user, workspace, backend, _web = scope
        for _index in range(flow.NEED_MOVES):
            task = TaskFactory(project=backend)
            move(task, Task.STATUS_IN_PROGRESS, 3, from_status=Task.STATUS_TODO)
        sent_back = TaskFactory(project=backend)
        move(sent_back, Task.STATUS_IN_PROGRESS, 1, from_status=Task.STATUS_IN_REVIEW)

        result = flow.bounces(workspace, backend, weeks=8)

        assert result["review_back"] == 1
        assert result["total"] == 1

    def test_it_refuses_on_too_few_moves_to_be_a_rate(self, scope):
        _user, workspace, backend, _web = scope
        task = TaskFactory(project=backend)
        move(task, Task.STATUS_IN_PROGRESS, 1, from_status=Task.STATUS_IN_REVIEW)

        result = flow.bounces(workspace, backend, weeks=8)

        assert result["ok"] is False
        assert result["need"] == flow.NEED_MOVES


class TestLeadTimeCannotRunBackwards:
    """Imported history stamps the row today and the events years ago."""

    def test_a_task_finished_before_it_was_created_is_not_a_sample(self, scope):
        from apps.tasks.metrics import compute_flow_metrics

        _user, workspace, backend, _web = scope
        task = TaskFactory(project=backend, status=Task.STATUS_DONE)
        move(task, Task.STATUS_DONE, 3, from_status=Task.STATUS_IN_PROGRESS)

        metrics = compute_flow_metrics(backend, weeks=8)

        assert all(sample >= 0 for sample in metrics["lead_times"])
        assert metrics["lead_median"] is None or metrics["lead_median"] >= 0
        assert workspace is not None
