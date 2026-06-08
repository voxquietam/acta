import datetime
from types import SimpleNamespace

import pytest

from apps.recurring.models import RecurringTask
from apps.recurring.services import materialize_due, occurrence_after, occurrence_on_or_after
from apps.recurring.tests.factories import RecurringTaskFactory
from apps.tasks.models import Task

D = datetime.date


def _rule(freq, *, interval=1, weekdays=None, day_of_month=None, start=D(2026, 1, 1)):
    """Lightweight duck-typed rule for the pure cadence functions (no DB)."""
    return SimpleNamespace(
        freq=freq,
        interval=interval,
        weekdays=weekdays or [],
        day_of_month=day_of_month,
        start_date=start,
    )


# ---------------------------------------------------------------------
# Pure cadence math
# ---------------------------------------------------------------------


def test_daily_every_day():
    rule = _rule("daily", start=D(2026, 1, 1))
    assert occurrence_on_or_after(rule, D(2026, 1, 1)) == D(2026, 1, 1)
    assert occurrence_after(rule, D(2026, 1, 1)) == D(2026, 1, 2)


def test_daily_every_three_days():
    rule = _rule("daily", interval=3, start=D(2026, 1, 1))
    assert occurrence_after(rule, D(2026, 1, 1)) == D(2026, 1, 4)
    # 01-02 lands between occurrences → next is 01-04.
    assert occurrence_on_or_after(rule, D(2026, 1, 2)) == D(2026, 1, 4)


def test_daily_never_before_start():
    rule = _rule("daily", start=D(2026, 1, 10))
    assert occurrence_on_or_after(rule, D(2026, 1, 1)) == D(2026, 1, 10)


def test_weekly_default_weekday_follows_start():
    # 2026-01-01 is a Thursday; with no weekdays the cadence is weekly Thu.
    rule = _rule("weekly", start=D(2026, 1, 1))
    assert occurrence_on_or_after(rule, D(2026, 1, 1)) == D(2026, 1, 1)
    assert occurrence_after(rule, D(2026, 1, 1)) == D(2026, 1, 8)


def test_weekly_specific_weekdays():
    # Mon + Wed, weekly. Start Thursday 2026-01-01 → first is Mon 2026-01-05.
    rule = _rule("weekly", weekdays=[0, 2], start=D(2026, 1, 1))
    assert occurrence_on_or_after(rule, D(2026, 1, 1)) == D(2026, 1, 5)
    assert occurrence_after(rule, D(2026, 1, 5)) == D(2026, 1, 7)
    assert occurrence_after(rule, D(2026, 1, 7)) == D(2026, 1, 12)


def test_weekly_every_two_weeks():
    # Every 2 weeks on Monday from Mon 2026-01-05.
    rule = _rule("weekly", interval=2, weekdays=[0], start=D(2026, 1, 5))
    assert occurrence_after(rule, D(2026, 1, 5)) == D(2026, 1, 19)
    assert occurrence_on_or_after(rule, D(2026, 1, 6)) == D(2026, 1, 19)


def test_monthly_day_of_month():
    rule = _rule("monthly", day_of_month=15, start=D(2026, 1, 1))
    assert occurrence_on_or_after(rule, D(2026, 1, 1)) == D(2026, 1, 15)
    assert occurrence_after(rule, D(2026, 1, 15)) == D(2026, 2, 15)


def test_monthly_clamps_to_month_length():
    rule = _rule("monthly", day_of_month=31, start=D(2026, 1, 31))
    # February 2026 has 28 days → clamps to the 28th.
    assert occurrence_after(rule, D(2026, 1, 31)) == D(2026, 2, 28)
    assert occurrence_after(rule, D(2026, 2, 28)) == D(2026, 3, 31)


def test_monthly_every_two_months():
    rule = _rule("monthly", interval=2, day_of_month=15, start=D(2026, 1, 15))
    assert occurrence_after(rule, D(2026, 1, 15)) == D(2026, 3, 15)


# ---------------------------------------------------------------------
# Cursor seeding + materialization engine
# ---------------------------------------------------------------------


@pytest.mark.django_db
def test_cursor_seeded_on_save():
    rule = RecurringTaskFactory(freq="weekly", weekdays=[0], start_date=D(2026, 1, 1))
    # First Monday on/after 2026-01-01 is 2026-01-05.
    assert rule.next_occurrence_date == D(2026, 1, 5)


