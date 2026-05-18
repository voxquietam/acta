"""Tests for the project icon picker endpoint.

Covers POST validation (curated subset only), clearing, foreign-project
access, and rendered-thumb response shape.
"""

from django.urls import reverse

import pytest

from apps.projects.icons import PROJECT_ICON_COLORS, PROJECT_ICONS
from apps.projects.tests.factories import ProjectFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """Workspace + project + member user."""
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    return ws.owner, project


def _icon_url(project):
    return reverse("web:set_project_icon", kwargs={"slug_prefix": project.slug_prefix})


@pytest.mark.django_db
class TestSetProjectIcon:

    def test_member_sets_curated_icon(self, client, setup):
        user, project = setup
        client.force_login(user)
        resp = client.post(_icon_url(project), {"icon": "rocket"})
        assert resp.status_code == 200
        project.refresh_from_db()
        assert project.icon == "rocket"
        # Response is the freshly rendered thumb partial.
        assert b"project-icon-thumb" in resp.content

    def test_empty_icon_clears_to_default(self, client, setup):
        user, project = setup
        project.icon = "rocket"
        project.save(update_fields=["icon"])
        client.force_login(user)
        resp = client.post(_icon_url(project), {"icon": ""})
        assert resp.status_code == 200
        project.refresh_from_db()
        assert project.icon == ""

    def test_non_curated_icon_rejected(self, client, setup):
        user, project = setup
        client.force_login(user)
        resp = client.post(_icon_url(project), {"icon": "skull-and-crossbones"})
        assert resp.status_code == 400
        project.refresh_from_db()
        assert project.icon != "skull-and-crossbones"

    def test_every_curated_icon_accepted(self, client, setup):
        """Each entry in the curated list must round-trip without 400."""
        user, project = setup
        client.force_login(user)
        for name in PROJECT_ICONS:
            resp = client.post(_icon_url(project), {"icon": name})
            assert resp.status_code == 200, f"curated icon {name!r} rejected"

    def test_foreign_project_returns_404(self, client, setup):
        user, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        client.force_login(user)
        resp = client.post(_icon_url(foreign_project), {"icon": "rocket"})
        assert resp.status_code == 404
        foreign_project.refresh_from_db()
        assert foreign_project.icon != "rocket"

    def test_get_not_allowed(self, client, setup):
        user, project = setup
        client.force_login(user)
        resp = client.get(_icon_url(project))
        assert resp.status_code == 405

    def test_anonymous_redirected(self, client, setup):
        _, project = setup
        resp = client.post(_icon_url(project), {"icon": "rocket"})
        assert resp.status_code in (302, 301)

    def test_color_only_post_keeps_icon(self, client, setup):
        """Submitting only ``icon_color`` must not blank the icon — the
        view path only updates the fields actually present in POST."""
        user, project = setup
        project.icon = "rocket"
        project.save(update_fields=["icon"])
        client.force_login(user)
        resp = client.post(_icon_url(project), {"icon_color": "blue"})
        assert resp.status_code == 200
        project.refresh_from_db()
        assert project.icon == "rocket"
        assert project.icon_color == "blue"

    def test_icon_only_post_keeps_color(self, setup, client):
        user, project = setup
        project.icon_color = "violet"
        project.save(update_fields=["icon_color"])
        client.force_login(user)
        resp = client.post(_icon_url(project), {"icon": "code"})
        assert resp.status_code == 200
        project.refresh_from_db()
        assert project.icon == "code"
        assert project.icon_color == "violet"

    def test_every_curated_color_accepted(self, client, setup):
        user, project = setup
        client.force_login(user)
        for color in PROJECT_ICON_COLORS:
            resp = client.post(_icon_url(project), {"icon_color": color})
            assert resp.status_code == 200, f"curated colour {color!r} rejected"

    def test_non_curated_color_rejected(self, client, setup):
        user, project = setup
        client.force_login(user)
        resp = client.post(_icon_url(project), {"icon_color": "hotpink"})
        assert resp.status_code == 400

    def test_empty_color_clears(self, client, setup):
        user, project = setup
        project.icon_color = "blue"
        project.save(update_fields=["icon_color"])
        client.force_login(user)
        resp = client.post(_icon_url(project), {"icon_color": ""})
        assert resp.status_code == 200
        project.refresh_from_db()
        assert project.icon_color == ""

    def test_icon_color_class_property(self, db):
        """``Project.icon_color_class`` resolves to the Tailwind utility."""
        from apps.projects.tests.factories import ProjectFactory

        p = ProjectFactory(icon_color="blue")
        assert p.icon_color_class == "text-blue-500"
        p2 = ProjectFactory(icon_color="")
        assert p2.icon_color_class == "text-subtle-foreground"
        p3 = ProjectFactory(icon_color="bogus")
        assert p3.icon_color_class == "text-subtle-foreground"
