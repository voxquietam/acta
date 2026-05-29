# 02 — Board views (Kanban + List + Table)

> **Wave 1 / Chunk B2** — project board panels at `/projects/<slug>/`
> and the equivalent panels under `/tasks/` (All Tasks).
> Date: 2026-05-29. Read-only. **No code changed.**
> Sources: `_kanban.html`, `_task_card.html`, `_table.html` (partly in
> B1), `_list_panel.html` (partly in B1), `_build_kanban_columns`
> (`views.py:5971-6068`), `ProjectDetailView._kanban_columns_ctx`
> (`views.py:1632-1656`), DnD + lazy + cross-view-freshness in
> `acta.js:707-900`, `applyClientFilters` + `recomputeKanbanSubstatus`
> + `rowMatches` (`acta.js:411-704, 1371-1456`), `task_filter_attrs`
> templatetag (`web_extras.py:103-175`), TODOs `kanban_substatus_recompute`
> and `kanban_filter_grouping_bugs`.

---

## 1. Quick verdict

**Two of the three known TODOs are functionally addressed.** The
"substatus stale after filter" TODO is **implemented** in
`acta.js:1371-1456` (recomputes overdue / done-this-week / avatar
stack from visible cards on every filter pass) — close it. The
"assignee filter shrinks card gap" TODO is likely fixed via the
`[hidden]` + `display:none` belt-and-suspenders pattern, but needs a
30-second browser check to be sure. "Axis carries over to kanban" is
benign: kanban templates never read `axis=`, so the URL param is dead
weight but visually harmless.

**Real perf concerns are different from what the TODOs flagged.** The
aging-WIP bar (`task.age_days`) silently never shows on All Tasks
because `status_since` is annotated only in `ProjectDetailView`, not
in `_user_task_qs`. `applyClientFilters` walks the entire
`[data-task-id]` set on every change (~192 × number-of-loaded-panels).
`task.labels.all` is evaluated 4× per kanban card and 4× per list row
(same flavour as B1 F1).

---

## 2. Known TODOs — current state

### 2.1 `project_todo_kanban_substatus_recompute` — **DONE (close it)**

The TODO (memory 10 days old) described the gap: overdue count +
avatar stack on the column header stay at server-rendered values when
`applyClientFilters` hides cards. The TODO's 4-step recipe:

| TODO step | Current state |
|---|---|
| 1. Add `data-overdue`, `data-assignee-{id,color,initial}` | ✓ `task_filter_attrs` templatetag emits all three (`web_extras.py:103-175`); avatar attrs on `_task_card.html:125-129, 134-137` |
| 2. After `applyClientFilters` walk `.kanban-column`, recompute overdue + avatars | ✓ `recomputeKanbanSubstatus()` in `acta.js:1371-1456`, called from `applyClientFilters:656` |
| 3. Patch the DOM (overdue span text + visibility, avatar stack innerHTML, hide empty row) | ✓ `acta.js:1408-1455` exactly |
| 4. "++ N this week" — server-side `data-done-this-week` or leave stale | ✓ `data-done-this-week` emitted (`web_extras.py:173`); recomputed in JS (`acta.js:1391, 1414, 1420-1423`) |
| 5. SSE peer events touch substatus | Partial: `recountKanbanColumns` runs on `acta:task-created`, but I didn't find a `recomputeKanbanSubstatus` call on `acta:task-changed` — verify in B3 / D1 |

**Action**: delete `memory/project_todo_kanban_substatus_recompute.md`
(per [[feedback-strike-done-todos]]) and remove the MEMORY.md line.
Re-verify the SSE path before deleting — see §6 fix candidate F2-SSE.

### 2.2 `project_todo_kanban_filter_grouping_bugs` — **likely already fixed; verify in browser**

**Bug #1 (card-to-card spacing shrinks under assignee filter)**:
trace of `applyClientFilters`:
- Card is `<div data-kanban-card>` directly inside `.kanban-column`
  (no `<li>` wrapper).
- `target = row.closest("li") || row.closest("tr") || row` resolves
  to the card itself (`acta.js:588`).
