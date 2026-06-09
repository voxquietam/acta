"""Cadence math and the task-materialization engine for recurring tasks.

The date helpers (:func:`occurrence_on_or_after`, :func:`occurrence_after`)
are pure functions of a rule's fields — no DB, no clock — so they are
cheap to unit-test exhaustively. :func:`materialize_due` is the scheduled
side-effecting engine: it walks active rules whose cursor is due and spawns
real tasks, advancing the cursor and honouring the end condition. See ADR
0028.

Weekday convention: ``rule.weekdays`` holds ints matching
:meth:`datetime.date.weekday` (0 = Monday … 6 = Sunday).
"""

import calendar
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

# Frequency string values (mirror ``RecurringTask.Freq``); compared as
# literals so this module needn't import the model just for the math.
_DAILY = "daily"
_WEEKLY = "weekly"
_MONTHLY = "monthly"


def occurrence_on_or_after(rule, d: date) -> date | None:
    """Return the first occurrence date on or after ``d`` for ``rule``.

    Never returns a date before ``rule.start_date`` — the anchor is the
    earliest possible occurrence.

    Args:
        rule: A :class:`~apps.recurring.models.RecurringTask` (only its
            ``freq`` / ``interval`` / ``weekdays`` / ``day_of_month`` /
            ``start_date`` fields are read).
        d: The lower bound (inclusive).

    Returns:
        The matching :class:`datetime.date`, or ``None`` for an unknown
        frequency.
    """
    start = rule.start_date
    target = max(d, start)
    if rule.freq == _DAILY:
        return _daily_on_or_after(target, start, rule.interval)
    if rule.freq == _WEEKLY:
        return _weekly_on_or_after(target, start, rule.interval, list(rule.weekdays or []))
    if rule.freq == _MONTHLY:
        return _monthly_on_or_after(target, start, rule.interval, rule.day_of_month)
    return None


def occurrence_after(rule, d: date) -> date | None:
    """Return the first occurrence strictly after ``d``.

    Thin wrapper over :func:`occurrence_on_or_after` used to advance the
    cursor from the occurrence just spawned.
    """
    return occurrence_on_or_after(rule, d + timedelta(days=1))


def initial_next_occurrence(rule) -> date | None:
    """Return the rule's first occurrence — the seed for ``next_occurrence_date``."""
    return occurrence_on_or_after(rule, rule.start_date)


