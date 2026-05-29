# 01 — All Tasks audit

> **Wave 1 / Chunk B1** — `/tasks/` workspace-wide index.
> Date: 2026-05-29. Read-only. **No code changed.**
> Sources: static read of `apps/web/views.py:462-714`, `_user_task_qs`
> at `views.py:366`, `apps/web/filters.py`, `templates/web/all_tasks.html`,
> `_all_tasks_inner.html`, `_view_panel.html`, `_table.html`,
> `_table_row.html`, `_task_row.html`, `_list_panel.html`, lazy-panel
> loader in `static/js/acta.js:147-221`. Historical reference:
> [[project-todo-all-tasks-lazy-panels]] (profiled 2026-05-17:
> ~400 ms / 1.7 MB on 192 tasks).

---

## 1. Quick verdict

**The 1.7 MB / 400 ms problem from 2026-05-17 is partially solved
(lazy panels work as designed) but the cost-per-row is still wasteful
on the active panel.**

Lazy-panel mechanics are correctly wired across view + template + JS,
so kanban / list / timeline / backlog no longer ship on the cold load.
The active panel (default `table`) still produces ~5800 LOC of inline
Alpine + 6 redundant `task.labels.all` evaluations per row, and the
filter-sidebar context builder runs unconditionally on every render.
None of this is N+1; all of it is template waste that compounds with
row count.

---

## 2. What works (good news)

### 2.1 Lazy-panel mechanism, end-to-end ✓

The original TODO's "Option 1: lazy-render alternate view bodies after
first paint" is **live**. All three layers cooperate:

**View layer** — `apps/web/views.py:462`:
- `get_context_data` sets `ctx["lazy_view_panels"] = True` (line 636).
- `get_template_names` short-circuits to a single panel partial when
  `?panel=table|kanban|list|timeline|backlog` is present (lines 485-496).
