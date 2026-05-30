# 99 — Wave 2 synthesis + prioritized backlog

> **Wave 2 / Chunk G** — final pass.
> Date: 2026-05-29. Read-only. **No code changed.**
> Consolidates findings from chunks C1, C3, C5, C6, C7, C9 + measurements
> M1, M3, M4, M6, M7, M10 into a ranked fix queue with concrete PR
> bundles.

---

## 1. Bottom line

Six per-app audits on the backend (`tasks`, `workspaces`, `comments`,
`activity`, `notifications/SSE`, `filter sidebar`) + live measurements
on a populated workspace (`ksu24`: 260 tasks, 17 members).

**Wave 1 held.** No regressions surfaced. The invariants Wave 1 locked
in (single-writer `log_event`, `actaForceApplySelfEvent` opt-in,
prefetched workspace label sets, dashboard aggregate collapse) all
verify clean against the populated DB.

**Wave 2 finds the codebase tighter than the "feels heavy" complaint
suggests.** ~61 findings total, of which:

- **0 P0 / correctness bugs.**
- **3 measurement-driven opportunities** (dashboard bound over-permissive
  by 1.7×, list view byte-size 6× heavier than kanban, ILIKE
  `_filter_search` is the next index decision when row count grows).
- **~8 per-request memoization opportunities** in workspace-helper
  context builders (C3 + C9) — the largest mechanical perf win.
- **~10 test-coverage gaps** spread across comments / reactions /
  workspaces / activity invite-policy / notifications race + factories
  — moderate value, locks future invariants.
- **~3 admin-form validators** (Telegram placeholder, label namespace,
  workspace config round-trip) — small but visible.

**Effort for the top PR bundles: ~12 hours of focused work** for
PR-1 → PR-5 (the queue most worth shipping immediately). The remainder
is opportunistic — `PR-6+` are defer-friendly.

**The single largest user-visible win is NOT in this Wave 2 queue** — it's
the lazy-list scope captured by `project_todo_all_tasks_lazy_panels`
(3.7 MB / 260 tasks for the list view). That belongs to a focused
single-PR effort in Wave 3 once the queue here lands.

---

## 2. PR queue (proposed execution order)

Each entry is a small, focused PR. **Impact** (1-5) = perceptible
user value (or developer-velocity for invisible items). **Effort**
(1-5) = developer-hours. **Risk** (1-5) = likelihood of regression.

### PR-1 — Tighten dashboard `assertNumQueries` bound `[I:1 / E:1 / R:1]`

Measurement M6 shows the dashboard cold path runs at **25 queries** on
`ksu24`; Wave 1 PR-3 set the bound at `< 50`. Lower it to `< 30` with a
docstring rationale referencing this measurement.

- **`apps/web/tests/test_dashboard.py:124, 142`** — replace `< 50` with
  `< 30` and update the docstring (the "future defensible additions"
  paragraph should record the M6 anchor).

Estimated: **20 min**. No code change to production paths.

### PR-2 — Per-request workspace-helper memoization `[I:3 / E:2 / R:2]`

C3 F1+F8 and C9 F1 all flag the same root cause: every HTMX swap
rebuilds `_workspace_members` / `_workspace_labels` /
`_workspace_label_groups` / `_workspace_projects` / `_workspace_cycles`
from scratch even when the swap target was just the main task panel.
Across a heavy-edit kanban session (30+ swaps) this is ~30 × 5
= ~150 redundant queries.

- Add a per-request memoization cache (e.g., `request._workspace_cache`)
  to each helper.
- Verify `resolve_active_workspace` (Wave 1 PR-2 `B4 F3`) still owns
  the lookup; only cache *after* it.
- One regression test per helper showing identical output on a second
  call within the same request.
- Cross-check with `filter_sidebar_context` — the sidebar uses the
  same helpers and benefits the most.

Estimated: **2 h**. Expected delta: −20-40 queries on heavy-edit
sessions; −0 on cold load (memoization within a single request).

### PR-3 — Lock-in test suite for All Tasks panels `[I:2 / E:2 / R:1]`

M1 anchored the AllTasksView at **15 / 10 / 11 / 10 queries** for
cold + `?panel=table/kanban/list`. Lock these as
`assertMaxNumQueries(15)` / `(13)` / `(13)` / `(13)` regressions
following the Wave 1 PR-3 model. Parametrize the panel choice; cover
one filter-swap (`?priority=`) as a separate test.

