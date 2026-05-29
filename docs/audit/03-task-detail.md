# 03 — Task detail (modal + page)

> **Wave 1 / Chunk B3** — `/projects/<slug>/<n>/` (full page) and
> `/projects/<slug>/<n>/?modal=1` (modal shell + body).
> Date: 2026-05-29. Read-only. **No code changed.**
> Sources: `TaskDetailView` (`views.py:2026-2099`), task-detail
> templates (7 files, 514 LOC), `_decorate_comments`/`_task_comments`
> (`views.py:2597-2647`), `_task_activity`/`_enrich_activity_events`
> (`views.py:2315-2454`), 9 inline cells in
> `templates/web/projects/_*_cell.html`, fragment endpoints
> (`views.py:2103-2310`), TODO `inline_cells_propagation`.

---

## 1. Quick verdict

**Task detail is the most N+1-safe surface in the audit so far.** The
core read path is clean (subtasks + comments with `prefetch_related`
through replies/authors/attachments + activity events with two
batched enrich queries + the meta-rail factories). Two avoidable
extra queries hide in `get_context_data`: a `values_list("id")` that
bypasses the prefetch cache, and the membership re-check in
`_get_user_task_or_404` on each fragment endpoint.

**The TODO `inline_cells_propagation` is confirmed open.** Only
`_status_cell.html` and `_task_context_menu.html` carry the
`actaForceApplySelfEvent` opt-in; the eight other inline cells
(priority / assignee / due / end / start / cycle / project / size)
silently leave the underlying table/kanban row stale after a modal
edit until a hard reload. This is the bug Vox happened to see on
status; the same exact bug exists for the other eight cells.

**Modal + page parity is a drift risk.** The view returns one of two
templates based on `?modal=1`; the two templates layer different
inner shells (`_task_detail_body.html` 136 LOC vs
`_task_detail_modal_body.html` 89 LOC). They diverge on what they
include, which is fine until someone edits one and forgets the
other — easy regression vector once Wave 2 starts refactoring.

---

## 2. What works (good news)

### 2.1 `get_object` adds the right joins (`views.py:2045-2063`)

`_user_task_qs` is reused as the base, then extended:

```python
return get_object_or_404(
    _user_task_qs(self.request.user).select_related("reporter", "parent")
    .prefetch_related("blocked_by__project", "blocks__project", "related__project"),
    project__slug_prefix=self.kwargs["slug_prefix"],
    number=self.kwargs["number"],
)
```

Three M2M relations prefetched **with the project join** because
each link-chip template renders `linked.slug` which needs
`linked.project.slug_prefix`. Comment in code explains the why.

### 2.2 `_task_comments` prefetches the full reply tree (`views.py:2624-2647`)

```python
task.comments.filter(parent__isnull=True)
    .select_related("author")
    .prefetch_related("replies__author", "attachments", "replies__attachments")
    .order_by("created_at")
```

The baseline-agent suspicion (`for reply in comment.replies.all()` →
N+1 in `_decorate_comments`) **is a false positive**. `replies` is
prefetched, so `.all()` reads from the prefetch cache; nothing hits
the DB. `replies__author` adds the author join in the same prefetch
batch. Reactions for every decorated row (top-level + replies) go
through one batched query in `attach_reactions`.

### 2.3 `_decorate_comments` mutates in-place but documented

The function attaches `task`, `can_modify`, `reaction_summary` to
every comment without a fresh query. The "mutate in place" pattern
would be a smell in random code; here it's the right call because
the comments live in a per-request list that's serialised out via
the template immediately. The docstring captures it.

### 2.4 `_task_activity` keeps the feed N+1-free with payload-scoping (`views.py:2315-2380`)

One query for events (`select_related("actor")` for the actor
avatar) plus two batched enrich queries in
`_enrich_activity_events` (user names for `task.assigned`, label
names for `task.labels_changed`). The payload-scoping
(`payload__task_id=task.id` for comment/attachment events) means
"event remains visible even after the underlying row is deleted" —
a nice ADR-0011 alignment.

### 2.5 SSE fragment endpoints are tight (`views.py:2103-2310`)

- `task_title_fragment` — `_user_task_qs + select_related("parent")` →
  1 query.
