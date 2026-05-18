"""Tests for the project favourite toggle endpoint.

Covers star / unstar transitions, access control, and the response
shape (star button HTML + OOB sidebar swap markup).
"""

from django.urls import reverse

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    return ws.owner, project


def _url(project):
    return reverse("web:toggle_project_favourite", kwargs={"slug_prefix": project.slug_prefix})


@pytest.mark.django_db
class TestToggleProjectFavourite:

    def test_star_adds_project(self, client, setup):
        user, project = setup
        client.force_login(user)
        assert not user.favourite_projects.filter(pk=project.pk).exists()
        resp = client.post(_url(project))
        assert resp.status_code == 200
        assert user.favourite_projects.filter(pk=project.pk).exists()

    def test_unstar_removes_project(self, client, setup):
        user, project = setup
        user.favourite_projects.add(project)
        client.force_login(user)
        resp = client.post(_url(project))
        assert resp.status_code == 200
        assert not user.favourite_projects.filter(pk=project.pk).exists()

    def test_response_contains_star_button(self, client, setup):
        user, project = setup
        client.force_login(user)
        resp = client.post(_url(project))
        # Star form id matches the toggle target for outerHTML swap.
        assert f"project-favourite-{project.slug_prefix}".encode() in resp.content

    def test_response_contains_sidebar_oob(self, client, setup):
        user, project = setup
        client.force_login(user)
        resp = client.post(_url(project))
        # OOB swap target for the sidebar favourites list.
        assert b"sidebar-favourites" in resp.content

    def test_foreign_project_404(self, client, setup):
        user, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        client.force_login(user)
        resp = client.post(_url(foreign_project))
        assert resp.status_code == 404
        assert not user.favourite_projects.filter(pk=foreign_project.pk).exists()

    def test_get_not_allowed(self, client, setup):
        user, project = setup
        client.force_login(user)
        resp = client.get(_url(project))
        assert resp.status_code == 405

    def test_anonymous_redirected(self, client, setup):
        _, project = setup
        resp = client.post(_url(project))
        assert resp.status_code in (302, 301)
