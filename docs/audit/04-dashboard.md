# 04 — Dashboard + Project Insights

> **Wave 1 / Chunk B4** — `/` (workspace dashboard) and
> `/projects/<slug>/insights/` (per-project flow metrics).
> Date: 2026-05-29. Read-only. **No code changed.**
> Sources: `DashboardView` (`views.py:1479-1522`), `build_dashboard_context`
> + helpers (`apps/web/dashboard.py:163-674`), `project_insights`
> (`views.py:4373-4433`), `inline_static` templatetag
> (`web_extras.py:22-99`), `_dashboard_inner.html` (536 LOC),
> `dashboard.css` (310 LOC), `templates/web/projects/insights.html`
> (197 LOC). Historical refs: dashboard cold-load profiled
> 2026-05-17 at **12 ms / ~30 KB** ([[project-todo-all-tasks-lazy-panels]]),
> FOUC fix shipped (`b598340`), matrix/heatmap idempotency fix
> shipped (`a3c1060`).

---

## 1. Quick verdict

**Dashboard performance is fine today** (12 ms cold load per the
2026-05-17 profile) but **runs on ~32 queries** — most of them
`SELECT COUNT(*) FROM tasks WHERE …`. That's not slow at the
current 192-task scale (each count is sub-millisecond on indexed
columns), but it's a *lot* of round-trips that can be cut to
**~8 queries** by collapsing KPI / alert / hygiene blocks into
single `aggregate(Count("id", filter=Q(…)))` calls. Same shape, same
output, fewer DB round-trips, cleaner code.

**`inline_static` + `dashboard.css` work as designed.** FOUC is
solved on dashboard inner-fragment swaps. The trade-off (handwritten
CSS file outside the Tailwind/esbuild pipeline) is documented and
intentional.

**`_build_people` is the only function that scales linearly with
total workspace task count** — it materialises every task row
(8 fields) for in-memory Python aggregation. Fine at <5 000 tasks,
flagged for review past 10 000.

**Project Insights is read-only display of computed flow metrics**.
The compute side (`compute_flow_metrics`, `compute_cfd`,
`compute_bottlenecks`) lives in `apps/tasks/metrics.py` and is
deferred to **C1 (tasks)** for query auditing.

---

## 2. Dashboard query profile

Counted statically from `build_dashboard_context` (`dashboard.py:163-455`)
+ helpers. **`?` = exact count requires runtime measurement.**

| # | Section | Queries | Collapsible? |
|---|---|---:|---|
| 2.1 | `WorkspaceMember.objects.filter(user=user).exists()` (DashboardView) | 1 | — |
| 2.2 | KPI tiles (created, created_prev, done, done_prev, in_flight) | 5 | → 1 aggregate |
| 2.3 | `workspace.memberships.select_related("user")` | 1 | — (data needed) |
| 2.4 | `_daily(created)` + `_daily(done)` | 2 | possibly → 1 |
| 2.5 | `closed_pairs` (active people sparkline) | 1 | — |
| 2.6 | Alerts (overdue + due_soon + due_soon_urgent + urgent_stale + stuck_review) | 5 | → 1 aggregate |
| 2.7 | `status_rows` (pipeline) | 1 | — |
| 2.8 | `_build_cfd` (created count, created points, done count, done points × ISO week) | 4 | → 2 (`Count + Sum` together per direction) |
| 2.9 | `_build_velocity` (per-project done weekly + project list) | 2 | — (already batched) |
| 2.10 | `dist_project` (in_flight by project) | 1 | — |
| 2.11 | `prio_counts` (active priority breakdown) | 1 | — |
| 2.12 | `dist_label` (in_flight top labels) | 1 | — |
| 2.13 | `_build_people` (8-field row dump for all workspace tasks) | 1 | — but **O(n_tasks) row count** |
| 2.14 | Hygiene (no_assignee, no_priority, no_labels, no_due, no_description) | 5 | → 1 aggregate |
| 2.15 | `_build_heatmap` (activity log 7d × 24h) | 1 | — |
| 2.16 | `workspace.wip_config()` (model method, may be cached) | 0-1 | — |
| 2.17 | `tasks.values("project_id").distinct().count()` (project_count) | 1 | could merge with 2.10 |
| | **Total estimate** | **~32-33** | **→ ~8-12 with aggregate collapse** |

Memory baseline: **12 ms / ~30 KB**. With 32 queries averaging
~0.3 ms each (typical for indexed counts) that's ~10 ms of DB time
+ ~2 ms Python + render. **Collapsing to ~10 queries cuts DB time
by 60-70%**, bringing cold-load into the ~5-7 ms range. Not a
visible win for the user today, but it removes the "death by a
thousand counts" pressure as task count grows.

---

## 3. What works (good news)