- `task_topbar_title_fragment` — `_get_user_task_or_404` → 1 query.
- `task_description_fragment` — `_get_user_task_or_404` → 1 query.
- `task_comments_fragment` — `_get_user_task_or_404 + _task_comments`
  → ~4 queries.
- `task_meta_fragment` — base + reporter + 5 workspace-scoped helpers
  → ~7 queries (each helper is one query each, no N+1).
- `task_timeline_fragment` — same as `task_meta_fragment` minus the
  factories.

Each fragment is a fresh round-trip but reads only the slice it
renders. ADR 0015 (real-time) intent honoured.

### 2.6 `_status_cell.html` pattern is solid (`templates/web/projects/_status_cell.html:44-94`)

`x-teleport="body"` lifts the dropdown out of any clipping ancestor
([[feedback-overflow-kills-popovers]]). `x-init` re-runs
`htmx.process($el)` on the teleported subtree so its `hx-post`
forms get HTMX-bound. `@htmx:before-request` flips
`actaForceApplySelfEvent({{ task.id }})` so the SSE self-event
isn't filtered out, and the surrounding table/kanban/list refresh
without a reload. `@click.outside="open=false"` for dismissal. The
`onScroll` listener auto-closes the dropdown on scroll, detaches
when `$el.isConnected` flips false. This is the reference pattern
the other 8 cells should mirror.

---

## 3. Real findings

### 3.1 `task.labels.values_list("id", flat=True)` bypasses the prefetch cache

`views.py:2098`:

```python
ctx["attached_label_ids"] = set(task.labels.values_list("id", flat=True))
```

The base queryset prefetches `labels`. `.values_list("id",
flat=True)` issues a **fresh** SQL query, bypassing the prefetch
cache. This is a documented Django behaviour: `values_list` doesn't
use the prefetched objects.

**Fix**: `set(l.id for l in task.labels.all())` reads from the
prefetch cache → 0 extra queries. Same pattern likely appears in
`task_meta_fragment` (`views.py:2194` — verified, same line).

**Savings**: 1 query × every task detail load + every meta fragment
fetch + every comment fragment fetch (when these endpoints prefetch
labels). Counted across SSE refresh hotspots, this adds up.

### 3.2 Inline cells without `actaForceApplySelfEvent` (TODO confirmed open)

Grep result for `actaForceApplySelfEvent` across the cell templates:

```
templates/web/projects/_status_cell.html       ← present
templates/web/projects/_task_context_menu.html ← present
```

Missing from:
- `_priority_cell.html`
- `_assignee_cell.html`
- `_due_date_cell.html`
- `_end_date_cell.html`
- `_start_date_cell.html`
- `_cycle_cell.html`
- `_project_cell.html`
- `_size_cell.html`

Every one of these is a `<form hx-post=...>` that mutates the task
and re-renders **its own cell** on success. The mutation also
broadcasts an SSE diff event (`emit_task_diff_events` per cell), but
the **same-tab actor's own SSE message is suppressed** (anti
double-render against the direct HTTP swap of the cell). Result:
the cell updates, but the row/card in the background list/kanban
stays stale until a hard reload.

This matches what Vox already wrote in the TODO. **Action**:

- **Option A (sweep)**: clone the opt-in into all 8 cells in one PR,
  with a regression test per cell in `test_inline_edits.py`.
- **Option B (per-bite)**: wait until each cell bites in production.

**The TODO is the user's call. The audit's input**: the opt-in is a
3-line change per cell (one `@htmx:before-request` on the dropdown
panel + a comment block). Sweep cost is small; the test file is
already 1 508 LOC and would absorb 8 small additions cleanly. **B3
votes for sweep** — the inconsistency is a worse smell than the
work to fix it.

### 3.3 `_get_user_task_or_404` re-runs the membership check on every fragment endpoint

Each fragment view calls `_get_user_task_or_404` which builds
`Task.objects.filter(project__workspace__memberships__user=user)`
fresh per call. Postgres handles it (the membership table is small,
the join is cheap) but a chatty page that fires 5 SSE-triggered
fragment refreshes burns 5 membership joins per second-ish during
heavy peer activity.

