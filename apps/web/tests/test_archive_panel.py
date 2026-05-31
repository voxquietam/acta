"""Archive panel — surfaces archived tasks under their own view tab.

Covers:
* ``?view=archive`` is an accepted view mode (cookie persistence path).
* ``?panel=archive`` lazy fetch returns the archived rows only.
* Unarchive button visible in the panel + endpoint fires ``acta:task-changed``
  so the parent inner refetches and the row disappears.
"""

from django.urls import reverse
from django.utils import timezone

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    user = ws.owner
    user.active_workspace = ws
    user.save(update_fields=["active_workspace"])
    return user, project


def _archived(project, user, *, title):
    """Create + archive a task, return it."""
    task = TaskFactory(project=project, reporter=user, title=title, status=Task.STATUS_DONE)
    task.archived_at = timezone.now()
    task.save(update_fields=["archived_at"])
    return task


@pytest.mark.django_db
class TestArchivePanelAllTasks:
    """All Tasks page exposes the Archive tab + lazy panel."""

    def test_panel_archive_lists_archived_only(self, client, setup):
        user, project = setup
        archived = _archived(project, user, title="dusty-relic")
        TaskFactory(project=project, reporter=user, title="still-active", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?panel=archive", HTTP_HX_REQUEST="true")
        body = resp.content.decode()
        assert archived.slug in body
        assert "still-active" not in body
        assert "Unarchive" in body

    def test_view_archive_renders_panel_inline(self, client, setup):
        user, project = setup
        archived = _archived(project, user, title="dusty-relic")
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?view=archive")
        body = resp.content.decode()
        assert archived.slug in body
        assert "Archive" in body

    def test_panel_archive_empty_state(self, client, setup):
        user, _project = setup
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?panel=archive", HTTP_HX_REQUEST="true")
        assert "Archive is empty" in resp.content.decode()


@pytest.mark.django_db
class TestArchivePanelProjectDetail:
    """Project detail page exposes the Archive tab + lazy panel."""

    def test_panel_archive_scoped_to_project(self, client, setup):
        user, project = setup
        other_project = ProjectFactory(workspace=project.workspace)
        ours = _archived(project, user, title="our-archived")
        theirs = _archived(other_project, user, title="their-archived")
        client.force_login(user)
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}) + "?panel=archive",
            HTTP_HX_REQUEST="true",
        )
        body = resp.content.decode()
        assert ours.slug in body
        assert theirs.slug not in body


@pytest.mark.django_db
class TestArchiveTaskFiresChangeEvent:
    """Unarchive from the panel must drive a refresh on listeners."""

    def test_unarchive_response_carries_task_changed_trigger(self, client, setup):
        user, project = setup
        task = _archived(project, user, title="to-unarchive")
        client.force_login(user)
        resp = client.post(
            reverse("web:archive_task", kwargs={"slug_prefix": project.slug_prefix, "number": task.number}),
            {"unarchive": "1"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "acta:task-changed"
