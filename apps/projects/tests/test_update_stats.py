"""compute_update_stats — frozen counter snapshot for a project update."""

import datetime

from django.urls import reverse
from django.utils import timezone

import pytest

from apps.projects.models import ProjectUpdate
from apps.projects.stats import compute_update_stats, resolve_stats_window_start
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def project(db):
    ws = WorkspaceFactory()
    return ProjectFactory(workspace=ws)


@pytest.mark.django_db
class TestComputeUpdateStats:

    def test_closed_keyed_off_completed_at_window(self, project):
        since = timezone.now() - datetime.timedelta(days=7)
        # Closed inside the window — counted.
        inside = TaskFactory(project=project, status=Task.STATUS_DONE)
        inside.completed_at = since + datetime.timedelta(days=2)
        inside.save(update_fields=["completed_at"])
        # Closed BEFORE the window — not counted.
        old = TaskFactory(project=project, status=Task.STATUS_DONE)
        old.completed_at = since - datetime.timedelta(days=1)
        old.save(update_fields=["completed_at"])
        result = compute_update_stats(project, since=since)
        assert result["closed"] == 1

    def test_current_status_counters(self, project):
        TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS)
        TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS)
        TaskFactory(project=project, status=Task.STATUS_IN_REVIEW)
        TaskFactory(project=project, status=Task.STATUS_PLANNED)
        TaskFactory(project=project, status=Task.STATUS_READY)
        result = compute_update_stats(project, since=timezone.now() - datetime.timedelta(days=30))
        assert result["in_progress"] == 2
        assert result["in_review"] == 1
        assert result["planned"] == 1
        assert result["ready"] == 1

    def test_archived_excluded_from_open_buckets(self, project):
        active = TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS)
        archived = TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS)
        archived.archived_at = timezone.now()
        archived.save(update_fields=["archived_at"])
        result = compute_update_stats(project, since=timezone.now() - datetime.timedelta(days=1))
        assert result["in_progress"] == 1
        assert active.pk  # quiet linter

    def test_window_iso_timestamps_present(self, project):
        since = timezone.now() - datetime.timedelta(days=3)
        result = compute_update_stats(project, since=since)
        assert "since" in result and "until" in result
        assert result["since"].startswith(str(since.year))


@pytest.mark.django_db
class TestResolveStatsWindowStart:

    def test_first_update_uses_project_created_at(self, project):
        assert resolve_stats_window_start(project) == project.created_at

    def test_subsequent_updates_use_latest_created_at(self, project):
        first = ProjectUpdate.objects.create(
            project=project,
            author=project.workspace.owner,
            health=ProjectUpdate.ON_TRACK,
            body="one",
        )
        assert resolve_stats_window_start(project) == first.created_at


@pytest.mark.django_db
class TestPostUpdateIncludesStats:

    def test_include_stats_freezes_snapshot(self, client, project):
        TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS)
        TaskFactory(project=project, status=Task.STATUS_PLANNED)
        client.force_login(project.workspace.owner)
        resp = client.post(
            reverse("web:post_project_update", args=[project.slug_prefix]),
            {"health": ProjectUpdate.ON_TRACK, "body": "weekly", "include_stats": "on"},
        )
        assert resp.status_code == 200
        update = ProjectUpdate.objects.get(project=project)
        assert update.stats["in_progress"] == 1
        assert update.stats["planned"] == 1
        assert "since" in update.stats and "until" in update.stats

    def test_no_stats_when_toggle_off(self, client, project):
        TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS)
        client.force_login(project.workspace.owner)
        client.post(
            reverse("web:post_project_update", args=[project.slug_prefix]),
            {"health": ProjectUpdate.ON_TRACK, "body": "weekly"},
        )
        update = ProjectUpdate.objects.get(project=project)
        assert update.stats == {}

    def test_card_renders_stats_block_when_present(self, client, project):
        TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS)
        client.force_login(project.workspace.owner)
        resp = client.post(
            reverse("web:post_project_update", args=[project.slug_prefix]),
            {"health": ProjectUpdate.ON_TRACK, "body": "weekly", "include_stats": "on"},
        )
        assert "Since last update" in resp.content.decode()
        assert "in progress" in resp.content.decode()

    def test_counters_link_into_project_views(self, client, project):
        TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS)
        client.force_login(project.workspace.owner)
        resp = client.post(
            reverse("web:post_project_update", args=[project.slug_prefix]),
            {"health": ProjectUpdate.ON_TRACK, "body": "weekly", "include_stats": "on"},
        )
        body = resp.content.decode()
        # Links carry a ``?w=`` workspace hint (project keys are unique per
        # workspace, not globally), so the view filters join with ``&``.
        base = reverse("web:project_detail", args=[project.slug_prefix])
        project_url = f"{base}?w={project.workspace.slug}"
        # Closed deep-links to the list view with a completed_at window filter.
        assert f"{project_url}&amp;view=list&amp;date_field=completed&amp;date_after=" in body
        # Status-snapshot chips deep-link to a current-status filter.
        assert f"{project_url}&amp;view=list&amp;status=in-progress" in body
        assert f"{project_url}&amp;view=list&amp;status=in-review" in body
        assert f"{project_url}&amp;view=list&amp;status=ready" in body
        # Planned chip opens the Backlog tab (planned + ready grooming).
        assert f"{project_url}&amp;view=backlog" in body
