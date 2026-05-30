# 00 — Wave 2 baseline + measurements

> **Wave 2 / Chunk A** — read-only project-wide audit, second pass.
> Date: 2026-05-29. Branch: `dev`. HEAD: `8fb4a9b` (11 commits ahead of
> prod `c514014`, 701 tests pass).
> Sources: 6 parallel Explore agents (C1 tasks, C3 workspaces, C5
> comments, C6 activity, C7 SSE, C9 filter sidebar) + live
> measurements on the docker stack.
> Purpose: anchor Wave 2's findings against current real numbers
> (Wave 1 captured upper bounds via `assertNumQueries`; this records
> the actual cost on a populated workspace). **No code changed.**

---

## 1. Stack state

Docker compose running (3 days uptime). Containers:

| Container | Status | Port |
|---|---|---|
| `acta.web` (uvicorn) | Up 3 days | host `:8001` → container `:8000` |
| `acta.db` (postgres:16) | Up 3 days healthy | host `:5433` |
| `acta.qcluster` (django-q) | Up 3 days | internal |

Probe user: `admin` (inactive Kaneo-imported user with workspace
membership in `ksu24`).

## 2. Database inventory (probe corpus)

Wave 2 measurements run against the actual prod-like data Vox already
has on dev (Kaneo import landed 2026-05-29 to `ksu24`).

| Entity | Total | Notes |
|---|---:|---|
| Users | 32 | 16 Kaneo placeholders + Vox + test fixtures |
| Workspaces | 7 | only 3 are non-trivial: `ksu24` / `audit` / `test` |
| WorkspaceMembers | 46 | |
| Projects | 56 | `ksu24` 18 + `audit` 21 + `test` 16 |
| Tasks | 533 | `ksu24` 260 + `audit` 193 + `test` 79 |
| Comments | 134 | |
| ActivityLog | 1 105 | |
| Labels | 40 | |
| Notifications | 319 | |

Workspace `ksu24` is the canonical heavy probe (260 tasks, 17 members).
`audit` is a stress-side corpus (193 tasks across 21 projects). `test`
is the seeded fixture from earlier sessions.

---

## 3. Live measurements (M1–M7, M10)

All numbers captured on the running docker stack, `ksu24` workspace,
`admin` user, default page settings. **Times include uvicorn + middleware
round-trip via `django.test.Client`** (not browser-rendered).

### M1 / M2 — AllTasksView cold + each `?panel=` + filter swap

| URL | Status | Queries | Wall (ms) | Payload (bytes) | Note |
|---|---|---:|---:|---:|---|
| `/tasks/` | 200 | **15** | 1 889 | **1 561 694** (1.5 MB) | cold load, default panel |
| `/tasks/?panel=table` | 200 | 10 | 327 | 1 340 163 (1.3 MB) | lazy panel — chrome stripped, body inline |
| `/tasks/?panel=kanban` | 200 | 11 | 223 | **603 835** (604 KB) | lazy panel — kanban grouping |
| `/tasks/?panel=list` | 200 | 10 | 895 | **3 744 965** (3.7 MB) | **lazy panel — list is the heaviest by 6×** |
| `/tasks/?priority=1,2` | 200 | 15 | 372 | 1 561 846 | filter swap, cold |
| `/tasks/?status=in-progress` | 200 | 15 | 107 | 376 079 | filter swap, narrow |

**Key takeaways:**
- **No N+1 in any path.** Cold load 15 queries; lazy panels 10–11.
  Wave 1 PRs locked these in.
- The list view is **3.7 MB on 260 tasks**, ~6× the kanban payload.
  This is the headline byte-shave target — `project_todo_all_tasks_lazy_panels`
  identifies it; Wave 2 confirms with numbers.
- A filter swap (`?priority=1,2`) re-runs the cold load (15 queries
  including chrome). No additional cost beyond the filter narrowing.

### M3 — per-row payload size

Derived from M1 / total tasks (260 in `ksu24`):

| Panel | Total bytes | Per-task ≈ | Source |
|---|---:|---:|---|
| `table` row | 1 340 163 | **5.2 KB** | `_table_row.html` |
| `kanban` card | 603 835 | **2.3 KB** | `_task_card.html` |
| `list` row | 3 744 965 | **14.4 KB** | `_task_row.html` (×N axis splits) |

The list view's per-row cost (14.4 KB) is ~3× the table row. The
multiplier hides because `_task_row.html` is repeated across five axes
in the list view layout (status / project / assignee / priority / due).

### M4 — kanban with `?order=priority`

| URL | Queries | Wall (ms) | Note |
|---|---:|---:|---|
| `/tasks/?panel=kanban&order=priority` | **11** | 224 | identical to vanilla kanban — `?order=` is a UI hint, not a query parameter at the column-building layer |

Wave 1 B2 §4.3 prediction confirmed: `?order=` does NOT raise query
count on kanban.

### M6 — Dashboard

| URL | Queries | Wall (ms) | Payload (bytes) | Bound |
|---|---:|---:|---:|---|
| `/` | **25** | 78 | 159 596 | `< 50` (Wave 1 PR-3) |
| `/?range=30d` | 25 | 73 | 159 815 | `< 50` |
| `/?range=14d&partial=1` | 25 | 71 | 159 596 | `< 50` |

