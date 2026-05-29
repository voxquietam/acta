lfdf# 00 — Baseline inventory

> **Wave 1 / Chunk A** — read-only project-wide audit pass.
> Date: 2026-05-29. Branch: `dev`. HEAD: `c514014`.
> Sources: 4 parallel Explore agents (backend / frontend / code-quality / tests).
> Purpose: map the project before drilling into hot pages (B1-B4) and
> the nav router (D1). Establishes a single reference for the rest of
> Wave 1. **No code changed.**

---

## 1. Top-line

| | |
|---|---|
| Python in `apps/` | ~42 180 LOC |
| Django apps | 15 (`accounts, activity, attachments, comments, common, cycles, labels, mcp, notifications, projects, reactions, tasks, telegram, web, workspaces`) |
| ADRs | 27 (0001-0027) |
| HTML templates (project) | 129 |
| Templates > 300 LOC | 8 |
| Alpine `x-data` blocks | 84 |
| HTMX endpoints (`hx-get/post`) | 147 |
| OOB swaps in templates | 13 |
| `acta.js` (nav router, monolith) | 3 244 LOC |
| Python test files | 97 |
| `def test_*` count | 1 341 |
| JS tests (vitest) | 2 files (filter + sort parity only) |
| Recent activity | 123 commits to `apps/web/views.py` in last 30 days |

**Code-quality markers: clean.** No `TODO/FIXME/XXX/HACK/KOSTYL`,
no `print`/`pdb`/`breakpoint` leftovers, no 5+ line commented blocks,
2 justified `# noqa: F401` (signal-registration imports). The "kostyli"
live in code shape and architecture, not in markers — which is the whole
point of this audit.

---

## 2. App inventory matrix

| App | Models | Views | Tests | Migrations | Notes |
|---|---:|---:|---:|---:|---|
| accounts | 2 | 6 page (545 LOC) | 7 files / 74 | 8 | Custom User, ApiToken (prefix-only reveal); auth churn (15 commits in 30d) |
| activity | 1 | 1 DRF / 0 page | 2 files / 11 | 4 | Append-only log; `log_event` is the only writer; `on_commit` SSE broadcast |
| attachments | 1 | 0 / 0 | 9 / 72 | 3 | Polymorphic FK (task/comment/project), dedup, magic-sniff, conftest with isolated MEDIA_ROOT |
| comments | 1 | 1 DRF | 2 / 12 | 4 | Polymorphic + 1-level threading. **Weak test coverage** |
| common | – | – | 2 / 19 | – | Markdown + scheduled-jobs harness |
| cycles | 1 | 0 / 0 | 8 / 55 | 3 | 591-LOC `services.py` — burndown, ideal-line, rollover |
| labels | 2 | 2 DRF / 1 page | 4 / 26 | 5 | Exclusive groups, hex validation, OOB pickers |
| mcp | – | 1 async | 6 / 113 | – | JSON-RPC tools (read 624 LOC, write 1 096 LOC) |
| notifications | 1 | 0 / 0 | 4 / 49 | 5 | 469 LOC fan-out, denorm preview, null-safe on deletes |
| projects | 2 | 2 DRF / 1 page | 2 / 19 | 6 | Task-number `SELECT FOR UPDATE`, immutable `slug_prefix` |
| reactions | 1 | 0 / 0 | 1 / 11 | 2 | Polymorphic, partial unique per target |
| tasks | 1 | 1 DRF / 1 page | 10 / 135 | 13 | **Core** — 473 LOC model, 740 LOC `bulk.py`, 471 LOC `events.py` |
| telegram | 3 | 1 page | 4 / 73 | 5 | 497 LOC services, regex placeholders, mute config |
| web | – | ~80 page views | 30 / 535 | – | **Monolith** — `views.py` 7 285 LOC, 123 commits in 30d |
| workspaces | 4 | 1 DRF / 1 page | 4 / 56 | 7 | Tenant root, role through-model, JSON config |

Notable shape:
- `web` is the only app without a `factories.py` despite the largest test
  count (535) — inline test setup everywhere.
- `activity` has **no `ActivityLog` factory** even though `log_event` is
  the headline anti-Kaneo invariant (ADR 0011).
- `reactions` has 1 test file for a polymorphic surface across
  task / comment / project_update — coverage is thin.

---

## 3. Heaviest files (>500 LOC, `apps/`)