- Lines 601-602 set **both** `hidden` attribute **and** inline
  `style="display: none"`.
- Tailwind v3.4 `space-y-2` translates to
  `& > :not([hidden]) ~ :not([hidden])` (v3.3+ behaviour). Hidden
  cards are excluded from the spacing selector chain → the visible
  ones stack with the configured gap, no shrink.

**Verdict**: with the current double-hide, the bug shouldn't reproduce.
But the TODO is 11 days old; the fix may have shipped after it was
written. **Action**: 30-second browser repro (open kanban, pick a
single assignee, eyeball card spacing). If clean, delete the TODO.

**Bug #2 (`axis=` carries over to kanban)**:
- `_kanban.html` never reads `axis`; the kanban body uses status
  columns exclusively.
- `applyClientFilters` URL mirror **explicitly preserves** `axis`
  (acta.js:678 comment "preserve everything else (sort, view, axis)").
- So `?view=kanban&axis=X` in the URL is a dead param when on
  kanban — visually harmless but slightly noisy.
- **No bug.** The "renders something odd" complaint in the TODO is
  likely about something else (maybe the column-sort + tab-switch
  inconsistency described in §4.2 below). Park.

### 2.3 `project_todo_kanban_column_sort` — open, DISCUSS

Per-column sort button (vs project-wide). Not investigated in B2 — it's
a feature design choice, not a perf/code issue. Leaves the TODO open.

---

## 3. What works (good news)

### 3.1 Drag-and-drop is clean (`acta.js:712-751`)

```js
fetch(`/api/v1/tasks/${taskId}/`, {
  method: "PATCH", headers: {...},
  body: JSON.stringify({ status: newStatus }),
}).then((r) => { if (!r.ok) { rollback(); return; } recount(); })
  .catch(rollback);
```

Rollback on `!r.ok` and `.catch(rollback)` — both failure modes
covered. `Sortable.get(col)` keeps re-binds idempotent on every
`htmx:afterSettle`. Open-in-new-tab via middle/Ctrl-click is a
**delegated** handler on `document` (lines 764-765) — survives every
swap because the listener never gets re-mounted.

### 3.2 Card uses `<div>` not `<a>` with rationale (`_task_card.html:13-22`)

Comment captures the kostyl-prevention:

> a draggable anchor races the browser's native link-drag, so Sortable
> intermittently never registered the move

Middle/Ctrl-click open via `data-task-url` and the delegated handler.
Single-click via `open_task_modal_attrs` (HTMX). Excellent code
literacy — this is the kind of comment future-Vox will thank
present-Vox for.

### 3.3 Cross-view freshness on task creation (`acta.js:842-856`)

`acta:task-created` clears every non-active `[data-panel-slot]` and
re-fires `lazyLoadPanels()` so a user who tab-switches right after a
create doesn't sit on a spinner. The active slot is left alone (it
got its inline HX-Retarget insert). Smart.

### 3.4 WIP warnings hidden during client-side filter (`acta.js:653-655`)

> WIP cues are server-rendered against the FULL board, so a
> client-side filter makes them stale … hide while a filter is active

`[data-wip-warning]` is toggled by `applyClientFilters`. The comment
in `_kanban.html:129-135` explains the same. Two-sided contract
documented.

### 3.5 `_build_kanban_columns` is genuinely O(n) (`views.py:5971-6068`)

Single pass over the materialised task list, per-status accumulators
in `dict` buckets. No DB hits. Per-status WIP-mode branching handled
cleanly. Aging-WIP `t.age_days` computed in the same loop. Clean.

### 3.6 ProjectDetailView annotates `status_since` (`views.py:1879-1893`)

One correlated subquery, not an N+1. Properly documented.

### 3.7 Client-side `applyClientFilters` recomputes everything

Walks the DOM once after a filter change and patches:
- Per-row visibility (`hidden` attr + `display:none`).
- Filter count badges (collapsed + expanded).
- Per-section count badges (status/priority/project/label/date).
- Kanban column count + substatus row (overdue + avatars).
- Backlog section counts + empty-section hide.
- List section counts + empty-section hide.
- WIP warning visibility.
- URL mirror (`history.replaceState`, with `axis`/`sort`/`view`
  preserved).
