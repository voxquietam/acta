"""Tests for the labels / label-groups management endpoints.

Covers CRUD round-trips, membership scoping, validation errors (name
collisions, off-palette colours, cross-workspace group ids), and the
drag-drop ``reorder_labels`` semantics. Every member can mutate by
design — there is no admin-only gate to verify here.
"""

import json

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.labels.models import Label, LabelGroup
from apps.labels.palette import LABEL_COLORS
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory

GOOD_COLOR = LABEL_COLORS[0]


@pytest.fixture
def workspace(db):
    return WorkspaceFactory()


@pytest.fixture
def member(workspace):
    user = UserFactory()
    WorkspaceMemberFactory(workspace=workspace, user=user, role=WorkspaceMember.MEMBER)
    return user


@pytest.fixture
def outsider(db):
    return UserFactory()


def _url(name, workspace, **kwargs):
    return reverse(f"web:{name}", kwargs={"slug": workspace.slug, **kwargs})


@pytest.mark.django_db
class TestCreateLabelGroup:
    def test_member_can_create(self, client, workspace, member):
        client.force_login(member)
        resp = client.post(_url("create_label_group", workspace), {"name": "Custom", "description": "Notes"})
        assert resp.status_code == 200
        group = LabelGroup.objects.get(workspace=workspace, name="Custom")
        assert group.description == "Notes"
        assert group.is_exclusive is False
        # Toast + acta:labels-changed both fire on success.
        triggers = json.loads(resp.headers["HX-Trigger"])
        assert "acta:labels-changed" in triggers
        assert triggers["acta:toast"]["level"] == "success"

    def test_exclusive_flag_persists(self, client, workspace, member):
        client.force_login(member)
        client.post(
            _url("create_label_group", workspace),
            {"name": "Mutex", "is_exclusive": "1"},
        )
        assert LabelGroup.objects.get(workspace=workspace, name="Mutex").is_exclusive is True

    def test_empty_name_rejected(self, client, workspace, member):
        client.force_login(member)
        resp = client.post(_url("create_label_group", workspace), {"name": "   "})
        assert resp.status_code == 400
        assert not LabelGroup.objects.filter(workspace=workspace, name="").exists()

    def test_duplicate_name_rejected_case_insensitive(self, client, workspace, member):
        LabelGroup.objects.create(workspace=workspace, name="Risks")
        client.force_login(member)
        resp = client.post(_url("create_label_group", workspace), {"name": "risks"})
        assert resp.status_code == 400

    def test_outsider_gets_404(self, client, workspace, outsider):
        client.force_login(outsider)
        resp = client.post(_url("create_label_group", workspace), {"name": "X"})
        assert resp.status_code == 404


@pytest.mark.django_db
class TestUpdateLabelGroup:
    def test_rename_and_re_describe(self, client, workspace, member):
        group = LabelGroup.objects.create(workspace=workspace, name="Old", description="")
        client.force_login(member)
        resp = client.post(
            _url("update_label_group", workspace, group_id=group.id),
            {"name": "New", "description": "Fresh hint", "is_exclusive": "1"},
        )
        assert resp.status_code == 200
        group.refresh_from_db()
        assert group.name == "New"
        assert group.description == "Fresh hint"
        assert group.is_exclusive is True

    def test_rename_to_clash_rejected(self, client, workspace, member):
        LabelGroup.objects.create(workspace=workspace, name="Risks")
        other = LabelGroup.objects.create(workspace=workspace, name="Capacity")
        client.force_login(member)
        resp = client.post(
            _url("update_label_group", workspace, group_id=other.id),
            {"name": "risks"},
        )
        assert resp.status_code == 400
        other.refresh_from_db()
        assert other.name == "Capacity"

    def test_cross_workspace_group_id_404(self, client, workspace, member):
        foreign = LabelGroup.objects.create(workspace=WorkspaceFactory(), name="X")
        client.force_login(member)
        resp = client.post(_url("update_label_group", workspace, group_id=foreign.id), {"name": "Y"})
        assert resp.status_code == 404


@pytest.mark.django_db
class TestDeleteLabelGroup:
    def test_group_removed_labels_become_ungrouped(self, client, workspace, member):
        group = LabelGroup.objects.create(workspace=workspace, name="Drop")
        label = Label.objects.create(workspace=workspace, name="orphan", color=GOOD_COLOR, group=group)
        client.force_login(member)
        resp = client.post(_url("delete_label_group", workspace, group_id=group.id))
        assert resp.status_code == 200
        assert not LabelGroup.objects.filter(pk=group.id).exists()
        label.refresh_from_db()
        assert label.group_id is None  # SET_NULL kept the label alive