- Per-panel context builders run only for the requested panel:
  - `?panel=timeline` returns after `_timeline_context` (lines 665-666).
  - `?panel=backlog` calls `_backlog_context` (lines 669-671).
  - `?panel=kanban` calls `_kanban_columns_ctx` (lines 679-681).
  - `?panel=list` calls `_list_axes_ctx` (lines 684-686).
  - Note: the **filter-sidebar context is skipped** on `?panel=` fetches
    (it's after the early returns) — good, that's ~234 LOC of work avoided.

**Template layer** — `templates/web/projects/_view_panel.html:28-66`:
- Each `<div x-show="$store.viewMode.current === 'X'" data-panel-slot="X">`
  only includes its body when `view_mode == "X"` under `lazy_view_panels`.
- Empty slots get `data-panel-slot="X"` for the JS loader.

**JS layer** — `static/js/acta.js:147-221`:
- `lazyLoadPanels(basePath)` walks `[data-panel-slot]`, fires one
  `htmx.ajax` per empty slot with `?panel=<key>`.
- Two guards: `slot.children.length > 0` (already filled) and
  `slot.dataset.panelLoading === "true"` (request in flight).
- URL-mismatch guard prevents loading a stale page's slots into the
  current page (line 164).
- Retriggered on `htmx:afterSettle` (line 212) — so filter-form
  swaps that rebuild the inner fragment re-populate slots.
- Failure path resets `panelLoading = "false"` (line 198), so a
  tab switch can retry a missed slot.

**This is a well-engineered piece of code.** The only soft spot is the
fixed 50 ms `setTimeout` before kicking off the fetches (line 215, 218,
220) — see findings §4.6.

### 2.2 Base queryset is N+1-safe ✓

`_user_task_qs` (`views.py:366-406`) eager-loads exactly what table /
kanban / list cells touch:

```python
return (
    Task.objects.filter(project__workspace__memberships__user=user)
    .select_related("project__workspace", "assignee", "cycle")
    .prefetch_related(
        Prefetch("labels", queryset=Label.objects.select_related("group")),
        "blocks",
        "blocked_by",
    )
)
```

Deliberately omits `reporter` and `parent` because they only appear in
the task-detail rail (the detail page adds them via its own
`select_related` after this base queryset). The `Prefetch("labels",
queryset=Label.objects.select_related("group"))` is exactly the
[[feedback-no-n-plus-one]] recipe: the labels chip-trigger groups by
`label.group`, so without the join each chip would re-query the
group FK.

**Cell access patterns audited row-by-row** for `_table_row.html` and
`_task_row.html`:
- `task.project.slug_prefix`, `task.project.name`, `task.project.icon`,
  `task.project.workspace.name` → `project__workspace` select_related ✓
- `task.assignee.display_name`, `.avatar`, `.avatar_color`,
  `.avatar_version` → `assignee` select_related ✓
- `task.cycle.id`, `.number`, `.display_name` → `cycle` select_related ✓
- `task.labels.all` (every label's `name`, `color`, `id`) →
  prefetched ✓
- `task.assignee == request.user` → in-memory compare on prefetched ✓

**No N+1 in the table/list/kanban render path on All Tasks.**
`assertNumQueries` would lock this in (see §5).

### 2.3 Export endpoint is N+1-safe ✓

`export_all_tasks_json` at `views.py:6770`:
```python
qs = _user_task_qs(request.user).select_related(
    "reporter", "parent__project"
).filter(project__workspace=active)
```

Adds the two joins the lean table queryset omits. Confirmed not the
"labels in bulk export" suspicion from baseline §8 (#2).

### 2.4 The baseline N+1 suspicion at `views.py:3440` is a **false positive**

The baseline backend agent flagged `for label in t.labels.all()` at
`views.py:3440` as a "Medium risk M2M without prefetch in bulk export".
Reading the surrounding code (lines 3412-3443) shows this is the
**search hover-card endpoint** that returns a single task by id, not
a bulk export. One task means no N+1 dimension. Drop this from the
suspicion list.

---

## 3. What's slow / wasteful (real findings)

### 3.1 `task.labels.all` evaluated 5-6× per row

`_table_row.html` calls `task.labels.all` six separate times for the
same row:

| Line | Expression |
|---:|---|
| 99 | `{% if task.labels.all %}` |
| 130 | aria-label `{% for label in task.labels.all %}` |
| 133 | `{% for label in task.labels.all\|slice:":3" %}` |
| 138 | `{% if task.labels.all\|length > 3 %}` |
| 139 | `{{ task.labels.all\|length\|add:"-3" }}` |
| 146 | popover `{% for label in task.labels.all %}` |

`_task_row.html` does the same pattern 4× (lines 88, 90, 100, 101).

**Cost**: with prefetch, each `.all()` returns the cached
`_prefetched_objects_cache["labels"]` list — no SQL. But each
`|length`, `|slice`, `{% for %}` still iterates the cached list in
Python. For 192 rows × 6 evaluations = **1 152 extra in-memory passes
over `labels`** on the table render alone. List view (when fetched)
re-renders the same row × 5 axes = **5 760 more**.

**Fix**: wrap once in `{% with labels=task.labels.all %}`. Or expose
two cached_property helpers on `Task`: `label_chip_preview` (first 3)
and `label_chip_overflow` (count - 3 or 0). Template becomes
`{% if task.label_chip_preview %}…{{ task.label_chip_overflow }}`.

### 3.2 Inline Alpine `x-data` of 19 LOC **per row** in `_table_row.html`

`_table_row.html:107-125` defines a per-row Alpine component for the
labels popover:

```html
<div x-data="{
       open: false,
       coords: { top: 0, left: 0, placement: 'above' },
       show() { ... 9 LOC ... },
       hide() { this.open = false; }
     }" class="inline-flex">
```

That's **~700 bytes of Alpine source per row** in raw HTML. For 192
rows = ~135 KB of inline JS just for label tooltips. Multiplied by the
list view's 5 axes (when fetched) → another ~675 KB.

**Fix**: hoist the popover state machine to a single document-level
Alpine component or `Alpine.data("labelCluster", () => {…})` definition
registered once in `acta.js`. Each row binds `x-data="labelCluster"`
or simply `x-data="{ open: false }"` with method calls dispatching to a
shared helper. Saves ~600 bytes per row × number of rendered rows.

Same pattern in `_task_row.html` for label pills (lines 88-103) — but
no per-row state machine there, just inline `{% for %}` over labels.
Lighter, but still benefits from §3.1.

### 3.3 The `_table_row.html` row template is 197 LOC

Combined with the inline `x-data` block above and ~10 `data-sort-*` /
`data-*` attributes per row, **a single rendered `<tr>` is on the order
of 2-3 KB of source HTML**. 192 rows × 2.5 KB ≈ **480 KB just for the
active table body**, which is the dominant chunk of the first-paint
payload after lazy panels removed kanban + list.

**Fix candidates (ordered by yield)**:
1. §3.1 + §3.2 above → ~150-200 KB shaved (estimated).
2. Drop `data-sort-*` attributes for columns the user can't sort on
   in JS — currently every row carries 10 sort attrs whether or not
   the client-side sort handler uses them all.
3. Move `lucide` inline SVGs to `<use href="#icon-X">` references with
   one `<symbol>` block at the top of `<body>`. Each lucide icon is
   ~200-400 bytes inline; the page has 6-8 icons per row + 3-4 in the
   header. Significant but not trivial to refactor — needs a
   templatetag tweak.

### 3.4 `filter_sidebar_context` runs on every All Tasks render (234 LOC)

`apps/web/filters.py:480-714` builds the entire sidebar state every
time `AllTasksView.get_context_data` finishes (line 703-713) — counts
per status, per priority, per project, per label, per cycle (when
enabled). For each dimension it issues an aggregation query against
the filtered queryset.

This is correctly **skipped on `?panel=` fetches** (early-returns
above), so filter-toggle round-trips don't pay it. But a cold load
and any inner-fragment filter-form swap does.

**Action**: Chunk B1 doesn't fix this; flag for **C9 (web)** to
measure exact query count + time, decide whether to memoize counts
per workspace+filter-state for short TTLs (1-5 s) or refactor.

### 3.5 `apply_task_filters` chains 12 helpers

`filters.py:20-55` — every All Tasks request runs:

```
_filter_archived → _filter_status → _filter_backlog →
_filter_int_field(priority) → _filter_int_field(size) →
_filter_int_field(project_id) → _filter_assignee → _filter_labels →
_filter_cycle → _filter_due → _filter_meta → _filter_date_range →
_filter_search
```

Each appends to the SQL `WHERE`. Postgres handles it fine; the worry
is `_filter_search` (`filters.py:358`) — if it does an `ILIKE %q%` on
title/description, it's O(N) per row without a trigram or full-text
index. **Verify in F (infra)** with an EXPLAIN — currently flagged.

### 3.6 50 ms fixed `setTimeout` before lazy-panel fetches

`acta.js:215, 218, 220` — the lazy-panel loader sleeps 50 ms before
firing. Comment doesn't explain why. Effects:

- On cold load, the alternate panels start fetching 50 ms after the
  page is interactive. Not visible to the user.
- On filter swap, **the panels rebuild 50 ms after the inner-fragment
  swap settles** — if the user switches to a panel during that 50 ms
  window, the JS sees `slot.children.length === 0` and starts a fetch.
  Should be fine (no double-fetch), but worth instrumenting.

**Action**: experiment with `requestIdleCallback(loader, {timeout: 200})`
on browsers that support it, fall back to `setTimeout(loader, 50)`.
Sometimes saves a frame; rarely costs anything. Defer to a fix-PR.

### 3.7 `_assignee_strip.html` is 184 LOC and always renders

The assignee strip renders on every All Tasks load (`all_tasks.html:29`).
184 LOC is not catastrophic but it's a heavy partial that's worth
measuring once. Adding to B1's "verify later" list — leave for now.

### 3.8 `_task_row.html` lucide-icon pattern repeats per row

Each row contains 4-6 lucide icons (status dot is not lucide, but
priority chevron, gauge, iteration-cw, arrow-right, label dots).
Lucide-static SVGs are inlined by the `{% lucide %}` templatetag.
Same fix as §3.3 (3) — `<use>` references with one `<symbol>` block.

### 3.9 Cookie write skipped on `?panel=` (intentional, well-documented)

`render_to_response` lines 544-565 — three preference cookies
(`acta_view_mode`, `acta_list_axis`, `acta_show_backlog`) and the
archive cookie are persisted **only when `?panel=` is absent**.

The reasoning is excellent and worth quoting in the audit because
it's the kind of subtle race that bites once and stays bitten:

> the view would bounce — a lazy tab switch fires the panel fetch
> while the URL still carries the previous `?view=` (pushState runs
> after), so writing it resets the cookie and `syncFromCookie` yanks
> the user back

This is a kostyl-prevention, not a kostyl. Leave it alone, but the
comment is gold; that level of context belongs in any similar
async-cookie-write code we add.

---

## 4. Subtle bugs / edge cases

### 4.1 Empty-state branch in `_all_tasks_inner.html:23-35` shows "No tasks match these filters" — but on the **first** All Tasks visit for a brand-new workspace, the user has never set any filters. Wording would benefit from a different copy when `request.GET` is empty.

Cosmetic. Doesn't affect perf. Park.

### 4.2 The `data-panel-slot` URL-mismatch guard (`acta.js:164-170`) protects against loading a stale page's slots, but it silently exits — never logs. A failed load (e.g. server 500 on `?panel=kanban`) also silently exits via `.finally` flag reset, so the user sees an empty panel after switching to that tab until something else triggers a reload.

Action: in C9 (web tests) add an integration test for the
`?panel=X` short-circuit + failure path. Not blocking.

### 4.3 `_view_panel.html:33` always passes `show_labels=True` to `_table.html` regardless of context:

```html
{% include "web/projects/_table.html" with tasks=table_tasks|default:tasks show_labels=True %}
```

But `AllTasksView.get_context_data:631` also sets
`ctx["show_labels"] = True`. So the explicit-pass is redundant
**but** harmless — `_table.html` would read the context anyway.
Cosmetic.

### 4.4 `_user_task_qs` filters via the membership join (`project__workspace__memberships__user=user`). On Postgres this triggers a JOIN through the membership table on every page load. For a user in N workspaces it's still one query, but a partial unique index on `(user_id, workspace_id)` already exists (membership table is small), so the planner handles it.

Not an issue at current scale; would be one to revisit if active workspaces
per user crosses ~50.

### 4.5 `_kanban_columns_ctx` (`views.py:567-596`) sorts the entire `table_tasks` list in Python with a 3-tuple comparator (status index, neg priority, neg updated_at timestamp). For 192 tasks this is ~1 ms; for 2 000 it's ~10 ms. Fine.

### 4.6 `_resolve_view_mode` accepts `default="table"` but the cookie-default behaviour in `__init__` is `"kanban"` (line 547). On a fresh visit `_resolve_view_mode` returns `"table"` (its default) but the cookie writer falls back to `context.get("view_mode", "kanban")` — and `view_mode` IS set in the context. So the cookie write is correct, but the fallback string `"kanban"` is dead (never observed). Cosmetic; leave.

---

## 5. Measurements deferred (need dev stack)

Hard prerequisite: dev stack up. Skipping per methodology — Vox brings
it up when she's ready. Then run:

1. **Query count baseline** with `CaptureQueriesContext`:
   - `GET /tasks/` cold (table view) — expected: ≤ 10
   - `GET /tasks/?panel=kanban` — expected: ≤ 6
   - `GET /tasks/?panel=list` — expected: ≤ 6
   - `GET /tasks/?panel=timeline` — expected: ≤ 6
   - `GET /tasks/?search=foo` (filter swap, inner fragment) — expected: ≤ 10

2. **Payload size baseline** (current state, post-lazy-panels):
   - `curl -s -o /dev/null -w '%{size_download}' http://web.acta.orb.local:8001/tasks/`
   - Repeat for each `?panel=`.

3. **`pytest --durations=20`** scoped to `apps/web/tests/test_all_tasks*`
   (if such files exist) and `apps/web/tests/test_filters.py`. Capture
   the 10 slowest tests as a regression-detector baseline.

4. **`_table_row.html` rendered size**: `python manage.py shell -c
   "from django.template.loader import render_to_string;
   from apps.tasks.models import Task; t = Task.objects.first();
   print(len(render_to_string('web/projects/_table_row.html', {'task': t, 'status_labels': Task.STATUS_LABELS, 'priority_labels': dict(Task.PRIORITY_CHOICES), 'today': None, 'show_labels': True, 'show_project': True})))"`.
   Concrete number for §3.3 estimate.

Park these until dev is up. When ready, paste numbers back into this
file under §5.

---

## 6. Fix candidates (input to Chunk G)

Each entry is a candidate fix-PR. **Format**: `[impact / effort /
risk] title`. Numbers are 1 (low) - 5 (high) for impact, 1 (small) -
5 (large) for effort, 1 (safe) - 5 (risky) for risk.

| # | Tag | Title | Notes |
|---|---|---|---|
| F1 | `perf/template` `[4/1/1]` | Wrap `task.labels.all` in `{% with %}` in `_table_row.html` + `_task_row.html` | Trivial; shaves ~1 000 in-memory iterations per render |
| F2 | `perf/payload` `[4/2/2]` | Hoist labels-popover Alpine component out of `_table_row.html` | ~150 KB shaved on table; ~700 KB on list view; needs a shared store + per-row `x-data` shrink |
| F3 | `tests/regress` `[3/1/1]` | Add `assertNumQueries` regression test for `AllTasksView` (cold + per `?panel=` + filter swap) | Locks in current N+1 safety |
| F4 | `perf/template` `[2/2/2]` | Replace inline lucide SVGs with `<svg><use>` references | Repo-wide; not All-Tasks-specific. Defer to D3 (CSS / Tailwind / FOUC) |
| F5 | `perf/server` `[3/3/3]` | Memoize `filter_sidebar_context` counts with a short TTL keyed on `(workspace, filter-state-hash)` | Verify cost first in C9; not All Tasks specific (also affects My Work, Project Detail) |
| F6 | `clean/template` `[1/1/1]` | Drop redundant `show_labels=True` pass in `_view_panel.html:33` | Cosmetic |
| F7 | `clean/template` `[1/1/1]` | Improve empty-state copy when no filters are active | Cosmetic |
| F8 | `perf/js` `[2/2/2]` | Try `requestIdleCallback` for lazy-panel loader | Cross-browser; defer to D1 (acta.js audit) |
| F9 | `infra/db` `[3/3/3]` | EXPLAIN `_filter_search` on a populated DB; add trigram or FTS index if `ILIKE` is sequential-scanning | Defer to F (infra) |
| F10 | `perf/data` `[3/3/2]` | Audit `data-sort-*` attribute set per row; drop any not consumed by the client sort | Need to grep `acta.js` for `data-sort-` reads |
| F11 | `clean/copy` `[1/1/1]` | Document the `?panel=` cookie-skip rationale in a doc comment or ADR addendum | The lines 532-543 comment is excellent; surface it |

**Lead candidate for first fix-PR**: F1 + F3 bundled — both small, both
in templates / tests, both make the next perf change safer. F2 is
where the visible byte-shave lives but takes a real refactor.

---

## 7. Inputs to other Wave 1 chunks

- **B2 (board views)**: kanban (`?panel=kanban` short-circuit) is in
  scope here; this audit confirmed it's correctly lazy-loaded but did
  not read `_kanban.html`. B2 will dig into the kanban rendering itself
  (`215 LOC`), DnD in `acta.js:707-817`, and substatus recompute on
  client filter (open TODO).
- **B3 (task detail)**: `_get_user_task_or_404` re-runs the membership
  check on every task open. Acceptable but worth a measurement.
- **B4 (dashboard)**: not impacted directly, but the lazy-panel pattern
  here is a model the dashboard could borrow if its tile-loading needs it.
- **D1 (acta.js)**: lazy-panel loader (`acta.js:147-221`) reviewed in
  context here and judged **solid**. D1 will look at the bigger picture
  — page cache, popstate, fetch vs HTMX choice.

---

## 8. Status

- Chunk B1: **complete**.
- No code changed. All findings are candidates for Chunk G.
- Next chunk: B2 (board views).