- Cookies (`acta_show_archived`, `acta_show_backlog`).
- Timeline re-render hook (`window.__tlAfterFilter`).

That's the entire client-side filter contract in one function. Tight.

---

## 4. Real findings

### 4.1 Aging-WIP bar **silently broken** on All Tasks

`_task_card.html:39-42` renders the left-edge aging bar from
`task.age_days`, computed in `_build_kanban_columns:6028-6031` from a
`status_since` queryset annotation. **The annotation is added in
`ProjectDetailView` (`views.py:1892`), not in `AllTasksView`'s
`_user_task_qs`** (`views.py:366-406`).

Effect: open a kanban inside one project → aging bar visible. Switch
to All Tasks kanban (active workspace) → no aging bar ever. No error,
no warning, no log line — `getattr(t, "status_since", None)` silently
returns None for the AllTasksView path.

**This may be intentional** ("aging across many projects is noise")
but the code reads as a missed annotation, not as a documented choice.
Two possible fixes:

- **F1 (silent-fix)**: add `Subquery(last_status_change)` to
  `_user_task_qs`. One extra subquery on every page that uses the
  base. Estimate: <2 ms / +1 query.
- **F1-alt (document)**: explicitly comment in `_task_card.html` and
  `_build_kanban_columns` that aging requires the annotation and
  All Tasks deliberately skips it.

Vox decision: which one wins.

### 4.2 Table column-sort + tab-switch is inconsistent

When the user clicks a table column-sort header, the table re-orders
via the `HX-Target=task-table-root` short-circuit (`views.py:1678-1679`
and `views.py:485-486`). Just the table partial re-renders; kanban
and list bodies in the DOM keep their original `(status, -priority,
-updated_at)` order.

On a cold load with `?order=priority`, kanban re-derives a fresh
ordering (`views.py:1640-1644`):

```python
table_order_key = (self.request.GET.get("order") or "").strip().lstrip("-")
if table_order_key in SORTABLE_COLUMNS:
    kanban_tasks = list(view_base.order_by("status", "-priority", "-updated_at"))
else:
    kanban_tasks = table_tasks
```

So:
- Cold load with `?order=priority`: table sorted by priority,
  kanban shows status-grouped (re-derived). Consistent.
- Cold load no sort: both consistent.
- User clicks "Sort by priority" mid-session (HX-Retarget swap),
  then switches to kanban (Alpine `x-show`): **kanban shows the
  original server-rendered order, not the table's new one**. This
  is fine because kanban doesn't honour `?order=` anyway. Probably
  matches user expectation.
- Same flow + user does a hard reload: kanban re-derives, table
  sorted. **Same result.** OK.

**Verdict**: subtle but consistent. Document if not already (the
`?order=` comment is good, but a one-liner near the `_kanban.html`
header would help). Not a bug.

### 4.3 Extra query in `?panel=kanban` when user has a custom `?order=`

In `ProjectDetailView._kanban_columns_ctx` the `view_base.order_by(...)`
call re-executes the queryset against the DB. Without custom sort,
`kanban_tasks = table_tasks` is reused (no extra query). **With
custom sort + `?panel=kanban` fetch**, the queryset is materialised
twice: once for `table_tasks` (line 1906) and once for the kanban
sort (line 1642).

But wait — `?panel=kanban` is a lazy fetch that runs **alone**
(early-returns on line 1947 before `table_tasks` is needed for table
rendering). So actually:
- `?panel=kanban` with no `?order=`: `table_tasks` evaluated once
  (line 1906), kanban_tasks = table_tasks. **1 query.**
- `?panel=kanban` with `?order=priority`: `table_tasks` evaluated
  once (line 1906) + `view_base.order_by` evaluated once (line 1642).
  **2 queries** for the same task set in two different orders.

Fix: in `?panel=kanban` with custom sort, skip the `table_tasks`
materialisation and pass `view_base` directly. Small win (one query
saved on a not-very-hot path).

