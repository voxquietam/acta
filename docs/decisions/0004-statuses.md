# ADR 0004: Task Statuses

**Status:** accepted (amended 2026-05-22 — added terminal `cancelled` status)
**Date:** 2026-05-15

## Context

Tasks need a status field that drives the kanban view. The team has settled on five buckets: `planned`, `to-do`, `in-progress`, `in-review`, `done`. The open question is whether statuses should be a global fixed enum or customizable per project (per-project `Status` model with order and color).

Per-project statuses are a real feature in mature trackers, but they add a model, a join, a UI for management, and migration concerns when adding a task to a project that has no statuses configured.

## Decision

- **MVP:** statuses are a **fixed set of five** values: `planned`, `to-do`, `in-progress`, `in-review`, `done`.
- **Storage:** `Task.status` is a `CharField(max_length=…)` — *not* a `choices=` constraint enforced by the DB. Validation lives in the serializer/form layer.
- **Why CharField instead of Django `choices` or a Postgres enum:** keeps the door open to convert `status` into a FK pointing at a per-project `Status` model later, without a destructive schema migration.

## Why

- **Five is enough for KSU24's workflow** and matches what the team is used to. No need to invest in customization UI right now.
- **`CharField` is the cheapest forward-compatible shape.** Postgres enums are painful to evolve; `choices` validation locks the values into migrations. A plain `CharField` with serializer-side validation lets us swap the column to a FK with a single migration when (if) per-project statuses become real.

## Consequences

- The kanban view has five columns, hard-coded for now.
- Status values are validated in DRF serializers, *not* by `models.TextChoices` on the field. Choices can still be exposed as a constant in code for serializer validation, admin display, and API docs — just not as a `choices=` argument on the field.
- Indexing: `Task.status` should still be indexed (composite with `project_id`) to keep kanban queries fast.
- Migration path: when we move to per-project statuses, the migration is (a) create `Status` table seeded with the five values per project, (b) add `status_id` FK, (c) backfill from the string column, (d) drop the string column. No data loss.

## Amendment 2026-05-22 — `cancelled` terminal status

Added a sixth status, `cancelled`, for tasks that were real (time may have
been spent) but whose need disappeared — distinct from `done` (completed)
and from archive (`archived_at`, hide/declutter). Linear's "Cancelled"
status is the precedent: a terminal, non-completion state.

- **Storage is unchanged** — still a plain `CharField` with no `choices=`
  at the DB level, so this is a code-only change (`Task.STATUS_VALUES` /
  `STATUS_LABELS` gained one entry; no migration).
- **Cancel / un-cancel reuse the status machinery.** Cancelling is
  `set_task_status('cancelled')`; re-opening is picking any other status
  from the same dropdown. No new endpoint, no new activity event — it
  rides the existing `task.status_changed` event.
- **Not a kanban column.** `Task.KANBAN_STATUS_VALUES` (the five workflow
  states) drives the board; cancelling drops a card off it. The board
  stays focused on active work.
- **Hidden by default in lists/tables**, more aggressively than `done`:
  `_filter_status` excludes `cancelled` unless an explicit
  `?status=cancelled` (the sidebar chip) opts it back in.
- **Excluded from throughput.** Cancelled tasks are not counted as `done`
  in velocity / overdue / project progress; the overview surfaces a
  separate `cancelled` count.