### 3.1 `inline_static` templatetag is well-engineered (`web_extras.py:22-99`)

```python
@lru_cache(maxsize=32)
def _read_static_file(relative_path: str) -> str:
    found = finders.find(relative_path)
    if not found:
        return ""
    return Path(found).read_text(encoding="utf-8")
```

- LRU 32 entries — fits every realistic page-specific CSS/JS.
- DEBUG=True clears the cache before each call → dev edits picked up
  on reload.
- Prod: file read once per worker, served from RAM thereafter.
- `finders.find` uses Django's staticfiles config so it works in
  both DEBUG and `collectstatic` modes (`STATICFILES_DIRS` →
  `STATIC_ROOT` lookup chain).
- Returns `mark_safe` — required for the `{% inline_static %}`
  output to render as HTML, not escaped text.

**Docstring is exemplary** ("A `<link rel="stylesheet">` inserted
via innerHTML does NOT render-block …"). This is the kind of
kostyl-prevention comment that should live in every non-obvious
helper. [[feedback-stop-kostyling]] applies — the helper is the
correct fix, not a kostyl.

### 3.2 FOUC fix is robust

Comment chain (`b598340` commit + `_dashboard_inner.html` head):
- Dashboard's inner partial is loaded both inline (cold page) and
  via HTMX inner-fragment swap (range-chip click).
- A `<link rel="stylesheet">` injected by HTMX doesn't render-block —
  the swap content paints unstyled for the duration of the CSS
  fetch.
- `{% inline_static "css/dashboard.css" %}` emits the CSS rules in a
  `<style>` tag inside the swap content. Apply synchronously.
- Same pattern works for any partial CSS/JS that ships on one page
  only and must apply post-innerHTML swap.

### 3.3 Matrix + heatmap idempotency (`a3c1060`)

Memory note says the dashboard's `_dashboard_inner.html` IIFEs for
workload-matrix + heatmap now `body.innerHTML = ""` / `grid.innerHTML
= ""` before populating. Required because `acta.js` snapshots
`#app-content` AFTER the IIFE ran; cached HTML already contains
rows; on restore the IIFE re-runs and appended on top.

This is the cache-restore drift problem. The fix is correct and
matches the pattern any IIFE that **builds children imperatively**
needs in the page-cache router model. **Could be a reusable helper
exposed on `window.acta`**, e.g. `window.acta.idempotentMount(el,
buildFn)` that clears then runs. Defer to D1 (acta.js).

### 3.4 `_build_velocity` is N+1-free (`dashboard.py:509-558`)

Two queries: one `GROUP BY (project_id, week)` aggregate for the
per-project done sparklines, plus a `Project.objects.filter(...)`
to enumerate projects. The `for p in projects:` loop reads only
in-memory (`per_project` dict). Good.

### 3.5 `DashboardView` short-circuits `partial=1` (`views.py:1491-1504`)

A range-chip click sends `?range=14d&partial=1` with `HX-Request:
true` → `_dashboard_inner.html` is returned alone (the page chrome
stays cached). This is the lazy/partial pattern done right — no
duplication with the page template, no extra plumbing.

### 3.6 `project_insights` is a clean read view (`views.py:4373-4433`)

Single `compute_flow_metrics` + `compute_cfd` + `compute_bottlenecks`
call chain (the compute is in `apps/tasks/metrics.py`). View just
shapes the result into JSON blobs for Chart.js. No N+1 here — the
metric functions are the only candidate for heavy work, deferred
to C1.

---

## 4. Real findings

### 4.1 KPI / alert / hygiene → 14 queries collapsible to 3 aggregates

**KPI** (5 → 1):

```python
kpi = tasks.aggregate(
    created=Count("id", filter=Q(created_at__gte=since)),
    created_prev=Count("id", filter=Q(created_at__gte=prev_since, created_at__lt=since)),
    done=Count("id", filter=Q(status=Task.STATUS_DONE, completed_at__gte=since)),
    done_prev=Count("id", filter=Q(status=Task.STATUS_DONE, completed_at__gte=prev_since, completed_at__lt=since)),
    in_flight=Count("id", filter=Q(archived_at__isnull=True) & ~Q(status__in=[…])),
)
```

**Alerts** (5 → 1) similarly. **Hygiene** (5 → 1) similarly.

**Savings**: 12 queries → 3. Same SQL plan optimisation as the
project list view does
(`ProjectListView.get_queryset:1549-1572` uses conditional `Count`
per status — already best-practice for that view).

### 4.2 `_build_cfd` runs 4 queries for 2 series (`dashboard.py:471-500`)

```python
# Created series — TWO queries:
tasks.filter(created_at__gte=start).annotate(w=TruncWeek("created_at")).values("w").annotate(n=Count("id"))
# … and:
tasks.filter(created_at__gte=start).annotate(w=TruncWeek("created_at")).values("w", "size")
```

Same `WHERE` + `TruncWeek`. The first query gets `n=Count`, the
second iterates rows to sum sizes in Python. **Combine into one
aggregate**:

```python
tasks.filter(created_at__gte=start).annotate(w=TruncWeek("created_at")).values("w").annotate(
    n=Count("id"),
    pts=Sum("size"),
)
```

Same for the done series → 4 queries become **2**. Savings: 2.

### 4.3 `_build_people` materialises every task row (`dashboard.py:563-573`)

```python
rows = list(
    tasks.values(
        "assignee_id",
        "status",
        "priority",
        "created_at",
        "completed_at",
        "due_date",
        "updated_at",
        "archived_at",
    )
)
```

This is a one-query map-reduce — fine at 192 tasks, but **O(n_tasks)**
data volume. At 5 000 tasks it's 8 columns × 5 000 rows = ~200 KB
of payload over the DB wire each dashboard load. At 50 000 it's
~2 MB.

The Python loop after is O(n_tasks × constant). Same scaling.

**Threshold for revisit**: workspace task count > 5 000 OR dashboard
cold load > 100 ms. Document the threshold in code so future-Vox
knows where the breaking point is. **Possible future fix**: per-user
aggregates in SQL with `GROUP BY assignee_id` + multiple
conditional counts. Trade-off: more SQL complexity, less Python.

### 4.4 `dashboard.css` is outside the build pipeline (already in baseline §6)

310 LOC of handwritten CSS lives in `static/css/dashboard.css`. It's
not piped through Tailwind's purge, so:
- Unused rules accumulate silently.
- Adding a new rule requires manual `collectstatic` in prod (which
  `make deploy` does, but cold reads in dev hit `finders.find`
  every time DEBUG is on).

**Mitigations to consider**:

- **F-doc** Document the inventory: which rules in `dashboard.css`
  apply where. Two-paragraph header comment in the file naming the
  surfaces that consume it.
- **F-tailwind** Move static rules out of `dashboard.css` into
  Tailwind utilities (e.g. via `@apply` in `main.css`). Keep
  truly-runtime-computed rules (matrix grid columns, heatmap cells)
  in `dashboard.css`. Net loss likely under 100 LOC.
- **F-build** Add `static/css/dashboard.css` to the Tailwind config
  `content` array so any classes used inside it are kept and any
  Tailwind utilities referenced inline get purged correctly. **
  Risk: tiny.**

### 4.5 27 inline `style="…"` in `_dashboard_inner.html`

Baseline §6 caught this. Audit context: most are JS-computed (pipe
bar widths, heatmap cell colours) — those have to stay inline.
Some are static (e.g. fixed colours for accent dots) and could move
into Tailwind utilities or `dashboard.css`. Quick win when bundled
with the `dashboard.css` audit (§4.4). Defer to D3 (CSS audit).

### 4.6 `tasks.values("project_id").distinct().count()` in the return dict (`dashboard.py:435`)

```python
"dash_project_count": tasks.values("project_id").distinct().count(),
```

**Extra query** when `dist_project` (§2.10) already gives this
information for free — `len(dist_project)` is the same thing
(distinct project ids with at least one in-flight task) **except**
`dash_project_count` counts ALL projects with at least one task
(including done/cancelled/archived), while `dist_project` only
counts in-flight projects. **Different semantics.** Keep both, but
add a one-line comment so future-readers don't merge them
incorrectly.

### 4.7 `closed_pairs` Python set-build is fine

```python
closed_pairs = list(
    tasks.filter(status=Task.STATUS_DONE, completed_at__gte=since, assignee__isnull=False)
    .annotate(d=TruncDate("completed_at"))
    .values_list("d", "assignee_id")
)
active_people = len({a for _, a in closed_pairs})
per_day_people = {}
for d, a in closed_pairs:
    per_day_people.setdefault(d, set()).add(a)
```

One query, then in-Python set-building for two derived values
(active_people total + per-day sparkline). Clean. Same pattern
could squeeze the assignee distinct count into the aggregate
(saving a tiny bit), but reads better in current shape. **Leave.**

### 4.8 `_build_heatmap` reads ActivityLog, not tasks

`dashboard.py:659-673` — single query joining ActivityLog by
workspace + 7-day window, grouped by `(ExtractIsoWeekDay,
ExtractHour)`. Activity log is the right source for "when does the
team work?". No findings here.

### 4.9 `DashboardView` membership exists check on every request (`views.py:1499`)

```python
has_membership = WorkspaceMember.objects.filter(user=self.request.user).exists()
```

One query just to check if the user belongs to any workspace. For
99% of users this is `True` and the check is wasted (the real
workspace resolution happens via `resolve_active_workspace`
afterwards). Could be merged with `resolve_active_workspace` (which
already queries memberships) for one less DB round-trip per
dashboard cold load. Tiny win.

### 4.10 Project Insights — no lazy mechanism

`/projects/<slug>/insights/` renders all chart blobs in one pass.
197 LOC template. No `?panel=` short-circuit because the page is
single-purpose (one set of charts). Fine.

But: `project_insights` calls **three** compute functions
(`compute_flow_metrics`, `compute_cfd`, `compute_bottlenecks`).
Each likely scans the activity log + tasks. **Deferred to C1
(tasks)** — `apps/tasks/metrics.py` audit pending. Add to C1 input.

---

## 5. Subtle issues to verify in dev

| # | Issue | How to verify |
|---|---|---|
| 5.1 | Actual query count vs estimate (§2) | `CaptureQueriesContext` around a dashboard cold load |
| 5.2 | `_build_people` time on a populated workspace | `pytest --durations` on a test that seeds 1 000 tasks |
| 5.3 | `inline_static` cache invalidation on file change | Edit `dashboard.css` in DEBUG; verify next dashboard render picks it up |
| 5.4 | `?partial=1` HTMX swap doesn't break Chart.js init | Click a range chip; verify matrix + heatmap + sparklines re-render |
| 5.5 | `project_insights` time on a project with thousands of activity rows | `CaptureQueriesContext` on a project page; defer details to C1 |

---

## 6. Fix candidates (input to Chunk G)

| # | Tag | Title | Notes |
|---|---|---|---|
| F1 | `perf/query` `[4/2/2]` | Collapse KPI + alerts + hygiene counts into 3 aggregate queries (saves ~12 queries) | Mechanical refactor; full test coverage already exists for KPI numbers |
| F2 | `perf/query` `[3/2/1]` | `_build_cfd` — combine `Count("id")` + `Sum("size")` into one aggregate per direction (saves 2 queries) | Tiny change, isolated |
| F3 | `perf/query` `[2/1/1]` | Merge `WorkspaceMember.exists()` check with `resolve_active_workspace` (saves 1 query) | One-liner |
| F4 | `perf/scale` `[2/3/3]` | Threshold doc in `_build_people` ("rewrite as SQL groupby past 5000 tasks") | Doc only; no code change |
| F5 | `clean/css` `[2/2/1]` | Add `static/css/dashboard.css` to Tailwind `content` config | Risk: tiny; rebuild bundle once |
| F6 | `clean/css` `[2/3/2]` | Move static-only rules out of `dashboard.css` into Tailwind/`main.css` | Defer to D3; flag here |
| F7 | `clean/code` `[1/1/1]` | One-line comment on `dash_project_count` vs `dist_project` semantics distinction | Doc only |
| F8 | `perf/js` `[2/2/2]` | Promote idempotent IIFE-mount pattern to `window.acta.idempotentMount(el, buildFn)` | Defer to D1 |
| F9 | `tests/regress` `[3/1/1]` | `assertNumQueries` on dashboard cold load (locks in F1/F2/F3 wins) | Bundle with B1 F3 + B2 F8 + B3 F3 |
| F10 | `doc/inventory` `[1/1/1]` | Header comment in `dashboard.css` listing the surfaces it covers | Two paragraphs |
| F11 | `perf/c1` `[?/?/?]` | (defer to C1) Audit `compute_flow_metrics`, `compute_cfd`, `compute_bottlenecks` query patterns | Input to C1 |

---

## 7. Inputs to other Wave 1 chunks

- **D1 (acta.js)**: §3.3 idempotent IIFE-mount pattern is generally
  useful; D1 should look at the dashboard's matrix/heatmap IIFE
  approach and decide whether to factor a helper.
- **D3 / Wave 3 frontend depth**: `dashboard.css` audit (§4.4-4.5)
  belongs there.

## 8. Inputs to Wave 2 (placeholder)

- **C1 (tasks)**: `compute_flow_metrics`, `compute_cfd`,
  `compute_bottlenecks` in `apps/tasks/metrics.py` — likely heavy
  on ActivityLog scans. Verify N+1-safety.
- **C6 (activity)**: dashboard's heatmap reads ActivityLog with a
  group-by `(ExtractIsoWeekDay, ExtractHour)`. Confirm a
  `(workspace, created_at)` index covers it.

---

## 9. Status

- Chunk B4: **complete**.
- No code changed.
- Dashboard is fast today; structural waste (~24 redundant count
  queries) flagged. Concrete collapse path documented in §4.1.
- `inline_static` mechanism praised; `dashboard.css` outside build
  pipeline flagged.
- `_build_people` documented as the one O(n_tasks) function in the
  dashboard hot path.
- Project Insights compute deferred to C1.
- 11 fix candidates added to G's input set.
- Next chunk: D1 (`acta.js` / nav router / page cache).
