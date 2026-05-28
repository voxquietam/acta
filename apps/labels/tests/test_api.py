"""Integration tests for ``LabelViewSet`` and ``LabelGroupViewSet``.

Covers workspace scoping, the membership guard on create, the colour
validator, and the label/group same-workspace invariant.
"""

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.labels.models import Label, LabelGroup
from apps.labels.tests.factories import LabelFactory, LabelGroupFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def member():
    return UserFactory()


@pytest.fixture
def workspace(member):
    return WorkspaceFactory(owner=member)


@pytest.fixture
def client(member):
    c = APIClient()
    c.force_authenticate(member)
    return c


@pytest.mark.django_db
class TestLabelCrud:
    def test_create_label(self, client, workspace):
        resp = client.post(
            "/api/v1/labels/",
            {"workspace": workspace.id, "name": "bug", "color": "#ff0000"},
        )
        assert resp.status_code == 201, resp.content
        assert Label.objects.filter(id=resp.data["id"], name="bug").exists()

    def test_cannot_create_in_foreign_workspace(self, client):
        foreign = WorkspaceFactory()
        resp = client.post(
            "/api/v1/labels/",
            {"workspace": foreign.id, "name": "x", "color": "#ffffff"},
        )
        assert resp.status_code == 400
        assert "workspace" in resp.data

    def test_invalid_color_rejected(self, client, workspace):
        resp = client.post(
            "/api/v1/labels/",
            {"workspace": workspace.id, "name": "x", "color": "notacolor"},
        )
        assert resp.status_code == 400
        assert "color" in resp.data

    def test_list_scoped_to_membership(self, client, workspace):
        mine = LabelFactory(workspace=workspace)
        LabelFactory()  # foreign
        resp = client.get("/api/v1/labels/")
        ids = {row["id"] for row in resp.data["results"]}
        assert ids == {mine.id}

    def test_retrieve_foreign_label_404(self, client):
        foreign = LabelFactory()
        resp = client.get(f"/api/v1/labels/{foreign.id}/")
        assert resp.status_code == 404

    def test_group_must_be_same_workspace(self, client, workspace):
        foreign_group = LabelGroupFactory()  # different workspace
        resp = client.post(
            "/api/v1/labels/",
            {"workspace": workspace.id, "name": "x", "color": "#abcabc", "group": foreign_group.id},
        )
        assert resp.status_code == 400
        assert "group" in resp.data


@pytest.mark.django_db
class TestLabelGroupCrud:
    def test_create_group(self, client, workspace):
        resp = client.post(
            "/api/v1/label-groups/",
            {"workspace": workspace.id, "name": "Priority", "is_exclusive": True},
        )
        assert resp.status_code == 201, resp.content
        assert LabelGroup.objects.filter(id=resp.data["id"], is_exclusive=True).exists()

    def test_cannot_create_in_foreign_workspace(self, client):
        foreign = WorkspaceFactory()
        resp = client.post(
            "/api/v1/label-groups/",
            {"workspace": foreign.id, "name": "x"},
        )
        assert resp.status_code == 400
        assert "workspace" in resp.data

    def test_list_scoped_to_membership(self, client, workspace):
        mine = LabelGroupFactory(workspace=workspace)
        foreign = LabelGroupFactory()
        resp = client.get("/api/v1/label-groups/")
        ids = {row["id"] for row in resp.data["results"]}
        # Workspace creation auto-seeds the default groups (Type / Area / Layer
        # via ``apps.labels.signals``) so a strict equality would also pin
        # those — match on inclusion / exclusion instead.
        assert mine.id in ids
        assert foreign.id not in ids
