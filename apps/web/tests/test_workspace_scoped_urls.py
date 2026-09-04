"""Canonical workspace-scoped routes (ADR 0031).

Every web section is reachable twice: at its legacy path and under
``/<workspace_slug>/``. The canonical form is generated from the legacy
patterns at import time, so these tests guard the generation itself as much
as the individual routes — if the mirroring breaks, everything below 404s.
"""

from django.urls import Resolver404, resolve, reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def workspace_setup(db):
    """A workspace with a project, a task, and a member.

    Returns:
        Tuple ``(user, workspace, project, task)``.
    """
    workspace = WorkspaceFactory(slug="ksu24")
    project = ProjectFactory(workspace=workspace, slug_prefix="SER")
    task = TaskFactory(project=project, reporter=workspace.owner)
    return workspace.owner, workspace, project, task


@pytest.mark.django_db
class TestCanonicalRoutes:
    def test_sections_resolve_under_a_workspace(self, client, workspace_setup):
        user, workspace, project, task = workspace_setup
        client.force_login(user)
        for path in (
            f"/{workspace.slug}/my-work/",
            f"/{workspace.slug}/projects/",
            f"/{workspace.slug}/projects/{project.slug_prefix}/",
            f"/{workspace.slug}/projects/{project.slug_prefix}/{task.number}/",
        ):
            assert client.get(path).status_code == 200, path

    def test_legacy_paths_still_work(self, client, workspace_setup):
        """Six months of links are already out there; they keep resolving."""
        user, _, project, task = workspace_setup
        client.force_login(user)
        for path in (
            "/my-work/",
            "/projects/",
            f"/projects/{project.slug_prefix}/{task.number}/",
        ):
            assert client.get(path).status_code == 200, path

    def test_reverse_produces_both_forms(self, workspace_setup):
        _, workspace, _, _ = workspace_setup
        assert reverse("web:my_work") == "/my-work/"
        assert reverse("web_ws:my_work", kwargs={"workspace": workspace.slug}) == f"/{workspace.slug}/my-work/"

    def test_section_name_is_not_swallowed_as_a_workspace(self):
        """``/projects/`` must stay the section, not the workspace "projects".

        The workspace segment matches any single path component, so the
        canonical patterns are mounted last. Getting that order wrong sends
        every legacy root path to the dashboard instead.
        """
        match = resolve("/projects/")
        assert match.namespace == "web"
        assert match.url_name == "project_list"


@pytest.mark.django_db
class TestCanonicalRouteAccess:
    def test_url_workspace_becomes_the_active_one(self, client, workspace_setup):
        """The URL is the most explicit intent there is, so it wins."""
        user, workspace, _, _ = workspace_setup
        other = WorkspaceFactory(slug="elsewhere")
        other.memberships.create(user=user)
        user.active_workspace = other
        user.save(update_fields=["active_workspace"])

        client.force_login(user)
        client.get(f"/{workspace.slug}/my-work/")
        user.refresh_from_db()
        assert user.active_workspace_id == workspace.pk

    def test_foreign_workspace_is_404_not_403(self, client, workspace_setup):
        """403 would confirm the workspace exists to anyone guessing slugs."""
        _, _, _, _ = workspace_setup
        outsider = UserFactory()
        own = WorkspaceFactory(slug="outsiders-own")
        own.memberships.create(user=outsider)
        client.force_login(outsider)
        assert client.get("/ksu24/my-work/").status_code == 404

    def test_unknown_workspace_is_404(self, client, workspace_setup):
        user, _, _, _ = workspace_setup
        client.force_login(user)
        assert client.get("/no-such-workspace/my-work/").status_code == 404

    def test_reserved_root_paths_are_not_workspaces(self, client, workspace_setup):
        """``/admin/`` must reach the admin, never a workspace lookup."""
        user, _, _, _ = workspace_setup
        client.force_login(user)
        # Resolving is enough — the admin itself redirects unauthenticated
        # staff, which is not what this test is about.
        try:
            match = resolve("/admin/")
        except Resolver404:  # pragma: no cover — would mean admin is unmounted
            pytest.fail("/admin/ no longer resolves")
        assert match.namespace != "web_ws"