| File | LOC | Comment |
|---|---:|---|
| `apps/web/views.py` | **7 285** | 123 commits in 30d. The audit's #1 hotspot. |
| `apps/web/tests/test_inline_edits.py` | 1 508 | Single test file covering every inline cell. Slow feedback. |
| `apps/mcp/tools/write.py` | 1 096 | Tool schemas — verbose, stable |
| `apps/tasks/bulk.py` | 740 | All-or-nothing bulk PATCH (ADR 0012) |
| `apps/web/filters.py` | 712 | `filter_sidebar_context` is 234 LOC alone |
| `apps/web/dashboard.py` | 673 | Aggregations + chart-data assembly |
| `apps/mcp/tools/read.py` | 624 | Tool schemas |
| `apps/cycles/services.py` | 591 | Scrumban math (ADR 0026/0027) |
| `apps/accounts/views.py` | 545 | Auth + invites + API tokens |
| `apps/web/templatetags/web_extras.py` | 528 | 23 commits in 30d — high template-render coupling |

`static/js/acta.js` (bundle, but hand-written): **3 244 LOC** — separate
hotspot, audited in B6 (D1).

---

## 4. Recently-modified hotspots (last 30 days)

Files churned ≥ 13 times → highest regression risk for Wave 1.

| Rank | File | Commits | LOC | Touched |
|---:|---|---:|---:|---|
| 1 | `apps/web/views.py` | 123 | 7 285 | 2026-05-29 |
| 2 | `templates/base_app.html` | 61 | 594 | 2026-05-29 |
| 3 | `apps/web/urls.py` | 57 | 527 | 2026-05-29 |
| 4 | `templates/web/_filters_sidebar.html` | 35 | 661 | 2026-05-28 |
| 5 | `templates/web/projects/_kanban.html` | 26 | 215 | 2026-05-28 |
| 6 | `templates/web/projects/_task_card.html` | 25 | – | 2026-05-28 |
| 7 | `templates/web/_task_row.html` | 24 | – | 2026-05-28 |
| 8 | `apps/web/templatetags/web_extras.py` | 23 | 528 | 2026-05-29 |
| 9 | `apps/web/filters.py` | 19 | 712 | 2026-05-28 |
| 10 | `apps/tasks/events.py` | 17 | 471 | 2026-05-27 |
| 11 | `apps/tasks/bulk.py` | 16 | 740 | 2026-05-26 |
| 12 | `apps/accounts/views.py` | 15 | 545 | 2026-05-29 |
| 13 | `apps/tasks/models.py` | 13 | 473 | 2026-05-26 |

Pattern: the perceived jank lives in **web/views.py + base_app.html +
filters_sidebar.html + project board partials** — exactly what Wave 1
B1-B4 + D1 target.

---

## 5. Heaviest templates (top 15)

| Template | LOC | Area |
|---|---:|---|
| `templates/web/_filters_sidebar.html` | 661 | filters |
| `templates/base_app.html` | 594 | layout |
| `templates/web/_dashboard_inner.html` | 536 | dashboard |
| `templates/web/_command_palette.html` | 501 | Cmd+K |
| `templates/web/projects/_timeline.html` | 455 | timeline/Gantt |
| `templates/web/projects/_task_context_menu.html` | 396 | bulk/ctxmenu |
| `templates/web/projects/_overview_panel.html` | 391 | project overview |
| `templates/accounts/settings.html` | 328 | settings |
| `templates/web/projects/_bulk_context_menu.html` | 272 | bulk |
| `templates/web/_create_task_modal.html` | 265 | modals |
| `templates/web/projects/list.html` | 247 | list view |
| `templates/web/projects/_links_panel.html` | 228 | links |
| `templates/web/workspaces/_settings_labels.html` | 227 | settings |
| `templates/web/projects/_kanban.html` | 215 | kanban |
| `templates/web/workspaces/settings.html` | 212 | settings |

---

## 6. Frontend shape — quick facts

- **Stack**: server-rendered Django + HTMX + Alpine + Chart.js +
  sortable.js; TipTap editor + reactions are the only esbuild bundles
  (`package.json`). Tailwind compiled to `static/css/main.bundle.css`.
- **Custom history router** (ADR 0024) lives in `static/js/acta.js`,
  ~3 244 LOC monolith. LRU page cache (20), token-based abort, popstate
  handler, htmx interception. **Primary suspect for nav jank** → B6.