**The dashboard runs at half its assertNumQueries bound.** This is a
tightening opportunity for the C1/C9 follow-up (bound: 50 → 30 with
margin). Verified that PR-2 aggregate collapses survive into `ksu24`'s
shape (not just the synthetic 40-task workspace the regression test
uses).

### M7 — `_build_people` on populated workspace

`ksu24`: 17 members, 260 tasks (input is a queryset, function does its
own `.values(...)`).

| Run | Wall (ms) | Queries |
|---|---:|---:|
| 1 | 5.91 | 1 |
| 2 | 5.28 | 1 |
| 3 | 5.37 | 1 |
| 4 | 5.39 | 1 |
| 5 | 5.15 | 1 |

**Mean ≈ 5.4 ms, 1 query.** Not a bottleneck at present scale.
Wave 1 B4 §5.2 marked this "deferred to measurement" — measurement
done; safe to leave. F4 R5 (`B4 F6` SQL groupby alternative) is
deferred indefinitely.

### M10 — `_filter_search` EXPLAIN ANALYZE

Search uses `Q(title__icontains=q) | Q(description__icontains=q)`
(filters.py:362). EXPLAIN ANALYZE on the populated DB (533 rows):

```
Limit  (cost=0.00..45.45 rows=1 width=57) (actual time=0.199..4.650 rows=6 loops=1)
  ->  Seq Scan on tasks_task  (cost=0.00..45.45 rows=1 width=57) (actual time=0.199..4.647 rows=6 loops=1)
        Filter: (((title)::text ~~* '%bug%'::text) OR (description ~~* '%bug%'::text))
        Rows Removed by Filter: 527
Planning Time: 1.284 ms
Execution Time: 4.666 ms
```

- **Seq Scan** as expected for unanchored `ILIKE '%...%'`.
- 4.7 ms on 533 rows. Linear scaling: ~47 ms / 5 k rows, ~470 ms / 50 k
  rows.
- **Decision deferred** until row count grows. `pg_trgm` + GIN
  `gin_trgm_ops` would push this to constant-time for the same query.
  Surface ticket: Wave 1 I1 (infra audit) — captured in §6 below.

---

## 4. Measurements NOT run (deferred)

| # | Item | Why deferred | Pickup signal |
|---|---|---|---|
| M5 | `applyClientFilters` walk cost on a heavy page | needs browser DevTools profile, not server-side measurement | Vox runs Lighthouse on `/tasks/?panel=list` on `ksu24` |
| M8 | `pytest --durations=20` slow-test baseline | global rule: do not run the full suite without explicit ask | Vox says "run it" — then targeted `apps/web apps/tasks` first |
| M9 | Page-cache hit rate under live traffic | requires production-like traffic + Caddy/nginx logs | post-deploy |

---

## 5. Findings carried into the per-chunk reports

Each Wave 2 chunk has its own report and findings. This baseline
records cross-chunk observations only.

**Cross-chunk observations:**

- **The list view (3.7 MB / 260 tasks) is the single largest perceived-jank
  candidate the audit can address with a non-trivial PR.** Wave 1
  `project_todo_all_tasks_lazy_panels` carries the scope; Wave 2 C9
  confirms the sidebar swap doesn't multiply this cost; Wave 2 C1
  metrics walkthrough confirms no per-row query loop. Mitigation
  lives in the template (column lazy-load, axis lazy-fetch). Not a
  Wave 2 sweep; a focused PR for Wave 3.
- **Dashboard `< 50` bound is now over-permissive (real 25).** Tighten
  to `< 30` once Wave 2 lands, with rationale in the docstring.
- **`_filter_search` ILIKE is fine today (4.7 ms / 533 rows).** Capture
  the trigger threshold (`> 5 000 rows` or `EXPLAIN time > 50 ms`) for
  the future infra pickup; Wave 4 / I1.
- **Telegram quiet hours feature gap (C7 F3) cross-references**
  memory `[[project-todo-telegram-quiet-hours]]` — not a Wave 2 PR.

---

## 6. Wave 2 chunk reports

| # | Chunk | Report | Findings |
|---|---|---|---:|
| C1 | tasks | `01-tasks.md` | 10 |
| C3 | workspaces | `02-workspaces.md` | 11 |
| C5 | comments + reactions | `03-comments.md` | 8 |
| C6 | activity log | `04-activity.md` | 10 (5 confirmations + 5 findings) |
| C7 | notifications + SSE | `05-notifications-sse.md` | 10 |
| C9 | filter_sidebar_context | `06-filter-sidebar.md` | 12 |

**Total finding count: ~61.** Most low-severity / documentation /
test-coverage. Wave 2 synthesis (`99-wave2-backlog.md`) collapses
into a ranked PR queue.

---

## 7. Wave 2 status

- 6 chunks complete (C1, C3, C5, C6, C7, C9) + this baseline.
- Methodology held: read-only, per-chunk report, no code changed.
- Measurements run: M1, M3, M4, M6, M7, M10. Deferred: M2 (covered by
  M1 payload column), M5 (needs browser), M8 (policy), M9 (needs prod
  traffic).
- Next: synthesis → `99-wave2-backlog.md` ranks PRs by impact / effort /
  risk and binds to Wave 1's outstanding M-series.