**This is the same concern flagged in B1 §4.4** (`_user_task_qs`
membership join cost). Not a regression; just visible at the
fragment-fetch layer. Defer to F (infra) for EXPLAIN.

### 3.4 Modal + page templates duplicate inclusion logic

`task_detail.html` (19 LOC, page wrapper) → `_task_detail_body.html`
(136 LOC).
`task_detail_modal.html` (70 LOC, modal shell + topbar) →
`_task_detail_modal_body.html` (89 LOC).

The two bodies render the same task with slightly different layout
chrome:
- Page body: full rail, comment threads as cards, activity feed as a
  separate panel.
- Modal body: condensed metadata, merged timeline (comments +
  activity interleaved by `created_at`), no separate activity panel.

The merged timeline (`ctx["timeline"]` from `_sort_timeline`) is
computed unconditionally in `get_context_data:2089-2090` — so the
page wastes the work building the merged timeline it doesn't render.
Negligible CPU (192 events × a sort), but a small example of how
modal/page divergence costs unmeasured.

### 3.5 `_task_detail_body.html` is 136 LOC, `_task_detail_modal_body.html` 89 LOC

Combined with `_task_meta.html` (133 LOC) and
`_task_detail_topbar.html` (67 LOC), the detail page renders ~330
LOC of HTML before the row/comment partials. Each `<form hx-post>`
cell template (~50-100 LOC each × 9 cells) compounds. **Total HTML
for a task detail render: ~1 200-1 800 LOC of template output**.