### 4.4 `task.labels.all` evaluated 4× per kanban card

Same pattern as B1 §3.1. `_task_card.html:86, 88, 94, 95`. For 192
cards × 4 = 768 in-memory passes over the prefetched labels list.
**Same fix as B1 F1** — wrap in `{% with labels=task.labels.all %}`.

The list view uses `_task_row.html` whose 4× pattern was already
flagged in B1.

### 4.5 `_task_card.html` per-row cost

146 LOC template + inline lucide SVGs (priority chevron, gauge,
iteration-cw, calendar, blocked badges via `_task_link_badges.html`).
Rough estimate: **1.0-1.5 KB per card** in source HTML × 192 = ~250 KB
for a kanban cold load. Smaller than the table (480 KB estimated in
B1) but still significant. Same fix as B1 F4 — `<svg><use>` references.

### 4.6 `applyClientFilters` walks every `[data-task-id]` in the DOM

`acta.js:578` — `document.querySelectorAll("[data-task-id]")`. On a
project page with all five panels loaded (table 192 + kanban 192 +
list 5×192 = 1344 + timeline 192 + backlog rows) the per-filter pass
walks well over 1 000 elements. Each runs `rowMatches` which does ~10
short circuits + a couple `Set.has`. Microseconds-each, but for ~2 000
elements × 14 filter conditions = 28 000 short evaluations on a single
keypress in the search box (debounced to 150 ms).

Profile in browser devtools before "fixing" this. Likely fine at the
current 192-task scale. **Threshold to revisit: active set > 3 000
tasks** (matches `project_todo_virtualize_or_jinja2`).

### 4.7 Multiple `[data-task-id]` for the same task across panels

The same task renders once in each loaded panel. So one task →
one `<tr data-task-id="42">` in table + one `<div data-task-id="42">`
in kanban + 5 `<a data-task-id="42">` in list axes. Total 7 marker
elements per task. `applyClientFilters` walks them all and runs
`rowMatches` on each — fine for correctness (each hides on its own
container), but duplicate work.

Possible optimisation: tag each panel with a marker, and in
`applyClientFilters` walk **only the active panel's** rows. The other
panels are display:none anyway (Alpine `x-show`), and they get
re-filtered when the user switches to them via the
`acta:list-insert-row` / panel-fetch path. Real win: ~6× less work
on filter changes. **F4 candidate, medium risk** (need to make sure
no stale state lingers).

### 4.8 `_kanban.html` collapsed-column DnD target detail (`_kanban.html:23`)

Comment says:

> sortable.js keeps the column as a drop target so users can drop a
> card into a folded Done

Good UX. But the body is `x-show`-hidden when collapsed (line 190).
Sortable drop targets that are `display:none` are off the hit-test in
some Sortable.js versions — worth one runtime check.

### 4.9 `recountKanbanColumns` counts **visible** cards (`acta.js:824-830`)

Counter excludes `[hidden]` rows via `[data-task-id]:not([hidden])`
on the column. Matches the `applyClientFilters` count math (line 626).
On `acta:task-created` the new card has no `[hidden]` attr (just got
inserted), so the count picks it up. Good.

### 4.10 `recomputeKanbanSubstatus` builds avatar `<img>`/`<span>` from `data-avatar-*` attrs

Walks visible cards, reads `data-avatar-url`, `data-avatar-bg`,
`data-avatar-initial`, `data-avatar-name`. The `data-avatar-*` attrs
are emitted by `_task_card.html:125-129, 134-137` but **NOT on the
list-view `_task_row.html`** (B1 read, lines 119-145). So if the user
is on the list panel with assignees visible, the substatus recompute
function doesn't read those rows (it scopes by `.kanban-column`),
which is correct — it only touches the kanban panel.

No bug, but documenting the scope tightens reasoning.

### 4.11 Avatar URL re-fetched on every recompute

`recomputeKanbanSubstatus:1432-1434` creates a fresh `<img>` with
`src=src.url` on every filter pass. Browsers cache the response, so
this is one HEAD-or-cache hit per avatar per recompute — fine in
practice but **could be** zero with cached `<img>` element reuse.
Not a fix candidate; just an observation.

