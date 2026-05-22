# ADR 0026: Scrumban support — WIP limits, aging, and flow metrics

**Status:** accepted
**Date:** 2026-05-22

## Context

Acta started as a kanban board. To support a **Scrumban** workflow
(kanban pull-flow + a cadence of reflection) the board needs the
pull-flow mechanics and the measurements that make flow visible:

- WIP limits per column (the headline scrumban mechanic — without them
  the team pushes work and bottlenecks form).
- Aging WIP signals (how long a card has sat in its column).
- Flow metrics: cycle time, lead time, throughput.
- Cumulative Flow Diagram (the canonical bottleneck-spotting chart).
- A bottleneck view: average time-in-status, current WIP, reopen rate.

The open architectural question was **where flow metrics are computed
and stored** — live from the activity log, or materialised into a
snapshot table fed by a daily job.

## Decision

### Scope (this iteration)

- **WIP limits** are per-project, per-status, stored as a JSON map on
  `Project.wip_limits` (`{status_key: max_cards}`; absent/0 = no limit).
  Set inline from the kanban column header. The header renders the
  `N/limit` fraction + a capacity bar and an over/at-limit warning per
  `comp-kanban-column-head` v3.
- **Aging WIP** is derived from the last `task.status_changed` activity
  row (annotated as `status_since` on the board queryset, one
  subquery — not an N+1); cards grow a left-edge bar (amber ≥3d,
  rose ≥7d) in active statuses.
- **Flow metrics** (cycle/lead/throughput, CFD, bottlenecks) live in
  `apps/tasks/metrics.py` and render on a per-project **insights page**
  (`/projects/<slug>/insights/`) with Chart.js.

### Computation: live replay of the activity log, no snapshot table

Every status change is already an append-only `ActivityLog`
(`task.status_changed`, `{from,to}` payload, `created_at`). All metrics
are a **replay of that log**, computed on demand for a trailing window:

- **Cycle time** = first `→ in-progress` to last `→ done`.
- **Lead time** = `created_at` to last `→ done`.
- **Throughput** = `→ done` transitions per ISO week.
- **CFD** = per day, each task's status reconstructed from its change
  history, counted per status.
- **Bottlenecks** = average time spent in each status (closed
  segments), current WIP per status, reopen rate (`from=done`).

We explicitly **reject** a `DailyStatusSnapshot` table + daily cron:

- A snapshot populated going forward starts **empty** — no history for
  weeks. Replay gives the full history the moment the feature ships.
- The insights page is not a hot path; bounded by a trailing window and
  one project's tasks, replay is `O(tasks × days)` — trivial at Acta's
  scale (small self-hosted teams).
- No new table, migration, cron job, or backfill to maintain.

If a workspace ever outgrows live replay, a snapshot/materialised table
can be added behind the same `apps.tasks.metrics` API without touching
callers. Until then, YAGNI.

## Consequences

- The activity log is now load-bearing for analytics, not just audit —
  `task.status_changed` payload shape (`from`/`to`) must stay stable.
- Cycle/lead/throughput windows track *recent* flow (trailing N weeks),
  not all-time, so medians reflect current behaviour.
- Cards reopened out of `done` drop back out of the completed sample
  (they aren't done any more) and count toward the reopen rate.
- Chart.js is loaded via CDN on the insights page only (same no-build
  approach as sortable.js on the board; see ADR 0014).
- Relates to ADR 0016 (dashboards) and ADR 0011 (activity log as the
  single source of truth).
