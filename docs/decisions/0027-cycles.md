# ADR 0027: Cycles — workspace-level time-boxed iterations

**Status:** accepted
**Date:** 2026-05-23

## Context

On top of the Scrumban mechanics (ADR 0026) the team wanted **cycles**
(Linear's term; Scrum's "sprints"): time-boxed iterations to plan "what
gets done in the next two weeks" and review velocity. Open questions:

1. **Scope** — per-project sprints, or one cadence the whole workspace
   shares?
2. **How do cycles advance** — a background scheduler minting rows, or
   something lazier?
3. **When does a task join a cycle** — manual only, or coupled to status?
4. **What is "ready" work** — does the board need a replenishment buffer?

## Decision

**Workspace-level cycles (Linear "Cycles"), not per-project sprints.**
A small team runs one cadence across every project. The cadence config
lives on `Workspace.cycle_settings` (JSON: `enabled`, `length_weeks`,
`start_date` anchor, `auto_rollover`), edited in workspace settings next
to WIP limits.

**Deterministic, lazily-materialized windows.** A cycle's bounds are a
pure function of `(anchor, length_weeks, index)`; `Cycle.number ==
index + 1`. `apps/cycles/services.ensure_cycles()` is idempotent and
called from any view that needs cycles — it materializes the current +
next window and reconciles every cycle's status (`planning` → `active`
→ `completed`) from `today`. **No background job rolls cycles** — they
roll forward on page load. Windows that elapsed before the first call
are never back-filled.

**`Task.cycle` FK (SET_NULL); null = backlog.** Cycle membership is
coupled to status by `apply_cycle_policy`:

- `planned` and `ready` are the **backlog zone** — no cycle. Manual /
  bulk cycle assignment to them is refused.
- Entering committed work (`to-do` / `in-progress` / `in-review`) with no
  cycle pulls the task into the **active** cycle (the commit point is
  `to-do`). Done / cancelled keep their cycle (velocity history).
- `auto_rollover` (opt-in): when a cycle completes, its unfinished tasks
  follow the team into the new active cycle.

**A `ready` replenishment buffer** (ADR 0004 amendment): a first-class
status / kanban column between planned and to-do for groomed, pullable
work — still backlog for cycle purposes.

**Metrics reuse the activity-log replay (ADR 0026).** Burndown (open
tasks/day vs. ideal) and velocity (done per cycle) are computed on the
fly from `task.status_changed` events — no snapshot table.

**Notifications via a daily management command, not a request hook.**
`notify_cycle_events` (cron) fans out inbox notifications on cycle start
and approaching end, idempotent via `Cycle.start/end_notified_at`. The
scheduler is slated to move into an admin-manageable form later (the
command stays; only the trigger changes).

## Consequences

- `ensure_cycles` runs on many requests; it's a few queries and idempotent.
  Status changes resolve the active cycle with a cheap `current_cycle`
  lookup first, only falling back to a full materialize when none exists.
- Per-cycle reads on the `/cycles/` dashboard (summaries, velocity) are
  batched into single grouped queries — query count is flat in cycle
  count (guarded by `test_dashboard_query_count_does_not_grow_with_cycles`).
- Changing the cadence reshapes only cycles not yet materialized; past
  cycles keep their stored bounds.
- The board has six status columns now (planned · ready · to-do ·
  in-progress · in-review · done).
- A future per-cycle detail page and cycle scope-change handling in the
  burndown (scope is currently assumed fixed) are left open.
