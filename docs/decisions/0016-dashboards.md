# ADR 0016: Dashboards and Statistics

**Status:** accepted
**Date:** 2026-05-15

## Context

The MVP includes dashboards: workspace-level overview, project-level overview, and a personal "my work" page. The user explicitly wants to see correlations and stats, not just task lists. This ADR scopes which dashboards ship in MVP, what metrics they show, and how the data is computed and delivered.

The constraint: keep this simple. Dashboards are easy to over-engineer with pivot tables, custom filters, and saved views. MVP gets the high-value views and nothing else.

## Decisions

### Pages and metrics in MVP

#### `/me/` — "My Work"

Personal dashboard for the logged-in user across all workspaces they belong to.

- **Counts cards:** open tasks assigned to me, due this week, overdue, in review.
- **Tasks list:** active tasks (status ≠ done) ordered by due date, then priority.
- **Recent activity** (last 7 days): events where I'm actor or where I'm assignee on the affected task.

No chart on this page in MVP — the counts and lists are the dashboard.

#### `/projects/{slug_prefix}/` — Project Overview

Project page already exists for kanban/table view; the overview tab adds:

- **Tasks by status** (bar chart, Chart.js).
- **Throughput last 30 days** — tasks moved to `done` per day (line chart).
- **Workload by assignee** — open task counts per assignee, weighted by `size` story points (horizontal bar chart).
- **Latest project update** — the most recent `ProjectUpdate` with its health badge, body preview, and author. Click to expand.
- **Overdue count** + list of overdue tasks (max 5, "view all" link to filtered table).

#### `/` — Workspace Dashboard

Landing page after login. Workspace-level rollup:

- **Active projects:** card per project with name, current health (from latest `ProjectUpdate`), open task count, recent activity timestamp.
- **Workspace throughput last 30 days** — tasks moved to done across all projects (line chart).
- **Top assignees by workload** — story-points weighted across all open tasks (horizontal bar).
- **Activity feed** — most recent 20 events workspace-wide.

### What's NOT in MVP

- **Custom dashboards** / saved views.
- **Time tracking** / time-in-status metrics (lead time, cycle time).
- **Burndown / burnup charts** (require cycles or sprints, which are out of MVP).
- **CSV/PDF export.**
- **Per-label or per-priority breakdowns** beyond the named views above.
- **Date range pickers** on charts. Default windows are fixed (30 days, 7 days, etc.). Configurable later.
- **Cross-project filters** ("show me all tasks across projects where assignee = X and label = Y"). Search/filter on the global tasks list covers this need at the data level; dedicated cross-project dashboards are deferred.

### Data layer

- All metrics are computed **live from the ORM** on each request. No precomputed aggregates, no materialized views, no nightly batch.
- Postgres handles workspace-scale aggregation trivially at MVP volumes (10k tasks tops).
- ORM queries use `annotate` + `aggregate` with appropriate `Count`, `Sum`, `Filter` expressions. Index on `(project_id, status)` and `(project_id, updated_at)` keeps the throughput chart fast.

### Caching

- **No caching in MVP.** Live query on each page load.
- If a dashboard page exceeds ~300 ms server time consistently, add 60-second per-user cache via Django's cache framework — single-line change. Don't pre-optimize.

### Rendering

- Dashboards are normal server-rendered Django pages. The template includes a `<canvas>` for each chart plus an inline `<script>` block that initializes Chart.js with the data injected from the view context as JSON.
- The data is serialized server-side into a small `<script type="application/json" id="chart-data-X">...</script>` block and read by the chart-init script. Keeps templates clean and avoids inline-JSON XSS issues with `bleach` involvement.
- Charts do not auto-refresh in MVP — page reload to see fresh numbers. Real-time updates are scoped to kanban and notifications, not stats charts (see [0015](0015-real-time.md)). Adding live charts later is straightforward but adds complexity not justified for MVP.
- All charts use the same Chart.js theme (dark by default, matching the app theme; light variant computed from CSS variables).

### Permissions

- `/me/` shows only the logged-in user's data.
- `/projects/{slug_prefix}/` requires workspace membership (already covered by general permissions in [0010](0010-permissions.md)).
- `/` workspace dashboard shows data from the **default workspace** the user is currently viewing. Multi-workspace switching is workspace-selector UI; out of scope for this ADR.

## Why

- **Live ORM queries** are the lowest-complexity option that works at MVP scale. Precomputation infrastructure (Celery, Redis, scheduled jobs) is exactly the kind of "second-system" trap we want to avoid.
- **Chart.js** is the cheapest mature charting library — declarative, dark/light theming via options, no React/Vue binding required. ApexCharts is a close second; defer the decision and start with Chart.js.
- **No date pickers in MVP** keeps the surface small. Fixed 7d/30d windows cover the "is the project moving" question. Custom ranges are a feature add, not a foundation.
- **Page-reload-for-fresh-stats** is acceptable because the volatility is low: throughput-per-day doesn't change moment-to-moment. Kanban does, and kanban is on the real-time channel.
- **No CSV/PDF export** — KSU24 doesn't need executive-style reporting. Adding export later is a per-chart serializer.

## Consequences

- Dashboards on first load can be slow if the queries aren't tuned. Implementation needs to verify the throughput chart and workload aggregation hit indexes. Add EXPLAIN ANALYZE checks during implementation.
- Without caching, repeated page reloads hit Postgres on every load. Acceptable at 10 users; revisit at 50.
- A user closing their laptop sees stale numbers when they re-open the page until they reload. Acceptable trade-off — staleness measured in tens of seconds.
- Chart.js bundle is loaded only on dashboard pages (kanban/table/task-detail don't need it). Lazy-load via inline `<script src=...>` only where needed.

## Open Questions

- Whether to compute cycle time / lead time even without cycles. Approximation: `task.completed_at - task.created_at`. Useful, but the `completed_at` field doesn't exist yet — would add to Task model. Defer to post-MVP unless the team specifically asks.
- Whether to surface a "blocked" indicator (tasks not updated in N days). Cheap to compute; adds noise. Defer.
- Whether the workspace dashboard activity feed and the standalone `/activity/` page are redundant. Lean toward keeping both: dashboard shows ~20 items as a glance; `/activity/` is the full searchable feed.
