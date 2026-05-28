"""Exclusive-group enforcement tests.

``LabelGroup.is_exclusive=True`` means a task can only carry one label
from that group at a time. The rule lives in
:mod:`apps.labels.services` and is exercised here directly, plus via
each call site (per-task toggle, task create, bulk add) so the
guarantee holds end-to-end.
"""

import pytest

from apps.labels.models import Label, LabelGroup
from apps.labels.palette import LABEL_COLORS
from apps.labels.services import add_labels_to_tasks, trim_exclusive_conflicts
from apps.tasks.bulk import _bulk_apply_labels
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory

GOOD_COLOR = LABEL_COLORS[0]


@pytest.fixture
def workspace(db):
    return WorkspaceFactory()


@pytest.fixture
def exclusive_group(workspace):
    return LabelGroup.objects.create(workspace=workspace, name="Kind", is_exclusive=True)


@pytest.fixture
def open_group(workspace):
    # ``Area`` collides with the default seeded group — use a unique name.
    return LabelGroup.objects.create(workspace=workspace, name="Topic", is_exclusive=False)


@pytest.mark.django_db
class TestAddLabelsToTasks:
    """End-to-end behaviour of the shared add-with-exclusivity helper."""

    def test_first_exclusive_label_attaches_normally(self, workspace, exclusive_group):
        bug = Label.objects.create(workspace=workspace, name="bug", color=GOOD_COLOR, group=exclusive_group)
        task = TaskFactory(project__workspace=workspace)
        add_labels_to_tasks([task.id], [bug.id])
        assert list(task.labels.values_list("name", flat=True)) == ["bug"]

    def test_second_exclusive_label_replaces_first(self, workspace, exclusive_group):
        """Adding a sibling label from the same exclusive group drops the prior one."""
        bug = Label.objects.create(workspace=workspace, name="bug", color=GOOD_COLOR, group=exclusive_group)
        feature = Label.objects.create(workspace=workspace, name="feature", color=GOOD_COLOR, group=exclusive_group)
        task = TaskFactory(project__workspace=workspace)
        add_labels_to_tasks([task.id], [bug.id])
        add_labels_to_tasks([task.id], [feature.id])
        assert list(task.labels.values_list("name", flat=True)) == ["feature"]

    def test_non_exclusive_groups_stack(self, workspace, open_group):
        """Open groups don't enforce single-pick — both labels survive."""
        auth = Label.objects.create(workspace=workspace, name="auth", color=GOOD_COLOR, group=open_group)
        chat = Label.objects.create(workspace=workspace, name="chat", color=GOOD_COLOR, group=open_group)
        task = TaskFactory(project__workspace=workspace)
        add_labels_to_tasks([task.id], [auth.id])
        add_labels_to_tasks([task.id], [chat.id])
        assert set(task.labels.values_list("name", flat=True)) == {"auth", "chat"}

    def test_labels_across_different_groups_coexist(self, workspace, exclusive_group, open_group):
        """Exclusivity is per-group, not workspace-wide."""
        bug = Label.objects.create(workspace=workspace, name="bug", color=GOOD_COLOR, group=exclusive_group)
        chat = Label.objects.create(workspace=workspace, name="chat", color=GOOD_COLOR, group=open_group)
        task = TaskFactory(project__workspace=workspace)
        add_labels_to_tasks([task.id], [bug.id, chat.id])
        assert set(task.labels.values_list("name", flat=True)) == {"bug", "chat"}

    def test_does_not_affect_other_tasks_in_workspace(self, workspace, exclusive_group):
        """Sibling-drop is scoped to the affected task ids only."""
        bug = Label.objects.create(workspace=workspace, name="bug", color=GOOD_COLOR, group=exclusive_group)
        feature = Label.objects.create(workspace=workspace, name="feature", color=GOOD_COLOR, group=exclusive_group)
        target = TaskFactory(project__workspace=workspace)
        bystander = TaskFactory(project__workspace=workspace)
        add_labels_to_tasks([bystander.id], [bug.id])
        add_labels_to_tasks([target.id], [bug.id])
        add_labels_to_tasks([target.id], [feature.id])
        # ``target`` was swapped; ``bystander`` still has ``bug``.
        assert list(target.labels.values_list("name", flat=True)) == ["feature"]
        assert list(bystander.labels.values_list("name", flat=True)) == ["bug"]


@pytest.mark.django_db
class TestTrimExclusiveConflicts:
    """First-wins dedup for the create-task path's label submission."""

    def test_drops_duplicate_exclusive_keeps_first(self, workspace, exclusive_group):
        bug = Label.objects.create(workspace=workspace, name="bug", color=GOOD_COLOR, group=exclusive_group)
        feature = Label.objects.create(workspace=workspace, name="feature", color=GOOD_COLOR, group=exclusive_group)
        trimmed = trim_exclusive_conflicts([bug.id, feature.id])
        assert trimmed == [bug.id]

    def test_keeps_non_exclusive_duplicates_alone(self, workspace, open_group):
        auth = Label.objects.create(workspace=workspace, name="auth", color=GOOD_COLOR, group=open_group)
        chat = Label.objects.create(workspace=workspace, name="chat", color=GOOD_COLOR, group=open_group)
        trimmed = trim_exclusive_conflicts([auth.id, chat.id])
        assert set(trimmed) == {auth.id, chat.id}

    def test_mixed_input_preserves_order_except_dropped(self, workspace, exclusive_group, open_group):
        bug = Label.objects.create(workspace=workspace, name="bug", color=GOOD_COLOR, group=exclusive_group)
        feature = Label.objects.create(workspace=workspace, name="feature", color=GOOD_COLOR, group=exclusive_group)
        auth = Label.objects.create(workspace=workspace, name="auth", color=GOOD_COLOR, group=open_group)
        trimmed = trim_exclusive_conflicts([bug.id, auth.id, feature.id])
        assert trimmed == [bug.id, auth.id]

    def test_empty_input_returns_empty(self):
        assert trim_exclusive_conflicts([]) == []


@pytest.mark.django_db
class TestBulkApplyLabelsExclusivity:
    """Bulk endpoint's label-add path routes through the exclusivity rule too."""

    def test_bulk_add_swaps_siblings_per_task(self, workspace, exclusive_group):
        bug = Label.objects.create(workspace=workspace, name="bug", color=GOOD_COLOR, group=exclusive_group)
        feature = Label.objects.create(workspace=workspace, name="feature", color=GOOD_COLOR, group=exclusive_group)
        t1 = TaskFactory(project__workspace=workspace)
        t2 = TaskFactory(project__workspace=workspace)
        # Seed: both tasks carry ``bug``.
        _bulk_apply_labels([t1.id, t2.id], [bug.id], [])
        # Bulk-add ``feature`` (same exclusive group) — ``bug`` must drop.
        _bulk_apply_labels([t1.id, t2.id], [feature.id], [])
        assert list(t1.labels.values_list("name", flat=True)) == ["feature"]
        assert list(t2.labels.values_list("name", flat=True)) == ["feature"]
