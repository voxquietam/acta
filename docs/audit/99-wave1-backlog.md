# 99 — Wave 1 synthesis + prioritized backlog

> **Wave 1 / Chunk G** — final pass.
> Date: 2026-05-29. Read-only. **No code changed.**
> Consolidates findings from chunks A, B1, B2, B3, B4, D1 into a
> ranked fix queue with concrete PR bundles.

---

## 1. Bottom line

After 6 audit chunks across the perceived-hot surfaces (All Tasks,
kanban + list + table, task detail, dashboard, nav router):

- **The app is in better shape than the "feels slow / janky"
  complaint suggested.** The lazy-panel mechanism is wired
  end-to-end and works. The base queryset is N+1-safe. The custom
  history router is correct. The SSE self-event filter prevents
  double-rendering with a clean opt-in escape hatch.
- **The two confirmed baseline N+1 suspicions are false positives**
  (`_decorate_comments` and the search hover-card label loop).
- **The headline `project_todo_inline_cells_propagation` is
  confirmed open** — 8 of 9 inline cells leave the underlying row
  stale after a modal edit. **Highest-visibility user-facing fix.**
- **One silent bug** — the aging-WIP card bar (`task.age_days`)
  never appears on All Tasks because `status_since` is annotated
  only in `ProjectDetailView`. Vox decision pending.
