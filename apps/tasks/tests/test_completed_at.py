"""Tests for ``Task.completed_at`` maintenance and the completed-date filter."""

import datetime

from django.http import QueryDict
from django.utils import timezone

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web.filters import apply_task_filters


@pytest.mark.django_db
class TestCompletedAtOnSave:
    def test_set_when_entering_done(self):
        task = TaskFactory(status=Task.STATUS_TODO)
        assert task.completed_at is None
        task.status = Task.STATUS_DONE
        task.save()
        task.refresh_from_db()
        assert task.completed_at is not None

    def test_cleared_when_leaving_done(self):
        task = TaskFactory(status=Task.STATUS_DONE)
        assert task.completed_at is not None
        task.status = Task.STATUS_TODO
        task.save()
        task.refresh_from_db()
        assert task.completed_at is None

    def test_set_when_created_as_done(self):
        task = TaskFactory(status=Task.STATUS_DONE)
        assert task.completed_at is not None

    def test_create_as_done_stamps_same_day_start_and_end(self):
        """Task created directly in done gets start_date == end_date == today.

        Without this, ``start_date`` stayed null — the timeline bar had no
        left edge and cycle/lead-time metrics couldn't measure a span.
        """
        today = timezone.localdate()
        task = TaskFactory(status=Task.STATUS_DONE)
        assert task.start_date == today
        assert task.end_date == today

    def test_create_as_done_preserves_explicit_start_date(self):
        """If the caller passes ``start_date``, the auto-stamp does not overwrite it."""
        explicit = timezone.localdate() - datetime.timedelta(days=3)
        task = TaskFactory(status=Task.STATUS_DONE, start_date=explicit)
        assert task.start_date == explicit
        assert task.end_date == timezone.localdate()

    def test_transition_to_done_does_not_invent_start_date(self):
        """An existing task with no start_date moving into done keeps it null.

        For an old task ``today`` would be a lie about when work began —
        only the create-in-done case auto-fills the start.
        """
        task = TaskFactory(status=Task.STATUS_TODO)
        assert task.start_date is None
        task.status = Task.STATUS_DONE
        task.save()
        task.refresh_from_db()
        assert task.start_date is None
        assert task.end_date == timezone.localdate()

    def test_unrelated_save_preserves_timestamp(self):
        task = TaskFactory(status=Task.STATUS_DONE)
        original = task.completed_at
        task.title = "edited title"
        task.save()
        task.refresh_from_db()
        assert task.completed_at == original

    def test_persisted_even_with_restricted_update_fields(self):
        task = TaskFactory(status=Task.STATUS_TODO)
        task.status = Task.STATUS_DONE
        # The caller restricted the save — completed_at must still persist.
        task.save(update_fields=["status", "updated_at"])
        task.refresh_from_db()
        assert task.completed_at is not None


@pytest.mark.django_db
class TestCompletedAtBulk:
    def test_bulk_to_done_sets_completed_at(self):
        from apps.tasks.bulk import _bulk_apply_scalars

        task = TaskFactory(status=Task.STATUS_TODO)
        _bulk_apply_scalars([task.id], {"status": Task.STATUS_DONE})
        task.refresh_from_db()
        assert task.completed_at is not None

    def test_bulk_leaving_done_clears_completed_at(self):
        from apps.tasks.bulk import _bulk_apply_scalars

        task = TaskFactory(status=Task.STATUS_DONE)
        assert task.completed_at is not None
        _bulk_apply_scalars([task.id], {"status": Task.STATUS_TODO})
        task.refresh_from_db()
        assert task.completed_at is None


@pytest.mark.django_db
class TestEndDateOnDone:
    """``end_date`` (the actual finish) is stamped on the move into done."""

    def test_set_when_entering_done(self):
        task = TaskFactory(status=Task.STATUS_TODO)
        assert task.end_date is None
        task.status = Task.STATUS_DONE
        task.save()
        task.refresh_from_db()
        assert task.end_date == timezone.localdate()

    def test_overwrites_planned_end(self):
        task = TaskFactory(status=Task.STATUS_TODO, end_date=datetime.date(2020, 1, 1))
        task.status = Task.STATUS_DONE
        task.save()
        task.refresh_from_db()
        # The actual finish replaces the plan.
        assert task.end_date == timezone.localdate()

    def test_kept_when_leaving_done(self):
        task = TaskFactory(status=Task.STATUS_DONE)
        stamped = task.end_date
        assert stamped == timezone.localdate()
        task.status = Task.STATUS_TODO
        task.save()
        task.refresh_from_db()
        # Unlike completed_at, the finish date stays on the record.
        assert task.end_date == stamped

    def test_not_rebumped_on_later_save_while_done(self):
        task = TaskFactory(status=Task.STATUS_DONE)
        Task.objects.filter(id=task.id).update(end_date=datetime.date(2026, 1, 2))
        task.refresh_from_db()
        task.title = "edited"
        task.save()
        task.refresh_from_db()
        # Still done, but the finish date is not re-stamped to today.
        assert task.end_date == datetime.date(2026, 1, 2)

    def test_set_when_created_as_done(self):
        task = TaskFactory(status=Task.STATUS_DONE)
        assert task.end_date == timezone.localdate()

    def test_bulk_to_done_sets_end_date(self):
        from apps.tasks.bulk import _bulk_apply_scalars

        task = TaskFactory(status=Task.STATUS_TODO)
        _bulk_apply_scalars([task.id], {"status": Task.STATUS_DONE})
        task.refresh_from_db()
        assert task.end_date == timezone.localdate()

    def test_bulk_explicit_end_date_wins_over_done_stamp(self):
        from apps.tasks.bulk import _bulk_apply_scalars

        task = TaskFactory(status=Task.STATUS_TODO)
        _bulk_apply_scalars([task.id], {"status": Task.STATUS_DONE, "end_date": "2026-09-09"})
        task.refresh_from_db()
        assert task.end_date == datetime.date(2026, 9, 9)