- **`apps/web/tests/test_all_tasks.py`** — add a new test class
  `TestAllTasksQueryBudget` with five tests.
- Document the M1 measurement values in the docstring.
- Crucially: also test `?panel=list` payload size (`< 4 MB` upper
  bound) so a future field addition that bloats the row gets a
  pytest fail rather than a silent regression. Use
  `response.content` length.

Estimated: **2 h**.

### PR-4 — Comments + activity test coverage `[I:2 / E:2 / R:1]`

Closes most of the gaps surfaced by C5 §6 and C6 §6:

- C5 F3: thread integrity (depth-1 invariant) — test attempt to
  create a third-level comment is rejected.
- C5 F4: polymorphic-target delete cascade — task / comment /
  project_update deletion paths each leave reactions in expected
  state.
- C5 F5: reactions parity across all three target types in a single
  parametrized test.
- C6 F4: add `ActivityLogFactory` + `WorkspaceFactory` helper for the
  recurring "log_event under request.user" assertion pattern.

Estimated: **2.5 h**. Drops 4 open coverage gaps; gives the activity
log its first proper factory.

### PR-5 — Telegram placeholder validator + silent-skip log `[I:2 / E:1 / R:1]`

C7 F5: admin can save a template with `{statua}` (typo) and the bad
token reaches a real DM. Add a `clean()` method on
`TelegramMessageTemplate` that walks `_PLACEHOLDER_RE` and validates
each captured token against the known-good set per `kind`. Raise
`ValidationError` on first unknown. Add an admin "Preview" button as a
follow-up if time permits.

C7 F7: `_broadcast_notification` silently skips when the notification
was deleted between commit and broadcast. Add one `logger.debug(...)`
so the path is observable in operational logs (otherwise indistinguishable
from a no-recipient case).

C7 F8: parametrized test for assignee resolution (4 cells: unassign-only,
status-only, both, neither). Wave 1 already covers two of the four;
this closes the matrix.

Estimated: **1.5 h**.

### PR-6 — Factories sweep `[I:1 / E:1 / R:1]`

C6 F4 and C7 F9 both ask for factories. Add:

- `apps/activity/tests/factories.py` → `ActivityLogFactory`.
- `apps/notifications/tests/factories.py` → `NotificationFactory`,
  `TelegramAccountFactory`, `TelegramMessageTemplateFactory`.
- (Optional) `apps/comments/tests/factories.py` already exists?
  Verify; if not, add `CommentFactory` + `ReactionFactory`.

Estimated: **1 h**. Future tests stop bootstrapping inline.

### PR-7 — Activity project-scoped composite index (proposal) `[I:2 / E:1 / R:3]`

C6 F1 flags: `metrics.py` queries `ActivityLog` with
`(project, target_type, event_type)` predicates without a composite
index. On `ksu24` (1 105 rows) this is fine; **not deployed without
measurement first**.

- Run a follow-up M-series check on a 10 k+ activity row corpus
  (synthesize via factory) to confirm the seq scan cost.
- If confirmed: add `models.Index(fields=["project", "target_type",
  "event_type"], name="…")` and migration.
- Migration is a `!` commit per CLAUDE.md.

Estimated: **2 h** (1 h measurement + 1 h migration + test).
**Defer until M-confirmation.**

### PR-8 — Trigram index on `_filter_search` (proposal) `[I:2 / E:1 / R:2]`

M10 confirms `_filter_search` is `Seq Scan` over `(title, description)`
on `ILIKE '%q%'`. At 533 rows the cost is 4.7 ms — fine. Trigger
threshold:

- > 5 000 rows in a single workspace, OR
- EXPLAIN time > 50 ms on real query

When triggered: add `CREATE EXTENSION IF NOT EXISTS pg_trgm` migration,
plus `GinIndex(fields=["title"], opclasses=["gin_trgm_ops"], ...)` and
matching for `description`. Falls under Wave 4 / I1.

Estimated: **1.5 h** (when triggered). **Defer.**

### PR-9 — Documentation pass `[I:1 / E:1 / R:1]`

Sweep one-line comments + docstring updates across:

- C1 F2-F3, F7, F9 — `events.py` payload shape inline comment on
  fields that JS reads vs informational; `bulk.py` policy
  references to ADR 0012; `metrics.py` invariants on `assert
  workspace` paths.
- C3 F2-F6 — workspace helper docstrings noting they're cached
  (after PR-2).
