"""Workspace slug rules — immutable once set, reserved names refused.

The slug is the workspace's segment in every URL it owns (ADR 0031), so it
is fixed at creation and may not collide with the root paths that stay
outside the workspace namespace.
"""

from django.core.exceptions import ValidationError
from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.workspaces.models import RESERVED_WORKSPACE_SLUGS, Workspace
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestSlugImmutability:
    def test_slug_cannot_change_after_creation(self):
        workspace = WorkspaceFactory(slug="ksu24")
        workspace.slug = "ksu24-renamed"
        with pytest.raises(ValidationError) as exc:
            workspace.full_clean()
        assert "slug" in exc.value.message_dict

    def test_name_is_still_editable(self):
        """Renaming a workspace changes the display name, not the URL."""
        workspace = WorkspaceFactory(slug="ksu24", name="KSU24")
        workspace.name = "KSU24 (renamed)"
        workspace.full_clean()
        workspace.save()
        workspace.refresh_from_db()
        assert workspace.name == "KSU24 (renamed)"
        assert workspace.slug == "ksu24"


@pytest.mark.django_db
class TestReservedSlugs:
    def test_reserved_slug_rejected_by_the_model(self):
        workspace = WorkspaceFactory.build(slug="api", owner=UserFactory())
        with pytest.raises(ValidationError) as exc:
            workspace.full_clean()
        assert "slug" in exc.value.message_dict

    def test_reserved_slug_rejected_on_create(self, client):
        """A workspace slugged ``admin`` would be unreachable, so refuse it.

        Django resolves URL patterns in order and the root paths are declared
        first, so such a workspace would exist in the sidebar and 404 (or
        show the Django admin) on every one of its own URLs.
        """
        user = UserFactory()
        client.force_login(user)
        response = client.post(
            reverse("web:create_workspace"),
            {"name": "Admin things", "slug": "admin"},
        )
        assert response.status_code == 400
        assert not Workspace.objects.filter(slug="admin").exists()

    def test_derived_slug_skips_a_reserved_word(self, client):
        """ "API" derives to "api", which is reserved — take the next free one."""
        user = UserFactory()
        client.force_login(user)
        response = client.post(reverse("web:create_workspace"), {"name": "API"})
        assert response.status_code in (200, 204, 302)
        created = Workspace.objects.get(owner=user)
        assert created.slug not in RESERVED_WORKSPACE_SLUGS
        assert created.slug.startswith("api-")

    def test_ordinary_slug_still_works(self, client):
        user = UserFactory()
        client.force_login(user)
        client.post(reverse("web:create_workspace"), {"name": "Acme", "slug": "acme"})
        assert Workspace.objects.filter(slug="acme").exists()
