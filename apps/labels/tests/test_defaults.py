"""Default-group seeding tests.

Covers :func:`apps.labels.defaults.seed_default_label_groups` and the
``post_save`` signal that fans it out to every freshly created
:class:`~apps.workspaces.models.Workspace`. The signal is the only
production caller — the helper itself only fills missing names, so the
"deleted default stays deleted" guarantee comes from the signal's
``created=True`` guard, not from the helper.
"""

import pytest

from apps.labels.defaults import DEFAULT_LABEL_GROUPS, seed_default_label_groups
from apps.labels.models import LabelGroup
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestSeedDefaultLabelGroups:
    """End-to-end behaviour of the seeder + its lone signal caller."""

    def test_new_workspace_gets_all_defaults_via_signal(self):
        """``post_save`` on Workspace creation seeds the full default set."""
        ws = WorkspaceFactory()
        seeded = {g.name for g in LabelGroup.objects.filter(workspace=ws)}
        expected = {seed["name"] for seed in DEFAULT_LABEL_GROUPS}
        assert seeded == expected

    def test_seeded_groups_carry_description_and_exclusivity(self):
        """Each seeded group ships with its description and ``is_exclusive`` flag."""
        ws = WorkspaceFactory()
        groups = {g.name: g for g in LabelGroup.objects.filter(workspace=ws)}
        for seed in DEFAULT_LABEL_GROUPS:
            row = groups[seed["name"]]
            assert row.description == seed["description"]
            assert row.is_exclusive == seed["is_exclusive"]

    def test_workspace_update_does_not_re_seed(self):
        """Re-saving a workspace must not re-run the seeder.

        Pins the signal's ``created=True`` guard: a team that deleted
        the ``Layer`` group won't see it reappear next time someone
        renames the workspace or toggles a setting.
        """
        ws = WorkspaceFactory()
        LabelGroup.objects.filter(workspace=ws, name="Layer").delete()
        ws.name = ws.name + " (renamed)"
        ws.save()
        assert not LabelGroup.objects.filter(workspace=ws, name="Layer").exists()

    def test_seed_helper_is_idempotent_by_name(self):
        """Calling the helper a second time on a seeded workspace creates nothing.

        Documents the function-level contract — "fill missing names". The
        signal handler is the only production caller and runs exactly
        once per workspace (on create), so this dedup never fires in
        practice; it just keeps the helper safe to re-run by hand.
        """
        ws = WorkspaceFactory()
        created = seed_default_label_groups(ws)
        assert created == 0
        assert LabelGroup.objects.filter(workspace=ws).count() == len(DEFAULT_LABEL_GROUPS)

    def test_workspaces_are_isolated(self):
        """Each workspace gets its own copy — seeding one doesn't touch another."""
        ws1 = WorkspaceFactory()
        ws2 = WorkspaceFactory()
        assert LabelGroup.objects.filter(workspace=ws1).count() == len(DEFAULT_LABEL_GROUPS)
        assert LabelGroup.objects.filter(workspace=ws2).count() == len(DEFAULT_LABEL_GROUPS)
        # Deleting one workspace's groups doesn't disturb the other's.
        LabelGroup.objects.filter(workspace=ws1).delete()
        assert LabelGroup.objects.filter(workspace=ws2).count() == len(DEFAULT_LABEL_GROUPS)