- C5 F1 — comment on the redundant `validate_task` check in the
  serializer noting it's defense-in-depth (matches the viewset
  scope filter).
- C7 F1 — `_truncate_preview` docstring documenting write-once
  semantics.

Estimated: **1 h**. Pure docs / comments.

### PR-10 — List view byte-shave scope memo `[I:0 / E:0.5 / R:0]`

**Not a code PR — a planning artifact.** M1 / M3 show `?panel=list`
is 3.7 MB on 260 tasks (~14 KB/row vs 5 KB for table). The
`project_todo_all_tasks_lazy_panels` memo records the open scope;
update it with:

- A sub-section "Wave 2 measurement" with the exact numbers.
- The trade-offs identified: axis lazy-load vs row template
  refactor vs both.
- A recommendation: tackle as Wave 3's PR-1 (single focused PR, ~5 h).

Estimated: **30 min**. Refresh the memo for the next session.

**Running total: ~12 h for PRs 1-6 + PR-9 (defer 7 + 8 until
measurement triggers)**.

---

## 3. UAT (in-browser checks)

Wave 2 generated **no UAT items**. Every finding is verifiable via
`pytest` / `EXPLAIN` / `CaptureQueriesContext`. Skip the UAT round.

---

## 4. Deferred — need new measurements

Pause until the trigger condition is met.

| # | Item | Trigger | Action when ready |
|---|---|---|---|
| W2-M1 | `ActivityLog` row count > 10 k on any single workspace | factory-build 10 k events, re-run C6 §F1 EXPLAIN | If seq scan > 50 ms: ship PR-7 |
| W2-M2 | `_filter_search` ILIKE > 50 ms in EXPLAIN | grows past 5 k rows | Ship PR-8 |
| W2-M3 | Heavy-edit kanban session (30+ swaps) | manual UAT once PR-2 lands | Confirm −20-40 query delta |
| W2-M4 | Browser-side `applyClientFilters` profile (Wave 1 M5) | Lighthouse on `/tasks/?panel=list` `ksu24` | Drives R2 prioritization |
| W2-M5 | `pytest --durations=20` for slow-test baseline (Wave 1 M8) | Vox runs `pytest apps/web apps/tasks --durations=20` | Wave 4 / I infra targets |

---

## 5. Larger refactors — defer past Wave 2

These have real value but are too big for the ship-and-feel queue.

| # | Item | Size | When |
|---|---|---|---|
| R1 | List view byte-shave (lazy axis fetch + row template trim) | ~5 h + tests | Wave 3 / PR-1, post-PR-2 |
| R2 | Workspace + comment factories rollout — replace inline fixtures everywhere | ~4 h | Bundle with PR-6 in a "factories everywhere" follow-up |
| R3 | C7 F2 — pre-render fewer HTML surfaces per task on bulk update | ~6 h | After W2-M3 confirms the win |
| R4 | `events.py` payload extraction into a typed `TypedDict` per event | ~3 h | Wave 3 frontend audit (D2) prep |
| R5 | C9 F2 — split `apps/web/filters.py` (712 LOC) into per-concern files | ~5 h | Long-term maintainability |
| R6 | C1 — `apps/tasks/bulk.py` (740 LOC) cascade refactor for clarity | ~6 h | After ADR 0012 review |

---

## 6. Surface to Wave 3

Items that belong in Wave 3's frontend / template / SSE-handler audit:

| # | Wave 3 chunk | What |
|---|---|---|
| D2 | **`acta.js` deep dive** | Per-section read: SSE handlers vs Python payloads (C7 drift table is the input); refresh debounce coalescing (C7 F6) |
| D3 | **`templates/web/_filters_sidebar.html`** | Single largest template (661 LOC, 35 commits in 30 d). Pair with C9's findings for the holistic view |
| D4 | **`_task_row.html` / `_task_card.html` / `_table_row.html`** | Identical Alpine boilerplate per row; lazy-load opportunity; payload-size driver (M3 anchor) |
| D5 | **TipTap editor + attachment chips** | Wave 1 outscope; close `project_todo_attachments_inline_and_dedup` |
| D6 | **Cmd+K palette** | Wave 1 outscope; 501 LOC + 154 LOC view |

---

## 7. Surface to Wave 4 (infra)

