"""Diff-based activity events for :class:`Task` mutations."""

import datetime as dt
from uuid import uuid4

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.labels.tests.factories import LabelFactory
from apps.tasks.events import build_diff_events, emit_task_diff_events, snapshot_task
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory


def _event_types(events):
    """Return the list of ``event_type`` strings for assertion convenience."""
    return [e.event_type for e in events]


@pytest.mark.django_db
class TestBuildDiffEventsWatchedFields:
    """Each watched field gets its own ``task.*`` event when it changes."""

    def test_status_change_emits_status_changed(self):
        task = TaskFactory(status=Task.STATUS_TODO)
        old = snapshot_task(task)
        task.status = Task.STATUS_IN_PROGRESS
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        assert "task.status_changed" in _event_types(events)
        e = next(e for e in events if e.event_type == "task.status_changed")
        assert e.payload == {"from": "to-do", "to": "in-progress"}

    def test_assignee_change_emits_assigned(self):
        task = TaskFactory()
        old = snapshot_task(task)
        new_assignee = UserFactory()
        task.assignee = new_assignee
        task.save()
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        e = next(e for e in events if e.event_type == "task.assigned")
        assert e.payload == {"from_user_id": None, "to_user_id": new_assignee.id}

    def test_due_change_emits_due_changed_iso(self):
        task = TaskFactory()
        old = snapshot_task(task)
        task.due_date = dt.date(2026, 6, 1)
        task.save()
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        e = next(e for e in events if e.event_type == "task.due_changed")
        assert e.payload == {"from": None, "to": "2026-06-01"}

    def test_priority_change_emits_priority_changed(self):
        task = TaskFactory(priority=Task.NO_PRIORITY)
        old = snapshot_task(task)
        task.priority = Task.HIGH
        task.save()
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        e = next(e for e in events if e.event_type == "task.priority_changed")
        assert e.payload == {"from": 0, "to": 2}

    def test_label_add_emits_labels_changed(self):
        task = TaskFactory()
        old = snapshot_task(task)
        label = LabelFactory(workspace=task.project.workspace)
        task.labels.add(label)
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        e = next(e for e in events if e.event_type == "task.labels_changed")
        assert e.payload == {"added_ids": [label.id], "removed_ids": []}

    def test_parent_change_emits_parent_changed(self):
        parent = TaskFactory()
        child = TaskFactory(project=parent.project)
        old = snapshot_task(child)
        child.parent = parent
        child.save()
        events = build_diff_events(old_state=old, task=child, actor=child.reporter)
        e = next(e for e in events if e.event_type == "task.parent_changed")
        assert e.payload == {"from_task_id": None, "to_task_id": parent.id}


@pytest.mark.django_db
class TestBuildDiffEventsCatchAll:
    """Title / description / size / project / number land in ``task.updated``."""

    def test_title_change_in_task_updated(self):
        task = TaskFactory(title="old")
        old = snapshot_task(task)
        task.title = "new"
        task.save()
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        e = next(e for e in events if e.event_type == "task.updated")
        assert e.payload["changes"]["title"] == {"old": "old", "new": "new"}

    def test_description_change_records_lengths_not_text(self):
        task = TaskFactory(description="short")
        old = snapshot_task(task)
        task.description = "a longer one"
        task.save()
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        e = next(e for e in events if e.event_type == "task.updated")
        ch = e.payload["changes"]["description"]
        assert ch == {"old_len": len("short"), "new_len": len("a longer one")}

    def test_size_change_in_task_updated(self):
        task = TaskFactory(size=None)
        old = snapshot_task(task)
        task.size = 3
        task.save()
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        e = next(e for e in events if e.event_type == "task.updated")
        assert e.payload["changes"]["size"] == {"old": None, "new": 3}

    def test_no_change_no_events(self):
        task = TaskFactory()
        old = snapshot_task(task)
        # No mutation.
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        assert events == []


@pytest.mark.django_db
class TestEmitTaskDiffEvents:
    """End-to-end: emit persists rows in a single ``bulk_create``."""

    def test_single_save_per_event_batch(self, django_assert_max_num_queries):
        """Multiple changed fields → exactly ONE INSERT via bulk_create.

        Within ``emit_task_diff_events`` the only INSERT we control is
        the activity log batch. ``build_diff_events`` may also fetch the
        task's current labels to compute the diff — that's a separate
        SELECT. The cap (2) reflects ``SELECT labels + INSERT events``.
        What the test prevents is a regression to N separate INSERTs
        for N events (which would push the cap to 1 + N).
        """
        task = TaskFactory()
        old = snapshot_task(task)
        task.status = Task.STATUS_DONE
        task.priority = Task.URGENT
        task.save()
        with django_assert_max_num_queries(2):
            count = emit_task_diff_events(
                old_state=old,
                task=task,
                actor=task.reporter,
            )
        assert count == 2
        types = set(
            ActivityLog.objects.filter(target_id=task.id).values_list("event_type", flat=True),
        )
        assert types == {"task.status_changed", "task.priority_changed"}

    def test_bulk_id_propagated_to_each_event(self):
        task = TaskFactory()
        old = snapshot_task(task)
        task.status = Task.STATUS_DONE
        task.priority = Task.HIGH
        task.save()
        bid = uuid4()
        emit_task_diff_events(old_state=old, task=task, actor=task.reporter, bulk_id=bid)
        bids = set(
            ActivityLog.objects.filter(target_id=task.id).values_list("bulk_id", flat=True),
        )
        assert bids == {bid}