@pytest.mark.django_db
def test_materialize_creates_task_when_due():
    rule = RecurringTaskFactory(freq="daily", start_date=D(2026, 1, 1))
    created = materialize_due(D(2026, 1, 1))
    assert len(created) == 1
    task = created[0]
    assert task.recurrence_id == rule.id
    assert task.occurrence_date == D(2026, 1, 1)
    assert task.status == Task.STATUS_TODO
    assert task.due_date == D(2026, 1, 1)
    rule.refresh_from_db()
    assert rule.next_occurrence_date == D(2026, 1, 2)
    assert rule.occurrences_created == 1


@pytest.mark.django_db
def test_materialize_is_idempotent_same_day():
    RecurringTaskFactory(freq="daily", start_date=D(2026, 1, 1))
    materialize_due(D(2026, 1, 1))
    materialize_due(D(2026, 1, 1))
    assert Task.objects.count() == 1


@pytest.mark.django_db
def test_materialize_backfills_missed_occurrences():
    # Daily rule anchored a week ago; one run fills every missed day.
    RecurringTaskFactory(freq="daily", start_date=D(2026, 1, 1))
    created = materialize_due(D(2026, 1, 7))
    assert len(created) == 7
    assert {t.occurrence_date for t in created} == {D(2026, 1, d) for d in range(1, 8)}


@pytest.mark.django_db
def test_materialize_respects_cap_per_rule():
    RecurringTaskFactory(freq="daily", start_date=D(2026, 1, 1))
    created = materialize_due(D(2026, 1, 31), cap_per_rule=5)
    assert len(created) == 5


@pytest.mark.django_db
def test_pile_up_creates_regardless_of_completion():
    # "By schedule" model: a new task appears even though the prior one is
    # still open (the agreed pile-up policy).
    RecurringTaskFactory(freq="daily", start_date=D(2026, 1, 1))
    materialize_due(D(2026, 1, 1))
    materialize_due(D(2026, 1, 2))
    materialize_due(D(2026, 1, 3))
    assert Task.objects.filter(status=Task.STATUS_TODO).count() == 3


@pytest.mark.django_db
def test_end_after_count_stops():
    rule = RecurringTaskFactory(
        freq="daily",
        start_date=D(2026, 1, 1),
        end_mode=RecurringTask.EndMode.AFTER_COUNT,
        max_occurrences=3,
    )
    created = materialize_due(D(2026, 1, 31))
    assert len(created) == 3
    rule.refresh_from_db()
    assert rule.next_occurrence_date is None
    assert rule.is_active is False


@pytest.mark.django_db
def test_end_on_date_stops():
    rule = RecurringTaskFactory(
        freq="daily",
        start_date=D(2026, 1, 1),
        end_mode=RecurringTask.EndMode.ON_DATE,
        end_date=D(2026, 1, 3),
    )
    created = materialize_due(D(2026, 1, 31))
    assert {t.occurrence_date for t in created} == {D(2026, 1, 1), D(2026, 1, 2), D(2026, 1, 3)}
    rule.refresh_from_db()
    assert rule.next_occurrence_date is None


@pytest.mark.django_db
def test_lead_time_creates_early():
    # Occurrence 2026-01-10, lead 3 days → due to spawn from 2026-01-07.
    RecurringTaskFactory(freq="daily", start_date=D(2026, 1, 10), lead_time_days=3)
    assert materialize_due(D(2026, 1, 6)) == []
    created = materialize_due(D(2026, 1, 7))
    assert len(created) == 1
    assert created[0].occurrence_date == D(2026, 1, 10)


@pytest.mark.django_db
def test_inactive_rule_skipped():
    RecurringTaskFactory(freq="daily", start_date=D(2026, 1, 1), is_active=False)
    assert materialize_due(D(2026, 1, 5)) == []
    assert Task.objects.count() == 0


@pytest.mark.django_db
def test_future_rule_not_yet_due():
    RecurringTaskFactory(freq="daily", start_date=D(2026, 6, 1))
    assert materialize_due(D(2026, 1, 1)) == []


@pytest.mark.django_db
def test_labels_copied_to_instance():
    from apps.labels.tests.factories import LabelFactory

    rule = RecurringTaskFactory(freq="daily", start_date=D(2026, 1, 1))
    label = LabelFactory(workspace=rule.workspace)
    rule.labels.add(label)
    created = materialize_due(D(2026, 1, 1))
    assert list(created[0].labels.all()) == [label]