@pytest.mark.django_db
class TestStartEndDateFilter:
    """The date-range filter accepts the new ``start`` / ``end`` fields."""

    def _ids(self, user, querystring):
        params = QueryDict(querystring)
        return set(apply_task_filters(Task.objects.all(), params, request_user=user).values_list("id", flat=True))

    def test_start_field(self):
        user = UserFactory()
        task = TaskFactory(status=Task.STATUS_TODO, start_date=datetime.date(2026, 6, 15))
        no_start = TaskFactory(status=Task.STATUS_TODO)
        ids = self._ids(user, "date_field=start&date_after=2026-06-01&date_before=2026-06-30")
        assert task.id in ids
        assert no_start.id not in ids

    def test_end_field(self):
        user = UserFactory()
        task = TaskFactory(status=Task.STATUS_TODO, end_date=datetime.date(2026, 7, 10))
        no_end = TaskFactory(status=Task.STATUS_TODO)
        ids = self._ids(user, "date_field=end&date_after=2026-07-01&date_before=2026-07-31")
        assert task.id in ids
        assert no_end.id not in ids


@pytest.mark.django_db
class TestDateRangeFilter:
    def _ids(self, user, querystring):
        params = QueryDict(querystring)
        return set(apply_task_filters(Task.objects.all(), params, request_user=user).values_list("id", flat=True))

    def _done_on(self, year, month, day):
        task = TaskFactory(status=Task.STATUS_DONE)
        moment = timezone.make_aware(datetime.datetime(year, month, day, 12, 0))
        Task.objects.filter(id=task.id).update(completed_at=moment)
        return task

    def test_completed_after_bound(self):
        user = UserFactory()
        old = self._done_on(2026, 1, 1)
        recent = self._done_on(2026, 5, 20)
        ids = self._ids(user, "date_field=completed&date_after=2026-05-01")
        assert recent.id in ids
        assert old.id not in ids

    def test_completed_range_excludes_non_done(self):
        user = UserFactory()
        done = self._done_on(2026, 5, 20)
        open_task = TaskFactory(status=Task.STATUS_TODO)
        ids = self._ids(user, "date_field=completed&date_after=2026-05-01&date_before=2026-05-31")
        assert done.id in ids
        assert open_task.id not in ids

    def test_completed_is_default_field(self):
        user = UserFactory()
        done = self._done_on(2026, 5, 20)
        open_task = TaskFactory(status=Task.STATUS_TODO)
        # No date_field given → defaults to completed.
        ids = self._ids(user, "date_after=2026-05-01&date_before=2026-05-31")
        assert done.id in ids
        assert open_task.id not in ids

    def test_created_field_explicit(self):
        user = UserFactory()
        task = TaskFactory(status=Task.STATUS_TODO)
        Task.objects.filter(id=task.id).update(created_at=timezone.make_aware(datetime.datetime(2026, 3, 10, 9, 0)))
        assert task.id in self._ids(user, "date_field=created&date_after=2026-03-01&date_before=2026-03-31")
        assert task.id not in self._ids(user, "date_field=created&date_after=2026-04-01")

    def test_due_field(self):
        user = UserFactory()
        task = TaskFactory(status=Task.STATUS_TODO, due_date=datetime.date(2026, 6, 15))
        no_due = TaskFactory(status=Task.STATUS_TODO)
        ids = self._ids(user, "date_field=due&date_after=2026-06-01&date_before=2026-06-30")
        assert task.id in ids
        assert no_due.id not in ids  # null due drops out when a bound is set

    def test_unknown_field_ignored(self):
        user = UserFactory()
        task = TaskFactory(status=Task.STATUS_TODO)
        # Bogus date_field → no date filtering applied.
        assert task.id in self._ids(user, "date_field=bogus&date_after=2000-01-01")

    def test_invalid_date_ignored(self):
        user = UserFactory()
        done = self._done_on(2026, 5, 20)
        assert done.id in self._ids(user, "date_field=completed&date_after=not-a-date")