| # | Item | What |
|---|---|---|
| I1 | EXPLAIN `_filter_search` on populated DB | W2-M2 trigger; PR-8 |
| I2 | Postgres index audit on activity `(project, target_type, event_type)` | W2-M1 trigger; PR-7 |
| I3 | Membership-join cost across `_user_task_qs` callers (Wave 1 I3) | unchanged from Wave 1 |
| I4 | `pytest --durations=10` → identify slow tests | W2-M5 |
| I5 | `_build_people` SQL groupby alternative when task count > 5 k | Wave 1 I5; now **measured fine at 260 tasks** (5.4 ms / 1 q); pickup deferred |

---

## 8. Memory hygiene

After Wave 2 ships:

- **Update on merge of PR-2**: refresh `[[project-todo-filter-sidebar-presets-views]]`
  memo with a note "per-request cache landed; presets/saved views still open".
- **Update on merge of PR-10 (memo)**: refresh
  `[[project-todo-all-tasks-lazy-panels]]` with Wave 2 measurements
  inline.
- **Keep**: every audit Wave 2 didn't touch.
- **Index update**: add `[Audit Wave 2 backlog](project_audit_wave2.md)`
  to `MEMORY.md` once written.

---

## 9. What this audit did NOT cover

For transparency:

- **`apps/web/views.py`** (7 285 LOC, 123 commits in 30 d) — the
  monolith. Wave 1 covered the All Tasks / Dashboard / Task Detail
  paths; Wave 2 covered the helpers. The remaining endpoints
  (search hover-card, exports, settings, member management,
  invite acceptance UI) are deferred.
- **`apps/cycles/services.py`** (591 LOC) — Scrumban math, ADR
  0026/0027. Out of scope.
- **`apps/attachments/`** — Wave 1 deferred; still deferred. Bundle
  with D5 in Wave 3.
- **MCP tools** (`write.py` 1 096 LOC + `read.py` 624 LOC) — not
  perf-relevant; defer indefinitely.
- **Bulk operations** (`apps/tasks/bulk.py` 740 LOC) — C1 audited
  surface invariants only; full cascade test matrix is R6 (Wave 3+).
- **Settings UI + workspace invite-flow** — Wave 2 covered only the
  invariant; the UI is C8 territory (still open).

---

## 10. Decision points for Vox

The audit captured **decisions Vox should make** before the queue
runs:

1. **PR-1 (tighten dashboard bound)**: 25 → `< 30` (margin of 5) or
   tighter (`< 28`)? Audit recommends `< 30` to leave room for one
   extra section.
2. **PR-2 (workspace helper memoization)**: cache on `request` vs
   thread-local. Audit recommends `request` — no surprise across
   workers, easier to reason about.
3. **PR-3 (panel test budget)**: keep bound as `assertMaxNumQueries`
   or switch to exact `assertNumQueries`? Audit recommends max — gives
   headroom for benign additions.
4. **PR-7 (activity index)**: when do we measure? Defer to first
   workspace > 10 k events, or proactively factory-build the test
   corpus now?
5. **R3 (HTML pre-render reduction)**: contentious — saving server
   ms for cost of one extra HTTP round-trip from clients. Decide
   after W2-M3 measurement.

---

## 11. Wave 2 status

- 6 audit chunks complete (C1, C3, C5, C6, C7, C9) + this synthesis +
  baseline.
- Methodology held: read-only, per-chunk report, no code changed,
  live measurements on populated workspace.
- ~61 findings, 10 PR bundles in §2, ~12 h of focused work to ship
  the queue (PR-1 → PR-6 + PR-9).
- All deferred items logged in §4 with concrete trigger conditions.
- Wave 3 inputs surfaced in §6.
- Wave 4 inputs surfaced in §7.
- Memory cleanup plan in §8.

**Next decision** is Vox's, not the audit's. Options:

A) **Ship PR-1 → PR-3 first** (~4.5 h) — perf bound + memoization +
   panel test budget. Quickest visible velocity per hour.
B) **Ship PR-1 + PR-3 + PR-4 + PR-5** (~6 h) — perf + tests +
   placeholder validator + silent-skip log; broader correctness +
   maintainability bundle.
C) **Defer Wave 2 PRs entirely; deploy Wave 1 first** — let users
   harvest Wave 1 value before merging more.
D) **Pause; start Wave 3 audit** (frontend / templates / SSE deep
   dive) before fixing.
E) **Some other order Vox prefers.**

Audit's vote: **C → A → B**. Wave 1 is a noticeable user-visible
shift; deploy it, harvest the feedback, then run Wave 2 perf PRs as
a follow-up batch. Wave 2 findings are all small enough that
ordering inside the batch doesn't carry risk.
