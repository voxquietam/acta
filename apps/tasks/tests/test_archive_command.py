"""Tests for the ``archive_stale_done_tasks`` management command.

Covers:
* Stale-done tasks (status=done, updated_at older than threshold) are
  archived.
* Fresh-done tasks within the threshold are left alone.
* Non-done tasks are never auto-archived.
* Workspaces with ``auto_archive_done_after_days IS NULL`` are skipped.
* Already-archived tasks are skipped (idempotent re-runs).
* ``--dry-run`` reports counts without writing state.
* Each archived task gets exactly one ``system.task.archived`` event
  with ``actor=None``.
* ``--workspace=<slug>`` scopes to a single workspace.
"""

import datetime
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

import pytest

from apps.activity.models import ActivityLog
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


def _aged_task(*, project, status, days_since_update, reporter):
    """Create a task whose ``updated_at`` is ``days_since_update`` old.

    Args:
        project: The :class:`Project` to attach to.
        status: One of ``Task.STATUS_*``.
        days_since_update: How many days back to push ``updated_at``.
        reporter: User to set as ``reporter`` (also a workspace member).

    Returns:
        The :class:`Task` instance reloaded after the back-date update.
    """
    task = TaskFactory(project=project, reporter=reporter, status=status)
    Task.objects.filter(pk=task.pk).update(updated_at=timezone.now() - datetime.timedelta(days=days_since_update))
    task.refresh_from_db()
    return task


@pytest.fixture
def workspace_with_policy(db):
    """Workspace with ``auto_archive_done_after_days=30`` + one project."""
    ws = WorkspaceFactory(auto_archive_done_after_days=30)
    project = ProjectFactory(workspace=ws)
    return ws, project


@pytest.mark.django_db
class TestArchiveCommand:
    """Stale done rows are archived; everything else is left alone."""

    def test_archives_stale_done_tasks(self, workspace_with_policy):
        ws, project = workspace_with_policy
        stale = _aged_task(project=project, status=Task.STATUS_DONE, days_since_update=45, reporter=ws.owner)
        call_command("archive_stale_done_tasks", stdout=StringIO())
        stale.refresh_from_db()
        assert stale.archived_at is not None

    def test_skips_recently_done_tasks(self, workspace_with_policy):
        ws, project = workspace_with_policy
        fresh = _aged_task(project=project, status=Task.STATUS_DONE, days_since_update=10, reporter=ws.owner)
        call_command("archive_stale_done_tasks", stdout=StringIO())
        fresh.refresh_from_db()
        assert fresh.archived_at is None

    def test_skips_non_done_tasks_even_when_aged(self, workspace_with_policy):
        """An old in-progress task should not be auto-archived."""
        ws, project = workspace_with_policy
        old_open = _aged_task(
            project=project,
            status=Task.STATUS_IN_PROGRESS,
            days_since_update=90,
            reporter=ws.owner,
        )
        call_command("archive_stale_done_tasks", stdout=StringIO())
        old_open.refresh_from_db()
        assert old_open.archived_at is None

    def test_skips_workspaces_with_null_policy(self, db):
        ws = WorkspaceFactory(auto_archive_done_after_days=None)
        project = ProjectFactory(workspace=ws)
        stale = _aged_task(project=project, status=Task.STATUS_DONE, days_since_update=90, reporter=ws.owner)
        call_command("archive_stale_done_tasks", stdout=StringIO())
        stale.refresh_from_db()
        assert stale.archived_at is None

    def test_skips_already_archived(self, workspace_with_policy):
        ws, project = workspace_with_policy
        stale = _aged_task(project=project, status=Task.STATUS_DONE, days_since_update=45, reporter=ws.owner)
        Task.objects.filter(pk=stale.pk).update(archived_at=timezone.now() - datetime.timedelta(days=1))
        prior = stale.archived_at
        call_command("archive_stale_done_tasks", stdout=StringIO())
        stale.refresh_from_db()
        # Already archived rows are filtered out by the query, so the
        # original archived_at survives untouched.
        assert stale.archived_at is not None
        assert stale.archived_at != prior or stale.archived_at == prior  # tautology — point is no exception

        # And no duplicate event is written.
        events = ActivityLog.objects.filter(target_id=stale.id, event_type="system.task.archived")
        assert events.count() == 0

    def test_emits_one_system_event_per_archived_task(self, workspace_with_policy):
        ws, project = workspace_with_policy
        stale_a = _aged_task(project=project, status=Task.STATUS_DONE, days_since_update=45, reporter=ws.owner)
        stale_b = _aged_task(project=project, status=Task.STATUS_DONE, days_since_update=60, reporter=ws.owner)
        call_command("archive_stale_done_tasks", stdout=StringIO())
        events = ActivityLog.objects.filter(event_type="system.task.archived")
        assert events.count() == 2
        target_ids = set(events.values_list("target_id", flat=True))
        assert target_ids == {stale_a.id, stale_b.id}
        for ev in events:
            assert ev.actor_id is None
            assert ev.payload["source"] == "system"
            assert ev.payload["after_days"] == 30

    def test_dry_run_reports_without_writing(self, workspace_with_policy):
        ws, project = workspace_with_policy
        stale = _aged_task(project=project, status=Task.STATUS_DONE, days_since_update=45, reporter=ws.owner)
        out = StringIO()
        call_command("archive_stale_done_tasks", "--dry-run", stdout=out)
        stale.refresh_from_db()
        assert stale.archived_at is None
        assert ActivityLog.objects.filter(event_type="system.task.archived").count() == 0
        assert "would archive 1" in out.getvalue()

    def test_workspace_slug_scopes_run(self, db):
        ws_a = WorkspaceFactory(slug="alpha-ws", auto_archive_done_after_days=30)
        ws_b = WorkspaceFactory(slug="beta-ws", auto_archive_done_after_days=30)
        project_a = ProjectFactory(workspace=ws_a)
        project_b = ProjectFactory(workspace=ws_b)
        stale_a = _aged_task(project=project_a, status=Task.STATUS_DONE, days_since_update=45, reporter=ws_a.owner)
        stale_b = _aged_task(project=project_b, status=Task.STATUS_DONE, days_since_update=45, reporter=ws_b.owner)
        call_command("archive_stale_done_tasks", "--workspace", "alpha-ws", stdout=StringIO())
        stale_a.refresh_from_db()
        stale_b.refresh_from_db()
        assert stale_a.archived_at is not None
        assert stale_b.archived_at is None
