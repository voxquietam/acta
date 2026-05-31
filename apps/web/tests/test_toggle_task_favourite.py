"""Tests for the task favourite toggle endpoint.

Covers star / unstar transitions, access control, and the response
shape (star button HTML + OOB sidebar swap markup). Mirrors
``test_toggle_project_favourite.py``.
"""

from django.urls import reverse

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    task = TaskFactory(project=project)
    return ws.owner, task


def _url(task):
    return reverse(
        "web:toggle_task_favourite",
        kwargs={
            "slug_prefix": task.project.slug_prefix,
            "number": task.number,
        },
    )


@pytest.mark.django_db
class TestToggleTaskFavourite:

    def test_star_adds_task(self, client, setup):
        user, task = setup
        client.force_login(user)
        assert not user.favourite_tasks.filter(pk=task.pk).exists()
        resp = client.post(_url(task))
        assert resp.status_code == 200
        assert user.favourite_tasks.filter(pk=task.pk).exists()

    def test_unstar_removes_task(self, client, setup):
        user, task = setup
        user.favourite_tasks.add(task)
        client.force_login(user)
        resp = client.post(_url(task))
        assert resp.status_code == 200
        assert not user.favourite_tasks.filter(pk=task.pk).exists()

    def test_response_contains_star_button(self, client, setup):
        user, task = setup
        client.force_login(user)
        resp = client.post(_url(task))
        assert f"task-favourite-{task.slug}".encode() in resp.content

    def test_response_star_carries_oob(self, client, setup):
        user, task = setup
        client.force_login(user)
        resp = client.post(_url(task))
        assert f"outerHTML:#task-favourite-{task.slug}".encode() in resp.content

    def test_response_contains_sidebar_oob(self, client, setup):
        user, task = setup
        client.force_login(user)
        resp = client.post(_url(task))
        assert b"sidebar-favourites" in resp.content

    def test_foreign_task_404(self, client, setup):
        user, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project)
        client.force_login(user)
        resp = client.post(_url(foreign_task))
        assert resp.status_code == 404
        assert not user.favourite_tasks.filter(pk=foreign_task.pk).exists()

    def test_get_not_allowed(self, client, setup):
        user, task = setup
        client.force_login(user)
        resp = client.get(_url(task))
        assert resp.status_code == 405

    def test_anonymous_redirected(self, client, setup):
        _, task = setup
        resp = client.post(_url(task))
        assert resp.status_code in (302, 301)


@pytest.mark.django_db
class TestSidebarFavouriteTasks:
    """Context processor + template integration for starred tasks in the rail."""

    def test_nested_under_starred_project(self, client, setup):
        user, task = setup
        user.favourite_projects.add(task.project)
        user.favourite_tasks.add(task)
        user.active_workspace = task.project.workspace
        user.save(update_fields=["active_workspace"])
        client.force_login(user)
        resp = client.get(reverse("web:my_work"))
        body = resp.content.decode()
        # Task slug + title appear under the project's nested row.
        assert task.slug in body
        assert task.title in body

    def test_orphan_task_in_issues_section(self, client, setup):
        user, task = setup
        # Project NOT starred; task is.
        user.favourite_tasks.add(task)
        user.active_workspace = task.project.workspace
        user.save(update_fields=["active_workspace"])
        client.force_login(user)
        resp = client.get(reverse("web:my_work"))
        body = resp.content.decode()
        assert "Issues" in body
        assert task.slug in body

    def test_no_favourites_shows_empty_state(self, client, setup):
        user, _ = setup
        client.force_login(user)
        resp = client.get(reverse("web:my_work"))
        body = resp.content.decode()
        # Empty-state CTA mentions both projects and tasks now.
        assert "Star a project" in body