Not a payload problem at current scale (rendered HTML is much
smaller than All Tasks's 1 700 LOC). Just a maintenance map.

### 3.6 `summarize_reactions` is called twice — once on task, once via `_decorate_comments`

`get_context_data:2083-2087` calls `summarize_reactions(target_field="task", ids=[task.id], user_id=user_id)`
for the single task itself (the task gets a reaction summary just
like comments). Then `_decorate_comments` calls
`attach_reactions(objs=decorated, target_field="comment", user_id=user_id)`
(`views.py:2621`) for the comments.

That's two distinct calls — one for the task, one for the comments.
Different `target_field` so they can't merge. **No issue**; just
confirming the two calls are intentional.

### 3.7 `summarize_reactions` query is a single batched call

Read of `summarize_reactions` is deferred to **C5 (comments)** /
**C6 (activity)** in Wave 2 — this audit confirms the call shape
(`ids=[…]` + `user_id`) but doesn't verify the SQL. Add to C5/C6
input set: confirm one query, not N.

### 3.8 TipTap mount/remount on nav swap — fixed but verify

Comment in [[memory]] (`14:00` line) says:
> Fixed #1 (TipTap announcement no reinit on nav-router): mountAll
> description_editor scans full document; bundle rebuilt; tested

So the editor mount is **document-wide** rather than scoped to a
swap target. Worth one runtime check on the task detail page:
- Open a task with a non-empty description.
- Switch to another task via the modal/page.
- Verify the editor mounts cleanly with no stale Alpine warning in
  the console.

Add to §4 verify-list.

### 3.9 `_workspace_members`, `_workspace_labels`, `_workspace_label_groups`, `_workspace_projects`, `_workspace_cycles` — verify each is one query

Each helper is read independently by `get_context_data:2093-2097`
and by `task_meta_fragment:2189-2194`. They are likely single-query
each (label-groups joins labels, projects joins workspaces) but
not directly read in this audit. Defer to **C3 (workspaces)** for
the deep dive; flag here so it's not forgotten.

If any of these helpers re-runs on every fragment endpoint and
they're all 1-query each → 5 extra queries per `task_meta_fragment`
fetch. SSE triggers `task_meta_fragment` whenever a peer changes
status/priority/assignee/due/labels/size on this task — so a
heavily-edited task in a busy workspace fans this out frequently.

Could be reduced with a single helper that returns a single dict
in one query (UNION ALL or a 5-key dict from a single `.values()`
pass). But before optimising, **measure** — Vox's "feels slow" may
or may not point here.

---

## 4. Subtle issues to verify in dev

| # | Issue | How to verify |
|---|---|---|
| 4.1 | Inline cell mutation → row stale (8 cells) | Open kanban; open a card modal; change priority; close modal; row still shows old priority |
| 4.2 | TipTap mounts cleanly on task switch | Open task A modal, type, close. Open task B modal. No console warning, editor mounts cleanly |
| 4.3 | `attached_label_ids` query count | `CaptureQueriesContext` around `TaskDetailView.get_context_data`; current ≥ 14 with the extra `values_list` query |
| 4.4 | `task_meta_fragment` query count under SSE storm | Two browsers, B repeatedly changes status; A's meta fragment refreshes — count queries per refresh |
| 4.5 | Aging WIP card bar — task detail | `_task_card.html` reads `task.age_days`. Task detail page may also; check whether status_since is in scope here |
| 4.6 | Modal close path on stale data | Open modal, change priority via cell (now broken per §3.2), close modal, refresh page — does the row resync from server? |

Park until dev is up.

---

## 5. Fix candidates (input to Chunk G)

| # | Tag | Title | Notes |
|---|---|---|---|
| F1 | `perf/query` `[3/1/1]` | Replace `task.labels.values_list("id", flat=True)` with `{l.id for l in task.labels.all()}` in `get_context_data` AND `task_meta_fragment` | 2 lines; verifiable with `assertNumQueries` |
| F2 | `bug/sweep` `[4/2/2]` | Add `actaForceApplySelfEvent` opt-in to 8 inline cells (priority/assignee/due/end/start/cycle/project/size) + per-cell test in `test_inline_edits.py` | Vox is undecided sweep-vs-per-bite; B3 recommends sweep. Closes the TODO |
| F3 | `tests/regress` `[3/1/1]` | `assertNumQueries` on `TaskDetailView` + each fragment endpoint (`task_meta`, `task_timeline`, `task_comments`, `task_title`, `task_description`) | Bundle with B1 F3 + B2 F8 |
| F4 | `perf/server` `[3/3/2]` | Skip `_sort_timeline` build when not modal mode | Cheap to do, saves a CPU pass for full-page renders |
| F5 | `perf/server` `[3/3/3]` | Merge `_workspace_members/labels/groups/projects/cycles` helpers into a single combined call | Verify cost first (measurement in §4.4) — only optimise if it shows |
| F6 | `clean/code` `[2/1/1]` | Hoist the `_status_cell` Alpine `x-data` state machine into a shared `Alpine.data("inlineCellDropdown", …)` block | Prep work for F2's sweep; less per-cell boilerplate |
| F7 | `clean/template` `[1/1/1]` | One-line comment in `_task_detail_body.html` + `_task_detail_modal_body.html` noting the modal/page divergence so future-edits update both | Doc cleanup |
| F8 | `bug/uat` `[2/1/1]` | UAT TipTap mount on task switch (§4.2) — close TODO if clean | UAT |

---

## 6. Inputs to other Wave 1 chunks

- **B4 (dashboard)**: not impacted directly; dashboard reads
  aggregated data, not task-detail surfaces.
- **D1 (acta.js)**: `actaForceApplySelfEvent` lives in `acta.js` —
  D1 should read the SSE self-event filtering logic and confirm
  the opt-in mechanism matches the comment in `_status_cell.html`.

## 7. Inputs to Wave 2 (placeholder)

- **C5 (comments)**: verify `summarize_reactions` + `attach_reactions`
  are single batched queries; verify polymorphic Comment FK handling;
  check for [[project-todo-comment-status-change]] groundwork
  (comment-with-status-flip).
- **C6 (activity)**: verify `_task_activity` event-type filter still
  matches the events `log_event` emits today (drift risk after
  refactors); the `task.labels_changed` exclusion makes the timeline
  blind to label history — intentional, but worth surfacing in
  [[project-todo-full-activity-history]].
- **C3 (workspaces)**: `_workspace_*` helpers reused on detail and
  meta fragments — audit query count and consider memoisation.

---

## 8. Status

- Chunk B3: **complete**.
- No code changed.
- Confirmed open: `project_todo_inline_cells_propagation` (8 cells
  missing the opt-in). Voted sweep.
- Confirmed false positive (from baseline §8): `_decorate_comments`
  is N+1-safe.
- Found 1 silent extra query (`.values_list("id", flat=True)`
  bypassing prefetch).
- 8 fix candidates added to G's input set.
- Next chunk: B4 (dashboard + project insights).
