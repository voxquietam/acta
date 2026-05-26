"""Diff-based activity events for :class:`Task` mutations."""

import datetime as dt
from uuid import uuid4

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
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

    def test_archive_emits_task_archived(self):
        from django.utils import timezone as tz

        task = TaskFactory()
        old = snapshot_task(task)
        task.archived_at = tz.now()
        task.save()
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        assert "task.archived" in _event_types(events)

    def test_unarchive_emits_task_unarchived(self):
        from django.utils import timezone as tz

        task = TaskFactory()
        task.archived_at = tz.now()
        task.save()
        old = snapshot_task(task)
        task.archived_at = None
        task.save()
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        assert "task.unarchived" in _event_types(events)

    def test_archive_timestamp_bump_alone_emits_nothing(self):
        """Two non-null archived_at values produce no diff event — the
        archive *transition* is what matters, not the timestamp itself."""
        from django.utils import timezone as tz

        task = TaskFactory()
        task.archived_at = tz.now() - dt.timedelta(days=1)
        task.save()
        old = snapshot_task(task)
        task.archived_at = tz.now()
        task.save()
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        assert "task.archived" not in _event_types(events)
        assert "task.unarchived" not in _event_types(events)


@pytest.mark.django_db
class TestBuildDiffEventsCatchAll:
    """Title / description / size land in ``task.updated``.

    A project / number change is reported by the dedicated
    ``task.project_changed`` event instead — see
    :class:`TestBuildDiffEventsProjectChange`.
    """

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
class TestBuildDiffEventsProjectChange:
    """Moving a task to another project emits ``task.project_changed``."""

    def test_project_change_emits_dedicated_event(self):
        task = TaskFactory()
        old = snapshot_task(task)
        target = ProjectFactory(workspace=task.project.workspace)
        task.project = target
        task.number = 999
        task.save(update_fields=["project", "number"])
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        types = _event_types(events)
        assert "task.project_changed" in types
        # project / number must not leak into the catch-all task.updated.
        assert "task.updated" not in types
        e = next(e for e in events if e.event_type == "task.project_changed")
        assert e.payload["from_project_id"] == old["project_id"]
        assert e.payload["to_project_id"] == target.id
        assert e.payload["from_slug"] == old["slug"]
        assert e.payload["to_slug"] == task.slug


@pytest.mark.django_db
class TestEmitTaskDiffEvents:
    """End-to-end: emit persists rows in a single ``bulk_create``."""

    def test_single_save_per_event_batch(self, django_assert_max_num_queries):
        """Multiple changed fields → exactly ONE INSERT via bulk_create.

        Within ``emit_task_diff_events`` the only INSERT we control is
        the activity log batch. The other queries are:

        * ``SELECT labels`` (current set, for diff)
        * ``SELECT task with select_related('project', 'assignee')``
          (for the SSE card pre-render — see ADR 0015)
        * ``SELECT labels`` again under the prefetch_related on that
          fresh task instance
        * ``SELECT blocks`` + ``SELECT blocked_by`` — the card now shows
          the blocked / blocking badges, which read both link sets.

        The cap (6) reflects all of those constant queries. What the
        test prevents is a regression to N separate INSERTs for N
        events (which would scale the cap with event count) — the
        prefetch additions are flat, not per-row.
        """
        task = TaskFactory()
        old = snapshot_task(task)
        task.status = Task.STATUS_DONE
        task.priority = Task.URGENT
        task.save()
        with django_assert_max_num_queries(6):
            count = emit_task_diff_events(
                old_state=old,
                task=task,
                actor=task.reporter,
            )
        # Three events: the status + priority edits, plus ``task.end_changed``
        # — moving to done auto-stamps ``end_date`` (the actual finish) in
        # ``Task.save`` → ``_sync_done_dates``.
        assert count == 3
        types = set(
            ActivityLog.objects.filter(target_id=task.id).values_list("event_type", flat=True),
        )
        assert types == {"task.status_changed", "task.priority_changed", "task.end_changed"}

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