def _daily_on_or_after(target: date, start: date, interval: int) -> date:
    """First date in ``start, start+interval, …`` that is >= ``target``."""
    if target <= start:
        return start
    delta = (target - start).days
    steps = -(-delta // interval)  # ceil division
    return start + timedelta(days=steps * interval)


def _weekly_on_or_after(target: date, start: date, interval: int, weekdays: list) -> date | None:
    """First weekly occurrence >= ``target``.

    Occurrences fire on each weekday in ``weekdays`` (falling back to
    ``start``'s own weekday when empty), but only in weeks whose index
    from ``start``'s week is a multiple of ``interval``. Scans day by day
    from ``target`` — bounded by the largest possible gap
    (``interval`` weeks), so a handful of iterations at most.
    """
    wanted = sorted(set(weekdays)) or [start.weekday()]
    anchor_monday = start - timedelta(days=start.weekday())
    cursor = target
    for _ in range(interval * 7 + 8):
        week_index = (cursor - anchor_monday).days // 7
        if cursor >= start and cursor.weekday() in wanted and week_index % interval == 0:
            return cursor
        cursor += timedelta(days=1)
    return None


def _monthly_on_or_after(target: date, start: date, interval: int, day_of_month) -> date:
    """First monthly occurrence >= ``target``.

    Occurrences fall on ``day_of_month`` (or ``start``'s day) of every
    ``interval``-th month from ``start``'s month, clamped to the month's
    length (e.g. day 31 → 30 in April, 28/29 in February).
    """
    dom = day_of_month or start.day

    def occ(step: int) -> date:
        total = start.year * 12 + (start.month - 1) + step * interval
        year, month0 = divmod(total, 12)
        month = month0 + 1
        day = min(dom, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    months_between = (target.year - start.year) * 12 + (target.month - start.month)
    step = max(0, -(-months_between // interval))  # ceil division
    while occ(step) < target:
        step += 1
    while step > 0 and occ(step - 1) >= target:
        step -= 1
    return occ(step)


def _past_end(rule, occ: date) -> bool:
    """Whether ``occ`` falls past the rule's end condition (so it must not spawn)."""
    from apps.recurring.models import RecurringTask

    if rule.end_mode == RecurringTask.EndMode.ON_DATE and rule.end_date is not None:
        return occ > rule.end_date
    if rule.end_mode == RecurringTask.EndMode.AFTER_COUNT and rule.max_occurrences is not None:
        return rule.occurrences_created >= rule.max_occurrences
    return False


def materialize_due(today: date | None = None, *, cap_per_rule: int = 50) -> list:
    """Spawn every task whose occurrence is due on or before ``today``.

    Walks active rules with a live cursor and, for each, creates a task per
    due occurrence (lead time included), advancing the cursor and applying
    the end condition. Independent of whether prior occurrences are done —
    the agreed "by schedule" model (ADR 0028). After a downtime gap every
    missed occurrence up to ``today`` is backfilled, bounded by
    ``cap_per_rule`` so a long outage can't spawn thousands at once.

    Args:
        today: The reference date; defaults to the local current date.
        cap_per_rule: Max occurrences to spawn per rule in one run.

    Returns:
        The list of newly created :class:`~apps.tasks.models.Task` rows.
    """
    from apps.recurring.models import RecurringTask

    if today is None:
        today = timezone.localdate()
    rule_ids = list(
        RecurringTask.objects.filter(
            is_active=True,
            next_occurrence_date__isnull=False,
        ).values_list("id", flat=True),
    )
    created = []
    for rule_id in rule_ids:
        created.extend(_materialize_rule(rule_id, today, cap_per_rule))
    return created


def run_once(rule) -> list:
    """Spawn the rule's next occurrence immediately (the "create now" action).

    Ignores the lead-time / due-date gating that :func:`materialize_due`
    applies — it materializes exactly the current cursor occurrence and
    advances. A no-op for a finished rule (no cursor).

    Returns:
        The created tasks (zero or one).
    """
    if rule.next_occurrence_date is None:
        return []
    return _materialize_rule(rule.id, rule.next_occurrence_date, 1)


def _materialize_rule(rule_id: int, today: date, cap_per_rule: int) -> list:
    """Spawn the due occurrences of one rule under a row lock.

    The ``select_for_update`` serializes concurrent runs on the same rule;
    combined with the ``(recurrence, occurrence_date)`` uniqueness on
    :class:`~apps.tasks.models.Task` the engine is safe to run twice.
    """
    from apps.recurring.models import RecurringTask

    out = []
    with transaction.atomic():
        # ``of=["self"]`` locks only the rule row — locking the nullable
        # ``assignee`` / ``created_by`` joins that ``select_related`` adds
        # is rejected by Postgres ("FOR UPDATE cannot be applied to the
        # nullable side of an outer join").
        rule = (
            RecurringTask.objects.select_for_update(of=["self"])
            .select_related("project__workspace", "assignee", "created_by")
            .get(pk=rule_id)
        )
        if not rule.is_active or rule.next_occurrence_date is None:
            return out
        spawned = 0
        while (
            rule.next_occurrence_date is not None
            and rule.next_occurrence_date - timedelta(days=rule.lead_time_days) <= today
            and spawned < cap_per_rule
        ):
            occ = rule.next_occurrence_date
            if _past_end(rule, occ):
                rule.next_occurrence_date = None
                rule.is_active = False
                break
            task = _spawn(rule, occ)
            if task is not None:
                out.append(task)
            rule.occurrences_created += 1
            spawned += 1
            nxt = occurrence_after(rule, occ)
            if nxt is not None and _past_end(rule, nxt):
                nxt = None
            rule.next_occurrence_date = nxt
            if nxt is None:
                rule.is_active = False
        if spawned:
            rule.last_spawned_at = timezone.now()
            rule.save(
                update_fields=[
                    "next_occurrence_date",
                    "occurrences_created",
                    "is_active",
                    "last_spawned_at",
                    "updated_at",
                ],
            )
    return out


def _spawn(rule, occ: date):
    """Create the task for ``rule`` at occurrence ``occ`` (idempotent).

    Returns the new task, or ``None`` when one already exists for this
    ``(rule, occ)`` pair — the uniqueness guard that makes a re-run after a
    crash between spawn and cursor-save a no-op.
    """
    from apps.activity.models import ActivityLog
    from apps.activity.services import log_event
    from apps.notifications.services import notify_task_created
    from apps.tasks.models import Task

    task, was_created = Task.objects.get_or_create(
        recurrence=rule,
        occurrence_date=occ,
        defaults={
            "project": rule.project,
            "title": rule.title,
            "description": rule.description,
            "status": Task.STATUS_TODO,
            "priority": rule.priority,
            "size": rule.size,
            "assignee": rule.assignee,
            "reporter": rule.created_by,
            "due_date": occ,
        },
    )
    if not was_created:
        return None
    label_ids = list(rule.labels.values_list("id", flat=True))
    if label_ids:
        task.labels.set(label_ids)
    log_event(
        workspace=rule.workspace,
        project=rule.project,
        actor=None,
        event_type="task.created",
        target_type=ActivityLog.TARGET_TASK,
        target_id=task.id,
        payload={
            "title": task.title,
            "status": task.status,
            "recurrence_id": rule.id,
            "occurrence_date": occ.isoformat(),
        },
    )
    # Notify the assignee their recurring task has landed — an in-app
    # ASSIGNED notification (which the Telegram fan-out hooks onto). ``actor``
    # is ``None`` (system), so the self-suppression rule never fires.
    notify_task_created(task=task, actor=None)
    return task
