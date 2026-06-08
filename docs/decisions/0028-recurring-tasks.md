# ADR 0028: Recurring tasks

**Status:** accepted
**Date:** 2026-06-08

## Context

The team wanted tasks that recur on a cadence — "do this every week / every
two weeks / on these specific weekdays" — plus one place to see and manage
every recurring definition. Open questions:

1. **What spawns the next task** — a fresh task each period on a schedule,
   or a new one only after the current is completed (Todoist-style)?
2. **Pile-up** — if the previous occurrence isn't done yet, still create
   the next, or hold off?
3. **How expressive is the rule** — simple intervals, specific weekdays,
   or full iCal RRULE?
4. **What status / where do generated tasks land?**

## Decision

**A rule entity, not a self-cloning task.** `apps/recurring/RecurringTask`
is the template: it holds the blueprint copied onto each generated task
(title, description, assignee, priority, size, labels) plus the recurrence
rule and a cursor. Each generated `Task` links back via `Task.recurrence`
(`SET_NULL`, so a task outlives the rule) and carries `Task.occurrence_date`.
This makes "see all recurring tasks" a first-class query and keeps generated
tasks normal in every other respect.

**By schedule, independent of completion.** A fresh task is created every
period regardless of whether the previous occurrence was finished — the
literal "needs doing every week" model. Pile-up is allowed: an unfinished
prior occurrence does **not** suppress the next.

**Structured rule, no new dependency.** `freq` (daily / weekly / monthly)
+ `interval` ("every N") + `weekdays` (JSON list of `date.weekday()` ints,
weekly) + `day_of_month` (monthly, clamped to month length). This covers
"every two weeks on Mon, Wed" and "the 15th of every month" without pulling
in `python-dateutil` / full RRULE. The cadence math
(`services.occurrence_on_or_after` / `occurrence_after`) is pure and
exhaustively unit-tested.

**Daily materializer, mirroring the cycles pattern.**
`services.materialize_due(today)` walks active rules whose
`next_occurrence_date - lead_time_days <= today`, spawns a task per due
occurrence, advances the cursor, and applies the end condition. Wired as a
django-q daily schedule (`apps.common.scheduled.materialize_recurring_tasks`
→ `manage.py materialize_recurring_tasks`, seeded by `setup_scheduled_jobs`,
~05:00). After downtime every missed occurrence up to `today` is backfilled,
bounded by `cap_per_rule=50` so a long outage can't spawn thousands.

**Generated tasks land in `to-do`**, `due_date = occurrence_date`,
reporter = the rule's creator, assignee/labels/priority/size copied from the
blueprint. The activity log gets a `task.created` event with `actor=None`
(system).

**Idempotency.** A `UniqueConstraint(recurrence, occurrence_date)` (only
where `recurrence` is set) plus `select_for_update(of=["self"])` per rule
make a double run — or a re-run after a crash between spawn and cursor-save
— a no-op.

### Lifecycle / config defaults

- **Lead time** `lead_time_days` (default 0): create the task N days early.
- **End condition** `end_mode`: `never` (default) / `on_date` (`end_date`) /
  `after_count` (`max_occurrences`).
- **Pause** via `is_active`; a finished rule has `next_occurrence_date = None`.
- The cursor is seeded on first save (`RecurringTask.save`) to the first
  occurrence on/after `start_date`.

## Consequences

- New app `apps/recurring/` (model + services + admin + management command).
  Phase 1 is headless — create/inspect rules via Django admin and run the
  materializer; the `/recurring/` page and the task-detail "Make recurring"
  affordance are Phases 2–3.
- `Task` gains two nullable columns and one partial unique constraint
  (migration `tasks 0013`).
- **Deferred:** assignment notifications for spawned tasks (the engine runs
  outside request context, so the live-insert HTML extras / `notify_task_*`
  are not emitted — peers see new recurring tasks on next navigation, as with
  other non-kanban live inserts); cycle assignment on spawn; "nth weekday of
  the month" rules.
