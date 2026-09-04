"""Canonical workspace-scoped routes (ADR 0031).

Every web section is reachable twice: at its legacy path and under
``/<workspace_slug>/``. The canonical form is generated from the legacy
patterns at import time, so these tests guard the generation itself as much
as the individual routes — if the mirroring breaks, everything below 404s.
"""

from django.test import Client
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
        """Six months of links are already out there; they keep landing.

        They redirect to the canonical form rather than serving directly,
        so what matters is that following one still reaches the page.
        """
        user, _, project, task = workspace_setup
        client.force_login(user)
        for path in (
            "/my-work/",
            "/projects/",
            f"/projects/{project.slug_prefix}/{task.number}/",
        ):
            assert client.get(path, follow=True).status_code == 200, path

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


@pytest.fixture
def raw_client():
    """A client that does NOT follow redirects.

    The project-wide ``client`` fixture follows them, which is right for
    suites asking "does this page render". This one is for the tests that
    are about the redirect itself.
    """
    return Client()


@pytest.mark.django_db
class TestLegacyRedirects:
    """Legacy paths keep working, but nudge the browser to the canonical one.

    The old URLs can never be retired — six months of them are in Telegram
    messages and bookmarks — so they resolve forever. A 301 just means what
    the user copies out of the address bar afterwards is unambiguous.
    """

    def test_legacy_page_redirects(self, raw_client, workspace_setup):
        user, workspace, _, _ = workspace_setup
        raw_client.force_login(user)
        response = raw_client.get("/my-work/")
        assert response.status_code == 301
        assert response["Location"] == f"/{workspace.slug}/my-work/"

    def test_querystring_survives(self, raw_client, workspace_setup):
        """Filters live in the querystring; losing them would break links."""
        user, workspace, _, _ = workspace_setup
        raw_client.force_login(user)
        response = raw_client.get("/tasks/", {"status": "to-do"})
        assert response["Location"] == f"/{workspace.slug}/tasks/?status=to-do"

    def test_legacy_task_and_project_redirect(self, raw_client, workspace_setup):
        """These resolve their workspace from the record, not the viewer."""
        user, workspace, project, task = workspace_setup
        raw_client.force_login(user)
        task_response = raw_client.get(f"/projects/{project.slug_prefix}/{task.number}/")
        assert task_response["Location"] == f"/{workspace.slug}/projects/{project.slug_prefix}/{task.number}/"
        project_response = raw_client.get(f"/projects/{project.slug_prefix}/")
        assert project_response["Location"] == f"/{workspace.slug}/projects/{project.slug_prefix}/"

    def test_canonical_url_does_not_redirect(self, raw_client, workspace_setup):
        """Guards against a redirect loop."""
        user, workspace, _, _ = workspace_setup
        raw_client.force_login(user)
        assert raw_client.get(f"/{workspace.slug}/my-work/").status_code == 200

    def test_htmx_fragments_are_left_alone(self, raw_client, workspace_setup):
        """A fragment answers into a target; redirecting swaps a whole page in."""
        user, _, project, task = workspace_setup
        raw_client.force_login(user)
        assert raw_client.get("/my-work/", HTTP_HX_REQUEST="true").status_code == 200
        modal = raw_client.get(
            f"/projects/{project.slug_prefix}/{task.number}/",
            {"modal": "1"},
            HTTP_HX_REQUEST="true",
        )
        assert modal.status_code == 200

    def test_post_endpoints_are_not_redirected(self, raw_client, workspace_setup):
        """A 301 on POST is replayed as GET, dropping the body of every form."""
        user, _, project, task = workspace_setup
        raw_client.force_login(user)
        response = raw_client.post(
            f"/projects/{project.slug_prefix}/{task.number}/priority/",
            {"priority": 2},
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code != 301
        task.refresh_from_db()
        assert task.priority == 2