@pytest.mark.django_db
class TestCreateLabel:
    def test_member_can_create_ungrouped(self, client, workspace, member):
        client.force_login(member)
        resp = client.post(
            _url("create_label", workspace),
            {"name": "urgent", "color": GOOD_COLOR},
        )
        assert resp.status_code == 200
        label = Label.objects.get(workspace=workspace, name="urgent")
        assert label.group_id is None
        assert label.position == 1

    def test_create_inside_group(self, client, workspace, member):
        group = LabelGroup.objects.create(workspace=workspace, name="Risks")
        client.force_login(member)
        resp = client.post(
            _url("create_label", workspace),
            {"name": "bug", "color": GOOD_COLOR, "group": str(group.id)},
        )
        assert resp.status_code == 200
        assert Label.objects.get(workspace=workspace, name="bug").group_id == group.id

    def test_position_appends_to_existing_group(self, client, workspace, member):
        group = LabelGroup.objects.create(workspace=workspace, name="Risks")
        Label.objects.create(workspace=workspace, name="a", color=GOOD_COLOR, group=group, position=1)
        Label.objects.create(workspace=workspace, name="b", color=GOOD_COLOR, group=group, position=2)
        client.force_login(member)
        client.post(
            _url("create_label", workspace),
            {"name": "c", "color": GOOD_COLOR, "group": str(group.id)},
        )
        assert Label.objects.get(workspace=workspace, name="c").position == 3

    def test_off_palette_color_rejected(self, client, workspace, member):
        client.force_login(member)
        resp = client.post(
            _url("create_label", workspace),
            {"name": "x", "color": "#123456"},  # not in LABEL_COLORS
        )
        assert resp.status_code == 400
        assert not Label.objects.filter(workspace=workspace, name="x").exists()

    def test_empty_name_rejected(self, client, workspace, member):
        client.force_login(member)
        resp = client.post(_url("create_label", workspace), {"name": "", "color": GOOD_COLOR})
        assert resp.status_code == 400

    def test_duplicate_name_rejected(self, client, workspace, member):
        Label.objects.create(workspace=workspace, name="dup", color=GOOD_COLOR)
        client.force_login(member)
        resp = client.post(_url("create_label", workspace), {"name": "DUP", "color": GOOD_COLOR})
        assert resp.status_code == 400

    def test_cross_workspace_group_id_rejected(self, client, workspace, member):
        foreign_group = LabelGroup.objects.create(workspace=WorkspaceFactory(), name="F")
        client.force_login(member)
        resp = client.post(
            _url("create_label", workspace),
            {"name": "x", "color": GOOD_COLOR, "group": str(foreign_group.id)},
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestUpdateLabel:
    def test_rename_and_recolour(self, client, workspace, member):
        label = Label.objects.create(workspace=workspace, name="old", color=GOOD_COLOR)
        client.force_login(member)
        resp = client.post(
            _url("update_label", workspace, label_id=label.id),
            {"name": "new", "color": LABEL_COLORS[1]},
        )
        assert resp.status_code == 200
        label.refresh_from_db()
        assert label.name == "new"
        assert label.color == LABEL_COLORS[1]

    def test_move_to_another_group_resets_position(self, client, workspace, member):
        src = LabelGroup.objects.create(workspace=workspace, name="Src")
        dst = LabelGroup.objects.create(workspace=workspace, name="Dst")
        Label.objects.create(workspace=workspace, name="a", color=GOOD_COLOR, group=dst, position=1)
        label = Label.objects.create(workspace=workspace, name="b", color=GOOD_COLOR, group=src, position=1)
        client.force_login(member)
        client.post(
            _url("update_label", workspace, label_id=label.id),
            {"name": "b", "color": GOOD_COLOR, "group": str(dst.id)},
        )
        label.refresh_from_db()
        assert label.group_id == dst.id
        assert label.position == 2  # appended after ``a``


@pytest.mark.django_db
class TestDeleteLabel:
    def test_delete_cascades_m2m(self, client, workspace, member):
        label = Label.objects.create(workspace=workspace, name="rip", color=GOOD_COLOR)
        task = TaskFactory(project__workspace=workspace)
        task.labels.add(label)
        assert task.labels.count() == 1
        client.force_login(member)
        resp = client.post(_url("delete_label", workspace, label_id=label.id))
        assert resp.status_code == 200
        assert not Label.objects.filter(pk=label.id).exists()
        assert task.labels.count() == 0


@pytest.mark.django_db
class TestReorderLabels:
    def test_persists_new_order_and_group(self, client, workspace, member):
        src = LabelGroup.objects.create(workspace=workspace, name="Src")
        dst = LabelGroup.objects.create(workspace=workspace, name="Dst")
        a = Label.objects.create(workspace=workspace, name="a", color=GOOD_COLOR, group=src, position=1)
        b = Label.objects.create(workspace=workspace, name="b", color=GOOD_COLOR, group=src, position=2)
        c = Label.objects.create(workspace=workspace, name="c", color=GOOD_COLOR, group=dst, position=1)
        client.force_login(member)
        # Move ``a`` into ``dst`` between ``c`` and (nothing) — final order: c, a.
        resp = client.post(
            _url("reorder_labels", workspace),
            {"group": str(dst.id), "label_ids": [str(c.id), str(a.id)]},
        )
        assert resp.status_code == 204
        a.refresh_from_db()
        b.refresh_from_db()
        c.refresh_from_db()
        assert (a.group_id, a.position) == (dst.id, 2)
        assert (c.group_id, c.position) == (dst.id, 1)
        # ``b`` was untouched by this slice (still in ``src``).
        assert b.group_id == src.id

    def test_label_outside_workspace_rejected(self, client, workspace, member):
        foreign = Label.objects.create(workspace=WorkspaceFactory(), name="x", color=GOOD_COLOR)
        client.force_login(member)
        resp = client.post(
            _url("reorder_labels", workspace),
            {"group": "", "label_ids": [str(foreign.id)]},
        )
        assert resp.status_code == 400

    def test_to_ungrouped(self, client, workspace, member):
        group = LabelGroup.objects.create(workspace=workspace, name="G")
        label = Label.objects.create(workspace=workspace, name="x", color=GOOD_COLOR, group=group, position=1)
        client.force_login(member)
        resp = client.post(
            _url("reorder_labels", workspace),
            {"group": "", "label_ids": [str(label.id)]},
        )
        assert resp.status_code == 204
        label.refresh_from_db()
        assert label.group_id is None
        assert label.position == 1