- **84 `x-data` blocks** total; largest is in `_filters_sidebar.html:44`
  (~50 LOC state machine, candidate for extraction to an Alpine store).
- **HTMX**: 58 `hx-get`, 89 `hx-post`. 13 OOB swaps. 20+ `hx-boost`
  links. **0 PATCH/PUT/DELETE through htmx** — they go through `fetch`
  from `acta.js` (worth checking for parity with HTMX restore behaviour).
- **No `morph:outerHTML` plugin in production**, despite being mentioned
  in some templates — anything relying on morphing falls back to default
  `outerHTML` swap (re-mounts Alpine, drops focus, etc.). This is a
  candidate root cause for several "feels janky" issues; flagged for B2/B3.
- **Inline `style="…"`**: 139 occurrences, concentrated in dashboard
  (27 in `_dashboard_inner.html`) and timeline (10 in `_timeline.html`).
  Mostly JS-computed widths/colors — intentional for data viz, but
  worth checking maintainability in B4.
- **`dashboard.css`** is **not** in the Tailwind/esbuild build pipeline
  — it's hand-written and served as a static file. Inlined into HTML
  via `{% inline_static %}` (`web_extras.py`) to dodge FOUC. **Risk**:
  unused rules pile up silently. Flagged for B4.

---

## 7. Test-coverage matrix

| App | Files | Tests | Factory | conftest | Critical-path gap |
|---|---:|---:|---|---|---|
| accounts | 7 | 74 | ✓ User | – | – |
| activity | 2 | 11 | **✗** | – | No ActivityLog factory; few diff-emission tests |
| attachments | 9 | 72 | ✓ | ✓ | – |
| comments | 2 | 12 | ✓ | – | No polymorphism test (Task vs ProjectUpdate CT) |
| common | 2 | 19 | – | – | – |
| cycles | 8 | 55 | ✓ | – | – |
| labels | 4 | 26 | ✓ | – | No OOB-fragment test for picker |
| mcp | 6 | 113 | – | – | – |
| notifications | 4 | 49 | ✓ | – | No multi-workspace recipient edge case |
| projects | 2 | 19 | ✓ | – | – |
| reactions | 1 | 11 | – | – | No CT consistency test on move |
| tasks | 10 | 135 | ✓ | – | No HTTP-layer test for bulk PATCH; only service layer |
| telegram | 4 | 73 | – | – | No webhook signature test |
| web | 30 | 535 | **✗** | – | **No factories** — inline setup everywhere |
| workspaces | 4 | 56 | ✓ | – | – |

**No `@pytest.mark.slow` / `flaky` / `skip` / `xfail` anywhere** — either
all tests run fast or this dimension is genuinely unused. Worth a
`pytest --durations=20` once during F (infra) to confirm.

---

## 8. Top backend suspicions (raw, to verify in B1-B4)

| # | Place | Suspicion |
|---|---|---|
| 1 | `apps/web/views.py:2616` `_decorate_comments` | `for reply in comment.replies.all()` inside loop; surrounding prefetch may not cover it |
| 2 | `apps/web/views.py:3440` | `for label in t.labels.all()` in serialised task dict — M2M without prefetch in bulk export |
| 3 | `apps/web/views.py:409` `_get_user_task_or_404` | `.filter().first().attr` chain without `select_related("project__workspace")` |
| 4 | `apps/cycles/services.py:80` | `for cycle in workspace.cycles.all()` in `reconcile_statuses()` — no `select_related` |
| 5 | `apps/web/views.py:7285` (file size) | 80+ view functions, complex interdependencies, mutation-in-place on comment decoration |
| 6 | `apps/tasks/serializers.py:85, 158` | `.filter().exists()` in `validate_*` runs on every write |
| 7 | `apps/projects/models.py:270` | `@cached_property` `top_level_comments` materialises to list — stale-reply risk if instance reused |

None are confirmed regressions; each becomes a measurement target in the
relevant Wave 1 chunk.

---

## 9. Top frontend suspicions (raw, to verify in B/D)