---

## 5. Subtle issues to verify in dev

| # | Issue | How to verify |
|---|---|---|
| 5.1 | Card spacing under assignee filter (TODO #1) | Open `/projects/<slug>/board/`, filter to one assignee, eyeball card gap |
| 5.2 | Aging-WIP bar missing on All Tasks kanban | Open `/tasks/?view=kanban`, look for left-edge bars on cards >3 days in column |
| 5.3 | Sortable drop target on collapsed Done column | Drag a card into the folded Done column; verify status update |
| 5.4 | Substatus refresh on SSE `acta:task-changed` | Two browsers, both on kanban; B changes a card's assignee; verify A's column avatar stack updates |
| 5.5 | `?panel=kanban` query count with `?order=priority` | `CaptureQueriesContext` on the panel endpoint with order set; expect 2 or 1 |
| 5.6 | `applyClientFilters` walks all panels on filter change | Devtools profile on a heavy `/tasks/` with all panels loaded |

Park until dev is up.

---

## 6. Fix candidates (input to Chunk G)

| # | Tag | Title | Notes |
|---|---|---|---|
| F1 | `bug/silent` `[3/1/1]` | Decide aging-WIP on All Tasks: annotate `status_since` in `_user_task_qs` OR document the skip | One-line code change + doc; Vox-decision |
| F2 | `perf/template` `[3/1/1]` | `{% with labels=task.labels.all %}` in `_task_card.html` (cross-applies B1 F1) | Trivial; bundle with B1 F1 |
| F2-SSE | `bug/sse` `[2/1/1]` | Wire `recomputeKanbanSubstatus` on `acta:task-changed` | Verify first whether it's already wired; ~5 lines |
| F3 | `clean/todo` `[1/1/1]` | Delete `project_todo_kanban_substatus_recompute` (DONE, verified §2.1) | Memory cleanup |
| F4 | `perf/js` `[3/3/3]` | Scope `applyClientFilters` to the active panel only | Threshold-driven; safe but needs careful state hygiene |
| F5 | `perf/server` `[2/2/2]` | Skip `table_tasks` materialisation on `?panel=kanban` with custom `?order=` | One query saved on a cold path |
| F6 | `clean/code` `[1/1/1]` | One-line comment near `_kanban.html` header about `?axis=` being a no-op | Doc cleanup |
| F7 | `perf/payload` `[3/2/2]` | Lucide `<symbol>`+`<use>` factor for repeated icons (cross-applies B1 F4) | Defer to D3 |
| F8 | `tests/regress` `[3/1/1]` | `assertNumQueries` on `?panel=kanban` + DnD PATCH path | Bundle with B1 F3 |
| F9 | `clean/uat` `[1/1/1]` | 30-second browser repro of TODO #1 card spacing; close TODO if clean | UAT, not code |
| F10 | `bug/uat` `[2/1/1]` | UAT collapsed-Done DnD target (§5.3) | UAT |

---

## 7. Inputs to other Wave 1 chunks

- **B3 (task detail)**: §4.1 aging bar question — task detail page
  also reads `task.age_days`? probably not, but check.
- **B4 (dashboard)**: dashboard tiles don't read `status_since`;
  not affected.
- **D1 (acta.js)**: `applyClientFilters` is 290 LOC + `rowMatches`
  82 LOC + `recomputeKanbanSubstatus` 86 LOC. D1 should look at the
  whole client-side filter mirror in one sitting (it's parity with
  `apply_task_filters` in Python — drift risk).
- **F (infra)**: §4.6 walk cost threshold sets a measurement target.

---

## 8. Status

- Chunk B2: **complete**.
- No code changed.
- 2 of 3 known TODOs functionally addressed (substatus DONE, axis
  carryover benign); 1 needs a 30-second browser repro (spacing).
- Found 1 silent bug (aging-WIP missing on All Tasks).
- 10 fix candidates added to G's input set.
- Next chunk: B3 (task detail — modal + page).