- **The biggest structural waste lives in two places**:
  - **Dashboard**: ~24 redundant `COUNT(*)` queries that collapse
    cleanly into 3 aggregate calls (Postgres handles them fast
    today, but it's a clear future-proofing win).
  - **`_table_row.html` and `_task_card.html`** each carry a
    long inline `x-data` block and 4-6 redundant `task.labels.all`
    evaluations per render. Both compound multiplicatively with
    row count.

Effort for the top-5 PR bundles: **~14 hours of focused work.**
Expected outcome: noticeably faster dashboard, ~150-200 KB shaved
off All Tasks first paint, end of the "modal change doesn't
update the row" bug for all 8 cells, and a regression-detector
test suite that locks the wins in.

---

## 2. PR queue (proposed execution order)

Each entry is a small, focused PR. **Impact** (1-5) = perceptible
user value. **Effort** (1-5) = developer-hours. **Risk** (1-5) =
likelihood of regression. Order is bundle-first, ship-and-feel
before the bigger refactors.

### PR-1 — Template light cleanup `[I:2 / E:1 / R:1]`

Trivial template hygiene with zero behavioural change.

- **B1 F1 + B2 F2**: wrap `task.labels.all` in `{% with
  labels=task.labels.all %}` in `_table_row.html`,
  `_task_row.html`, `_task_card.html`. Saves ~1 000 in-memory
  iterations per page render at current task volume.
- **B1 F6**: drop redundant `show_labels=True` pass in
  `_view_panel.html:33`.
- **B1 F11**: surface the `?panel=` cookie-skip rationale as a
  one-line code comment.
- **B2 F6**: one-line comment near `_kanban.html` header noting
  `?axis=` is a no-op there.
- **B4 F7**: one-line comment on `dash_project_count` vs
  `dist_project` semantics distinction.
- **B3 F7**: one-line comment in both detail body templates
  noting modal/page divergence to update both.

Estimated: **1.5 h**. Pre-existing tests should pass unchanged.

### PR-2 — Query collapses `[I:4 / E:2 / R:2]`

The real perf win. All `aggregate(Count("id", filter=Q(…)))`
style — Postgres returns the same rows with one query instead of
N.

- **B4 F1**: collapse dashboard KPI (5 queries → 1) + alerts
  (5 → 1) + hygiene (5 → 1) into 3 aggregates. Saves ~12 queries
  per dashboard load.
- **B4 F2**: `_build_cfd` combines `Count("id")` + `Sum("size")`
  per direction. Saves 2 queries.
- **B4 F3**: merge `WorkspaceMember.exists()` membership check
  with `resolve_active_workspace`. Saves 1 query per dashboard
  load.
- **B3 F1**: replace `task.labels.values_list("id", flat=True)`
  with `{l.id for l in task.labels.all()}` in
  `TaskDetailView.get_context_data` AND `task_meta_fragment`.
  Saves 1 query per task detail render + per SSE meta fragment
  refresh.

Estimated: **3 h**. Existing tests cover the numbers; add one
`assertNumQueries` per affected endpoint (rolls into PR-3).

### PR-3 — Regression test suite `[I:3 / E:2 / R:1]`

Locks in the wins from PR-2 and prevents future N+1 from sneaking
back.

- **B1 F3**: `assertNumQueries` on `AllTasksView` cold load,
  each `?panel=` short-circuit, and a filter-form swap.
- **B2 F8**: `assertNumQueries` on `?panel=kanban` cold + a DnD
  PATCH path.
- **B3 F3**: `assertNumQueries` on `TaskDetailView` (modal +
  page) and each fragment endpoint (`task_meta`,
  `task_timeline`, `task_comments`, `task_title`,
  `task_description`).
- **B4 F9**: `assertNumQueries` on dashboard cold + `?range=` swap.

Estimated: **3 h**. Each test is short (<20 lines).

### PR-4 — Inline cells propagation sweep `[I:4 / E:2 / R:2]`

The headline user-facing bug from `project_todo_inline_cells_propagation`.

- **B3 F2**: clone `actaForceApplySelfEvent` opt-in into 8
  inline cells: `_priority_cell`, `_assignee_cell`,
  `_due_date_cell`, `_end_date_cell`, `_start_date_cell`,
  `_cycle_cell`, `_project_cell`, `_size_cell`.
- **B3 F6** (optional same PR): hoist the `_status_cell` Alpine
  `x-data` state machine into a shared `Alpine.data(
  "inlineCellDropdown", …)` definition so each cell becomes
  `x-data="inlineCellDropdown"` instead of 19 LOC inline. Cuts
  per-cell boilerplate; trivial after F2.
- Test per cell in `apps/web/tests/test_inline_edits.py` (8
  cells → 8 tests; the file already has 1 508 LOC of test
  infrastructure to copy from).

Estimated: **3 h**. Closes `memory/project_todo_inline_cells_propagation.md`
on merge.

### PR-5 — UX toasts on async failure paths `[I:3 / E:1 / R:1]`

Visible feedback for the two raw-fetch silent failures.

- **D1 F1**: toast on `promoteTask` 4xx/5xx and on
  `handleKanbanDrop` rollback. Currently the user sees "nothing
  happened" or "the card snapped back" with no explanation.
- **D1 F2**: bump `actaForceApplySelfEvent` TTL 4 s → 30 s and
  add a one-line code comment explaining why (slow workers /
  fanout). Defensive; auto-cleanup still works.

Estimated: **1 h**.

### PR-6 — Aging-WIP bar decision `[I:2 / E:1 / R:2]`

Vox-decision PR: either backfill `status_since` annotation into
`_user_task_qs` so the aging bar appears on All Tasks kanban, OR
document the intentional skip in `_task_card.html` and
`_build_kanban_columns`.

- **B2 F1**: ONE of the two options. The "annotate" path adds
  one `Subquery` to the base queryset (cost: <2 ms / +1 query
  per row, scales fine). The "document" path is a comment-only
  change.

Estimated: **0.5 h** once Vox chooses.

### PR-7 — dashboard.css → build pipeline `[I:2 / E:2 / R:1]`

Bring `dashboard.css` under Tailwind's content scan so any
classes used inside it are kept, and dead utilities get purged.

- **B4 F5**: add `static/css/dashboard.css` to `tailwind.config.js`
  `content` array. Rebuild bundle once.
- **B4 F10**: header comment in `dashboard.css` listing which
  surfaces / templates consume it.

Estimated: **1 h**.

### PR-8 — SSE substatus wire `[I:2 / E:1 / R:2]`

- **B2 F2-SSE**: verify `recomputeKanbanSubstatus` runs on
  `acta:task-changed` SSE event (or wire it if not). Without
  this, a peer's status change updates the column count + cards
  but leaves the avatar stack stale until a hard reload.

Estimated: **1 h** (15 min verify + maybe 45 min implement +
test).

### PR-9 — Done-TODO cleanup `[I:1 / E:1 / R:1]`

- **B2 F3**: delete `memory/project_todo_kanban_substatus_recompute.md`
  + remove the MEMORY.md line (already implemented in
  `acta.js:1371`).
- **B2 F9** (after UAT confirms): delete or downgrade
  `memory/project_todo_kanban_filter_grouping_bugs.md` —
  bug #1 (spacing) likely already fixed by `[hidden]` +
  `display:none` pattern; bug #2 (axis carryover) is benign.

Estimated: **15 min** after the UAT step (next subsection).

### PR-10 — `window.acta` future-proofing `[I:1 / E:1 / R:1]`

- **D1 F5**: switch `window.acta = {…}` to `Object.assign(
  window.acta = window.acta || {}, {…})` so a future late-loaded
  script can't clobber exports.
- **D1 F7**: drop the `window.__actaInvalidatePageCache`
  defensive read (cosmetic).

Estimated: **30 min**.

**Running total: ~14.5 h for PRs 1-10.**

---

## 3. UAT (in-browser checks) — do these FIRST

These need 5 minutes total in a running dev stack. They unblock
PR-9 (TODO cleanup) and de-risk PR-4 (sweep).

| # | UAT | Outcome | Source |
|---|---|---|---|
| U1 | Card spacing under assignee filter | If clean: close kanban_filter_grouping_bugs #1 | B2 §2.2 |
| U2 | Aging-WIP bar on All Tasks kanban (look for left edge bar) | Confirm Vox decision data for PR-6 | B2 §4.1 |
| U3 | TipTap mount on task switch (open A → switch to B, no console warn) | Close [[memory]] item | B3 §4.2 |
| U4 | Collapsed-Done DnD target works | Confirm vs. doubt in B2 §4.8 | B2 §4.8 |
| U5 | Idiomorph fallback when CDN blocked | Defensive; one-off | D1 §5.3 |
| U6 | Toast queue replay on early HTMX error | Defensive; one-off | D1 §5.6 |

Block these out for a single 30-minute UAT session before
starting PR-4.

---

## 4. Deferred — need dev stack measurements

Pause until Vox brings the stack up. The order matches the
measurement methodology in `00-baseline.md:11`.

| # | Item | Source | Action when ready |
|---|---|---|---|
| M1 | Query count baseline on AllTasksView (cold + each `?panel=` + filter swap) | B1 §5 | Paste numbers into `01-all-tasks.md §5` |
| M2 | Payload size baseline on AllTasksView | B1 §5 | Same |
| M3 | `_table_row.html` rendered size | B1 §5 | Same |
| M4 | `?panel=kanban` query count with `?order=priority` (verify §4.3 prediction) | B2 §5.5 | `02-board-views.md §5` |
| M5 | `applyClientFilters` walk cost on a heavy page | B2 §5.6 | Same |
| M6 | Dashboard query count baseline (current vs after PR-2) | B4 §5.1 | `04-dashboard.md` followup |
| M7 | `_build_people` time on populated workspace | B4 §5.2 | Same |
| M8 | `pytest --durations=20` slow-test baseline | A §7 | Use for Wave 4 / F (infra) |
| M9 | Page-cache hit rate under live traffic | D1 §5.1 | Inform whether the cache pulls its weight |
| M10 | EXPLAIN on `_filter_search` (verify ILIKE vs trigram) | B1 §3.5 | Triggers F infra fix |

---

## 5. Larger refactors — defer past Wave 1

These have real value but are too big for the ship-and-feel
queue.

| # | Item | Size | When |
|---|---|---|---|
| R1 | B1 F2 — hoist labels-popover Alpine into shared store / `Alpine.data` | ~3 h + tests | After PR-3 (regression suite in place) |
| R2 | B2 F4 — scope `applyClientFilters` to active panel only | ~4 h + tests | After M5 measurement confirms it's worth it |
| R3 | D1 F4 — split `initTimeline` into `static/js/timeline.js` | ~4 h | Future maintainability PR; coordinate with D2 audit |
| R4 | D1 F10 — split `acta.js` into 8-10 logical files at section boundaries | ~1 day | Long-term frontend organization PR |
| R5 | B4 F6 — move static-only rules out of `dashboard.css` into Tailwind | ~3 h | Bundle with D3 (CSS audit) in Wave 3 |
| R6 | B1 F4 + B2 F7 — lucide `<symbol>+<use>` refactor | ~4 h | Bundle with D3 |

---

## 6. Surface to Wave 2

Items that belong in Wave 2's per-app audit:

| # | Wave 2 chunk | What |
|---|---|---|
| W1 | **C1 (tasks)** | Audit `apps/tasks/metrics.py` (`compute_flow_metrics`, `compute_cfd`, `compute_bottlenecks`) — input from B4 |
| W2 | **C3 (workspaces)** | `_workspace_members`, `_workspace_labels`, `_workspace_label_groups`, `_workspace_projects`, `_workspace_cycles` — input from B3 (deferred F5 evaluation) |
| W3 | **C5 (comments)** | Verify `summarize_reactions` + `attach_reactions` batching; polymorphic FK invariant tests; hover-card endpoint shape — input from B3 / D1 |
| W4 | **C6 (activity)** | Confirm `_task_activity` event-type filter still matches `log_event` output; review `task.labels_changed` exclusion vs full-activity-history page — input from B3 |
| W5 | **C9 (web)** | `filter_sidebar_context` (234 LOC) — memoise or refactor — input from B1 |
| W6 | **C7 (notifications/SSE)** | Verify `broadcast_task_events` payload shape matches `applyCardReplace/applyRowHtmlTable` consumers — input from D1 (drift risk between Python emit and JS handlers) |

---

## 7. Surface to Wave 4 (infra)

| # | Item | What |
|---|---|---|
| I1 | EXPLAIN `_filter_search` on populated DB | Confirm ILIKE behaviour; add pg_trgm/GIN index if sequential-scan | B1 F9 |
| I2 | Postgres index audit on `(workspace, created_at)` for heatmap | B4 §8 | |
| I3 | Membership-join cost across `_user_task_qs` callers | B1 §4.4 / B3 §3.3 | |
| I4 | `pytest --durations=10` → identify slow tests | A §7 | |
| I5 | `_build_people` SQL groupby alternative when task count > 5k | B4 F4 | |

---

## 8. Memory hygiene

Update `MEMORY.md` after Wave 1 wraps:

- **Delete on merge of PR-9**: `project_todo_kanban_substatus_recompute.md`.
- **Delete on merge of PR-4**: `project_todo_inline_cells_propagation.md`.
- **Update on merge of PR-6**: `project_todo_kanban_filter_grouping_bugs.md`
  (subset of bugs may close).
- **Keep**: `project_todo_all_tasks_lazy_panels.md` — the
  underlying lazy-panels mechanism is shipped, but the per-row
  payload (`_table_row.html`) is still where the next bytes-shave
  lives; the TODO captures the scope.

Update `MEMORY.md` add (after Wave 1 ship):
`[Audit Wave 1 backlog](project_audit_wave1.md)` already points
at the plan; add a one-line link to the docs/audit/ directory
once the first PR lands.

---

## 9. What this audit did NOT cover

For transparency:

- **Filter sidebar (`_filters_sidebar.html`, 661 LOC)** — heaviest
  template by LOC; not deep-read. Mentioned in B1 §3.4. Belongs
  to C9 (web) in Wave 2.
- **Cmd+K palette (`_command_palette.html`, 501 LOC + `palette_search`
  view 154 LOC)** — only structural references; not deep-read.
- **Bulk operations (`apps/tasks/bulk.py` 740 LOC + `_bulk_context_menu.html`
  272 LOC)** — flagged as recently-modified hotspot in A §4 but
  not in Wave 1 scope.
- **Settings + workspaces + invites** — not in Wave 1 scope. C3/C8
  in Wave 2.
- **Timeline / Gantt** (`_timeline.html` 455 LOC + `initTimeline`
  460 LOC) — structural only. D2 or split-PR.
- **MCP tools** (`apps/mcp/tools/write.py` 1 096 LOC + read 624 LOC)
  — not perf-relevant; defer indefinitely.
- **Telegram + notifications fanout** — touched only via SSE in D1.

---

## 10. Decision points for Vox

The audit captured **decisions Vox should make** before the queue
runs:

1. **PR-6 (aging-WIP bar)**: annotate `status_since` in
   `_user_task_qs` (cost +1 query) OR document the All-Tasks
   skip as intentional? (B2 §4.1)
2. **PR-4 (inline cells propagation)**: confirm sweep vs.
   per-bite preference? B3 §3.2 recommends sweep; the TODO
   notes Vox was undecided.
3. **R2 (`applyClientFilters` scope)**: do we wait for the M5
   measurement before doing it, or proceed on the qualitative
   argument? (Audit recommends: wait for M5.)
4. **PR-7 (dashboard.css → build pipeline)**: risk-tolerance
   check — adding the CSS file to `content` config triggers a
   bundle rebuild. Verify no surprise purge removal.

---

## 10b. Decisions locked 2026-05-29

After Vox UAT pass + decision review:

| # | PR | Decision | Notes |
|---|---|---|---|
| PR-4 | Inline cells propagation | **Sweep all 8** | Bundle with B3 F6 (`Alpine.data("inlineCellDropdown")` extraction). 8 tests in `test_inline_edits.py` |
| PR-6 | Aging-WIP bar | **Document, do NOT annotate** | UAT showed the bar is too subtle to register even on project kanban. Status_since stays project-only. Spawned new TODO [[project-todo-make-aging-wip-visible]] |
| PR-7 | `dashboard.css` → Tailwind content | **Add to content array** | Build bundle, compare size; ship if diff < 5 KB |

### UAT outcomes

| # | Item | Outcome |
|---|---|---|
| U1 | Card spacing under assignee filter | ✅ gap not collapsed → `project_todo_kanban_filter_grouping_bugs` **deleted** |
| U2 | Aging-WIP on All Tasks | UAT inconclusive (bar too subtle to perceive); PR-6 = document path |
| U3 | TipTap mount on task switch | ✅ works, no console warnings → fix confirmed |
| U4 | Collapsed-Done DnD target | ❌ **new bug**, captured as [[project-todo-kanban-collapsed-done-dnd]] |
| U5 | Idiomorph fallback | skipped (defensive only) |
| U6 | Toast queue replay | skipped (defensive only) |

### Memory hygiene applied

- **Deleted** `memory/project_todo_kanban_substatus_recompute.md` (implementation shipped — verified in B2 §2.1).
- **Deleted** `memory/project_todo_kanban_filter_grouping_bugs.md` (bug #1 UAT-confirmed closed; bug #2 was benign no-op).
- **Created** `memory/project_todo_kanban_collapsed_done_dnd.md` (new bug from U4).
- **Created** `memory/project_todo_make_aging_wip_visible.md` (spawned from PR-6 trade-off).
- `MEMORY.md` index updated to match.

### Ready-to-start order

1. **PR-1** (template light cleanup, 1.5 h) — quickest visible win, near-zero risk
2. **PR-2** (query collapses, 3 h) — real perf delta on dashboard
3. **PR-3** (regression test suite, 3 h) — lock in PR-1 + PR-2 wins
4. **PR-4** (inline cells sweep, 3 h) — user-visible UX fix
5. **PR-5** (UX toasts + TTL bump, 1 h) — small polish
6. **PR-7** (dashboard.css → content, 1 h) — bundle hygiene
7. **PR-6** (aging-WIP document, 0.5 h) — close the gap with comments
8. **PR-8** (SSE substatus wire verify, 1 h) — confirm + fix if needed
9. **PR-9** (memory cleanup) — already executed in this section ✓
10. **PR-10** (`window.acta` Object.assign, 0.5 h) — future-proof

**Plus** new standalone PR for collapsed-Done DnD bug — bundle decision later.

---

## 11. Wave 1 status

- 6 audit chunks complete (A, B1, B2, B3, B4, D1) + this synthesis.
- Methodology held: read-only, per-chunk report, no code changed.
- 50+ findings, 10 PR bundles in §2, ~14 h of focused work to
  ship the queue.
- All deferred measurements logged in §4 with concrete pickup
  instructions for when dev is up.
- Wave 2 inputs surfaced in §6.
- Wave 4 inputs surfaced in §7.
- Memory cleanup plan in §8.

**Next decision** is Vox's, not the audit's. Options:

A) **Ship PR-1 → PR-2 → PR-3 first** (~7.5 h) — bottom-half
   quick wins, regression-locked. Highest ROI per hour.
B) **Ship PR-4 alone next** (~3 h) — the user-visible "modal
   propagation" sweep. Higher visibility than perf.
C) **Pause; start Wave 2 audit** — keep mapping before fixing.
D) **Some other order Vox prefers.**

Audit's vote: **A → B → continue**. Lock in regressions first,
then the visible UX win, then decide whether more audit is
needed once we see the dashboard get faster.