| # | Place | Suspicion |
|---|---|---|
| 1 | `static/js/acta.js` (3 244 LOC) | Single file: nav router, history cache, client-side filter mirror (~300 LOC), kanban DnD, timeline (~1 000 LOC). Hard to reason about side effects → D1. |
| 2 | `templates/base_app.html` 10 inline `<script>` tags | Order-of-load + Alpine `defer` interaction — handled today via `window.Alpine` checks but fragile → B/D pass |
| 3 | `_filters_sidebar.html:44` 50+ LOC `x-data` | Long inline state machine; localStorage; counts → candidate for extraction to Alpine store |
| 4 | Date cells (`_start_date_cell.html`, `_end_date_cell.html`, `_due_date_cell.html`) | Three near-identical templates ~197 LOC each; merge to one parameterised partial |
| 5 | Status / priority / project cells | Tri-state logic identical, only CSS differs — same dedup opportunity |
| 6 | `_create_task_modal.html` `x-data` | localStorage `openAfter` toggle; check for race against custom history restore |
| 7 | `actaForceApplySelfEvent` only in `_status_cell.html` | Known TODO `project_todo_inline_cells_propagation` — modal mutation does not reach the row for priority/assignee/due/start/end/cycle/project/size |
| 8 | `dashboard.css` outside build pipeline | Orphaned rules accumulate; no Tailwind purge sees it |
| 9 | 27 inline `style="…"` in `_dashboard_inner.html` | Mostly JS-computed widths — but if any are static, they belong in `dashboard.css` |
| 10 | `templates/base_app.html` 61 commits in 30d | Highest layout churn → highest regression risk |

---

## 10. Pre-known backlog (from `MEMORY.md`)

`MEMORY.md` already catalogs ~50 active TODOs. Wave 1 is **not** trying
to absorb that list — it's looking for things `MEMORY.md` doesn't know
about yet. The relevant pre-known items the Wave 1 chunks will keep an
eye on, but not duplicate:

- `project_todo_all_tasks_lazy_panels` — All Tasks 1.7 MB / 400 ms.
- `project_todo_inline_cells_propagation` — `actaForceApplySelfEvent` opt-in.
- `project_todo_kanban_substatus_recompute` — overdue + avatar stack stale after JS-hide.
- `project_todo_kanban_filter_grouping_bugs` — assignee filter spacing + group-by bleed-through.
- `project_todo_dependent_assignee_filter` — top strip should derive from sidebar queryset.
- `project_todo_overflow_kills_popovers` — `overflow:hidden/auto` clipping dropdowns.
- `feedback_no_quotes_in_alpine_xdata` — `"` inside JS comment inside `x-data=""` bug.
- `project_django_template_cached_loader_fix` — dev.py loaders config.

---

## 11. Measurement methodology for Wave 1

Each B/D chunk will produce metrics in a consistent shape so we can rank
findings later (Chunk G):

1. **Query profile** — `CaptureQueriesContext` around the view; count
   and list (deduped by SQL signature) the queries; flag any that grow
   with `len(qs)`.
2. **Payload size** — `curl -s -o /dev/null -w '%{size_download}\n'`
   against the rendered page (when the dev container is up) **or**
   static `wc -c` on the rendered output via Django test client.
3. **Time-to-first-byte** — `manage.py test_render <url>` (cheap loop)
   for ~5 hot URLs; we'll just diff before/after fix-PRs.
4. **JS bytes per page** — count `<script>` tags + total bundle size
   shipped on first paint.
5. **Alpine init time** — `performance.mark('alpine:init:start/end')`
   around `Alpine.start()`; surfaced in D1.

The hard prerequisite is "dev stack must be running" for 1-4. The user
controls when to bring it up; the audit chunks do **not** start it.

---

## 12. Wave 1 execution plan

| # | Chunk | Output | Status |
|---|---|---|---|
| A | This file | `00-baseline.md` | ✓ done |
| B1 | All Tasks (1.7 MB / 400 ms) | `01-all-tasks.md` | next |
| B2 | Kanban + list + table | `02-board-views.md` | queued |
| B3 | Task detail (modal + page) | `03-task-detail.md` | queued |
| B4 | Dashboard + project insights | `04-dashboard.md` | queued |
| D1 | `acta.js` / nav router / page cache | `05-nav-router.md` | queued |
| G | Wave 1 synthesis → fix-PR backlog | `99-wave1-backlog.md` | queued |

Order rationale: the user feels jank most in **All Tasks, Dashboard/nav,
Kanban**. B1 first because it's the only chunk with quantified pain
(1.7 MB / 400 ms). D1 last in Wave 1 because the page-cache router
touches every other chunk's findings — better to know what each page
needs from it before judging the router itself.
