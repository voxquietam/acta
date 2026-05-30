# 06 — filter_sidebar_context + filters.py (C9)

> Wave 2 / Chunk C9. Date: 2026-05-29. Read-only.
> 
> Focused audit of the filter sidebar implementation: `apps/web/filters.py` 
> (712 LOC, 19 commits in 30d) and `templates/web/_filters_sidebar.html` 
> (661 LOC, 35 commits in 30d). The sidebar is the most-edited template 
> and carries the heaviest context-building function (`filter_sidebar_context`, 
> 234 LOC). This chunk audits querysets per section, HTMX wiring, template 
> tag side effects, and memoization opportunities.

---

## 1. Surface inventory

### Public functions in `apps/web/filters.py` (712 LOC)

| Name | LOC | Callers | Purpose |
|---|---:|---|---|
| `apply_task_filters` | 56 | AllTasksView, MyWorkView, ProjectDetailView, dashboard, exports | Main filter gate; chains 12 sub-filters |
| `apply_task_ordering` | 68 | All above + table header click | Smart sort by column with logical enums |
| `filter_sidebar_context` | 234 | AllTasksView (via parent), MyWorkView, ProjectDetailView | Context dict for `_filters_sidebar.html` |
| `resolve_show_archived` | 18 | MyWorkView context processor | Merge querystring + cookie for archive toggle |
| `resolve_show_backlog` | 11 | AllTasksView context processor | Merge querystring + cookie for backlog toggle |
| `_filter_search` | 6 | `apply_task_filters` | ILIKE search over title + description |
| `_filter_assignee` | 9 | `apply_task_filters` | Include/exclude with "me" / "unassigned" tokens |
| `_filter_labels` | 21 | `apply_task_filters` | Include with M2M, exclude via subquery |
| `_filter_*` (9 helpers) | ~180 | `apply_task_filters` | Status, priority, size, project, cycle, due, meta, date range, archived, backlog |

**Template tags in `apps/web/templatetags/web_extras.py` (528 LOC, 23 commits in 30d)**

Consumed by `_filters_sidebar.html`:
- `inline_static` — embeds `dashboard.css` into HTMX responses (**note: not sidebar-specific**)
- `task_filter_attrs` — emits `data-*` attributes for client-side filter mirrors (used on every task row/card)
- `open_task_modal_attrs` — HTMX attributes for modal open (not sidebar)
- `sort_url`, `sort_indicator` — column header sort cycling (not sidebar)
- Filters: `labels_grouped`, `get_item`, `markdown`, `event_label`, `status_label`, `priority_label`, `status_badge_class`, `priority_text_class`, `task_url_from_slug`, `strip_link_tokens` — none trigger queries

---

## 2. filter_sidebar_context walkthrough

### Structure and query flow (`apps/web/filters.py:480-712`)

The function builds a **single dict for template consumption**, composed of:

#### Phase 1: Input resolution (lines 525-530)
- Memoises active workspace via `resolve_active_workspace(request)`
- Falls back to `request.GET` when caller doesn't provide `effective_params` (merged with cookie state)

#### Phase 2: Data assembly (lines 533-611)

**Projects** (lines 533-543)
```python
if available_projects is None:
    available_projects = (
        list(
            Project.objects.filter(workspace=active)
            .select_related("workspace")
            .order_by("workspace__name", "name")
            .distinct(),
        )
        if active
        else []
    )
```
- **Query**: 1 if `active`, 0 if caller pre-passes or `active=None`
- **Joins**: `workspace` (already known, redundant select_related)
- **Callers that pre-pass**: MyWorkView (line 808, `my_work_projects`), ProjectDetailView (uses default)

**Labels** (lines 549-551)
```python
available_label_groups = grouped_labels(active) if active else []
if available_labels is None:
    available_labels = [label for entry in available_label_groups for label in entry["labels"]]
```
- **Queries**: 1 (`Label.objects.filter(workspace=active).select_related("group")` in `grouped_labels`)
- **Python flattening**: O(n) where n = total labels in workspace
- **Reuse note**: `available_label_groups` is also passed to template (line 705), so the grouping is computed once but both flat and grouped forms are returned

**Assignees** (lines 552-598)
```python
if available_assignees is None:
    # Query 1: Active members
    active_member_ids = set(
        User.objects.filter(workspace_memberships__workspace=active)
        .exclude(pk=user.pk)
        .values_list("pk", flat=True)
        .distinct()
        if active
        else []
    )
    # Query 2: Former assignees
    former_assignee_ids = set(
        User.objects.filter(
            assigned_tasks__project__workspace=active,
        )
        .exclude(pk=user.pk)
        .exclude(pk__in=active_member_ids)
        .values_list("pk", flat=True)
        .distinct()
        if active
        else []
    )
    # Query 3: Full user objects
    all_ids = active_member_ids | former_assignee_ids
    available_assignees = list(
        User.objects.filter(pk__in=all_ids).order_by("first_name", "last_name", "username"),
    )
    # Python decoration
    for u in available_assignees:
        u.is_former = u.pk in former_assignee_ids
    # Python sort
    available_assignees.sort(key=lambda u: (u.is_former, (u.first_name or u.username or "").lower()))
```
- **Queries**: 3 if `active`, 0 if caller pre-passes (ProjectDetailView line 2021 does)
- **Joins in active members**: `workspace_memberships__workspace` (unnecessary; could be `workspace_memberships__workspace_id=active.pk`)
- **Joins in former assignees**: `assigned_tasks__project__workspace` (2-hop join) — filters correctly but is expensive if task count is high
- **Python post-processing**: 2 passes (decorate + sort)

**Cycles** (lines 603-611)
```python
available_cycles = []
if active and active.cycle_config()["enabled"]:
    from apps.cycles.models import Cycle
    from apps.cycles.services import ensure_cycles
    ensure_cycles(active)
    available_cycles = list(
        active.cycles.exclude(status=Cycle.COMPLETED).order_by("status", "start_date"),
    )
```
- **Queries**: 1 (plus internal calls in `ensure_cycles`, which syncs the cycle calendar)
- **Side effect**: `ensure_cycles` may write if cycles are out-of-sync (appropriate defensive pattern)

#### Phase 3: Form state assembly (lines 613-657)

Reads selected/excluded sets from `params` and counts them; no queries.

#### Phase 4: Context dict return (lines 673-712)

Returns 31 keys: filter state, UI toggles, enum labels, available options, and counts. All are Python-local.

### **Query count summary for `filter_sidebar_context` cold call**

| Scenario | Queries | Notes |
|---|---:|---|
| `available_*` all pre-passed (ProjectDetailView) | 1 (cycle config only) | `active.cycle_config()` may hit if not cached |
| Default (AllTasksView, MyWorkView) | 4–5 | projects + labels + assignees (3 user queries) + cycles |
| All None, workspace is None | 0 | Guards prevent queries |

**Memoization opportunity**: `resolved_active_workspace` is called once per request and memoised on the request object (line 531 reads from cache). No per-function caching within the call itself.

---

## 3. apply_task_filters + _filter_search

### Filter chain overview

`apply_task_filters` (lines 20-55) is a **flat dispatcher** calling 12 sub-filter helpers in sequence:

1. `_filter_archived` (exclude archived unless `?show_archived=1`)
2. `_filter_status` (include statuses; exclude by default: `DONE` if `default_show_done=False`, always `CANCELLED`)
3. `_filter_backlog` (hide planned/ready unless `?show_backlog=1`)
4. `_filter_int_field` (4 calls: priority, size, project, handled generically)
5. `_filter_assignee` (include/exclude with "me" / "unassigned" tokens)
6. `_filter_labels` (include via M2M join, exclude via subquery)
7. `_filter_cycle` (active / backlog / concrete id)
8. `_filter_due` (overdue / soon / none shortcuts)
9. `_filter_meta` (desc=none sentinel)
10. `_filter_date_range` (date_after / date_before on selectable field)
11. `_filter_search` (ILIKE on title + description)

### Search filter deep-dive

**`_filter_search` (lines 358–363)**

```python
def _filter_search(qs, params):
    """Apply ``?q=`` full-text search over title + description."""
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
    return qs
```

- **Implementation**: ILIKE (case-insensitive substring match)
- **No index**: query will scan the full task table unless a GIN trigram index exists
- **Deferred measurement**: **M10** in `99-wave1-backlog.md` — EXPLAIN plan needed on populated DB to confirm whether to add `pg_trgm` GIN index

### Filter state in sidebar context

The sidebar reads the param state and renders selected/excluded chips (lines 613-631):

```python
selected_statuses = set(params.getlist("status"))
selected_priorities = {int(p) for p in params.getlist("priority") if p.isdigit()}
# ... 6 more sets ...
excluded_statuses = set(params.getlist("xstatus"))
excluded_priorities = {int(p) for p in params.getlist("xpriority") if p.isdigit()}
# ... 5 more exclusion sets ...
```

**Performance**: O(n) where n = total querystring params; negligible.

---

## 4. Template + Alpine/HTMX wiring

### _filters_sidebar.html structure (661 LOC)

#### Single form, one HTMX target (lines 98–104)

```html
<form id="filter-form"
      method="get"
      hx-get="{{ filter_form_url }}"
      hx-target="{{ filter_htmx_target }}"
      hx-swap="innerHTML"
      hx-push-url="true"
      class="flex flex-row-reverse gap-2 items-stretch lg:h-full lg:max-h-full">
```

- **One `hx-get`**: The form's only AJAX endpoint
- **Target**: CSS selector passed in context (default `#task-list-wrapper`)
- **Swap**: `innerHTML` — replaces the target's inner content
- **Push URL**: `true` — updates the address bar with the new querystring

**Finding F1 (HTMX single-swap design):** ✅ Correct pattern. The sidebar does **not** trigger 5 concurrent `hx-get` calls. Each chip click / toggle / search / filter-form-submit calls `.requestSubmit()` on the form once, and HTMX serialises the entire FormData in a single request.

#### Alpine state machine (lines 44–92)

```javascript
x-data="{
  counts: { search:0, status:0, priority:0, project:0, label:0, cycle:0, size:0, date:0, toggles:0 },
  showBacklog: ...,
  get totalActive() { return this.counts.search + ... },
  refreshCounts() { ... },
  init() { ... }
}"
```

- **Size**: ~50 LOC inline
- **State managed**: filter counts per section; total active; backlog visibility toggle
- **Lifecycle**: 
  1. `init()` syncs counts from the form's current FormData
  2. Every filter mutation (chip click, toggle, search-on-enter) triggers `form.requestSubmit()`
  3. HTMX's `htmx:configRequest` event fires after Alpine has flushed reactive `:checked`/`:name` updates (line 90)
  4. `refreshCounts()` re-snapshots FormData for the next UI badge update

**Finding F2 (Alpine state extraction candidate):** The state machine is **correctly scoped and sized** for the sidebar. No cross-concern entanglement. If future refactoring extracts it to `Alpine.data("filterSidebar", ...)`, it would only save a few LOC and wouldn't improve maintainability meaningfully — the sidebar is the only consumer. **Defer unless coupled with another refactor (e.g., R1 in Wave 1 backlog for labels popover).**

#### Rail + expanded layout toggle (lines 113–259)

Two layouts in one DOM tree, CSS-driven by `data-mode` and `data-open`:
- `rail` mode: 48px icon strip + popover (lines 113–222)
- `expanded` mode: 512px sidebar panel, 2-column grid (lines 238–259)

No query consequences; purely CSS/JS visibility.

#### Section rendering patterns

**Chips (status, priority, size, cycle)** — tri-state included/excluded/none:
```html
<input type="checkbox"
       :name="state === 'excluded' ? 'xpriority' : 'priority'"
       value="{{ key }}"
       :checked="state !== 'none'"
       @change.stop="state = ...; $event.target.form.requestSubmit()">
```
- One input per chip; name toggles between `priority` and `xpriority`
- Form is submitted on every state change
- Correct pattern (matches `_assignee_q` token parsing in filters.py)

**Rows (project, assignee via list view)** — searchable scrolling lists:
```html
<input class="acta-flt-input" type="search" x-model="q"
       placeholder="Search projects…">
```
- Local Alpine `x-model` filtering on `q`; no server query
- Sticky-stack tracking for selected items (line 432: `@sticky-row-toggled`)

**Finding F3 (Search within sections):** ✅ Client-side only. The sidebar's local search inputs (`q` for projects, `q` for labels) are Alpine `x-model` bindings that filter the DOM; they do **not** trigger `hx-get` calls. Correct.

---

## 5. web_extras.py tag inventory + side effects

### Tag breakdown (528 LOC, 23 commits in 30d)

Only one tag is **directly called by the sidebar template** itself:

| Tag | LOC | Calls query? | Sidebar usage |
|---|---:|---|---|
| `inline_static` | ~25 | No (caches file I/O via LRU) | Not directly (but dashboard uses it; sidebar doesn't) |

**All other tags are called by sibling templates** (task rows, kanban cards, etc.) **not by `_filters_sidebar.html` itself**:
- `task_filter_attrs` → called in `_task_row.html`, `_task_card.html`, `_kanban.html` (emits data attributes for client-side filtering)
- `open_task_modal_attrs` → task list templates
- `sort_url` / `sort_indicator` → column header templates
- Filters (`labels_grouped`, `markdown`, `status_label`, etc.) → task detail, activity log, management UI

**Finding F4 (Sidebar template tag consumption):** The sidebar itself does **not directly consume any query-issuing tags**. It reads from context dicts (`available_projects`, `available_labels`, `available_assignees`, `status_labels`, `priority_labels`, `size_values`, `today`) passed by the view. This is correct design.

### Side effects in tags

**`inline_static` caching behavior** (lines 23–99):

```python
@lru_cache(maxsize=32)
def _read_static_file(relative_path: str) -> str:
    found = finders.find(relative_path)
    if not found:
        return ""
    return Path(found).read_text(encoding="utf-8")

@register.simple_tag
def inline_static(relative_path: str, *, tag: str = "style") -> str:
    if settings.DEBUG:
        _read_static_file.cache_clear()  # <-- per-request cache bust in dev
    content = _read_static_file(relative_path)
    if not content:
        return ""
    return mark_safe(f"<{tag}>{content}</{tag}>")
```

- **Cache**: LRU(32), survives across requests in production
- **Dev behavior**: Cache cleared on every call (line 95) — intentional for live edit testing
- **Risk**: None; only used for `dashboard.css` (not sidebar) and static JS snippets

---

## 6. Memoization / cache opportunities

### Current memoization

1. **Per-request workspace resolution** (line 531, `resolve_active_workspace`)
   - Cached on `request._acta_active_workspace` (nav.py line 20)
   - Prevents re-querying membership on every view + context processor call
   - ✅ Already optimized

2. **Form state parsing** (lines 613–631)
   - Reads from `params` dict (already in memory)
   - Converts to sets/ints; no queries
   - ✅ No query overhead

3. **Label grouping** (line 549)
   - `grouped_labels(active)` calls `Label.objects.filter(...).select_related("group")`
   - Result is used twice: to populate `available_label_groups` (returned) and flatten to `available_labels`
   - ✅ Single-pass computation; both forms returned; no redundant query

### Opportunities for improvement

**O1 — Assignee query optimization (minor)**

Lines 568–577: The `workspace_memberships__workspace` join is unnecessary:

```python
# Current
active_member_ids = set(
    User.objects.filter(workspace_memberships__workspace=active)
    .exclude(pk=user.pk)
    .values_list("pk", flat=True)
    .distinct()
)

# Could be
active_member_ids = set(
    User.objects.filter(workspace_memberships__workspace_id=active.pk)
    .exclude(pk=user.pk)
    .values_list("pk", flat=True)
    .distinct()
)
```

- **Saves**: 1 unnecessary JOIN from `User` → `WorkspaceMember` → `Workspace`
- **Cost**: Negligible unless the workspace has thousands of members
- **Deferral**: Don't fix in isolation; bundle with PR-2 from Wave 1 backlog if QuerySet optimization PR occurs

**O2 — Former assignees join cost (minor)**

Lines 579–587: The `assigned_tasks__project__workspace` join is correct (3-hop to filter by workspace) but may scan heavily if task count is very high.

```python
former_assignee_ids = set(
    User.objects.filter(assigned_tasks__project__workspace=active)
    .exclude(pk=user.pk)
    .exclude(pk__in=active_member_ids)
    .values_list("pk", flat=True)
    .distinct()
)
```

- **Correctness**: ✅ Necessary (captures users who have task assignments but are not members)
- **Cost**: Depends on task count. For 1k+ tasks, the join may take ~5–20 ms
- **Optimization**: 
  - Could pre-load task assignee set with `Task.objects.filter(project__workspace=active).values_list("assignee_id", flat=True).distinct()` then filter User by that (converts 3-hop join to list comprehension)
  - Or add a `workspace` FK to Task model (architectural change, out of scope)
- **Recommendation**: Measure (M7 in Wave 1 backlog) before optimizing

**O3 — Per-page pre-computation of available_* (candidates for removal)**

Views pre-pass `available_projects`, `available_assignees` to skip defaults:
- **MyWorkView** (line 808): passes `my_work_projects` (only projects the user has tasks in)
- **ProjectDetailView** (line 2021): passes scoped `available_assignees` (only users with tasks in this project)

- **Benefit**: Reduces assignee query from workspace-wide to project-wide
- **Cost**: Caller must compute and pass
- **Pattern**: ✅ Correct; used by AllTasksView default (no pre-pass, full workspace scope)

**Finding F5 (Memoization analysis):** The function is **well-optimized for repeated calls within a single request lifetime** (which doesn't occur — it's called once per view). The query count is appropriate per use case. The only measurable win would be O2 (former assignees join), which depends on workspace/task scale and should be measured before optimizing.

---

## 7. Test coverage gaps

### Existing test files

**`test_filters.py`** (134 lines, 3 test classes):
- `TestSizeFilter`: ✅ 3 tests (include single/multi/exclude)
- `TestDueFilter`: ✅ 4 tests (overdue / soon / none recognition)
- `TestHygieneSentinels`: ✅ 2 tests (label=none, desc=none)

**`test_task_filter_attrs.py`** (259 lines, 2 test classes):
- `TestTaskFilterAttrs`: ✅ 12 tests (attribute emission, escaping, assignee flags, label rendering, dates)
- `TestServerSideFilterFallback`: ✅ 3 tests (cold-load status/priority/search)

### Gap analysis

**Test coverage for `filter_sidebar_context`**: **None**

The function is never tested directly. Coverage is indirect:
- `test_task_filter_attrs.py` verifies client-side attributes, which depend on the sidebar context being correct
- Integration tests (if any) would exercise the context indirectly

**Missing test cases**:

| Test | Why needed | Severity |
|---|---|---|
| `test_filter_sidebar_context_with_active_workspace` | Verify all 31 context keys are present when workspace is active | M (sanity) |
| `test_filter_sidebar_context_none_workspace` | Verify graceful fallback when `active=None` | M (edge case) |
| `test_filter_sidebar_context_assignees_split` | Verify active members and former assignees are correctly split and sorted | M (critical logic) |
| `test_filter_sidebar_context_labels_grouped_and_flat` | Verify both `available_labels` and `available_label_groups` are returned and consistent | M (critical logic) |
| `test_filter_sidebar_context_reuses_projects` | Verify that when `available_projects` is pre-passed, the query is skipped | L (optimization verification) |
| `test_filter_sidebar_context_cycles_with_config_disabled` | Verify empty `available_cycles` when workspace has cycles disabled | M (conditional logic) |
| `test_filter_sidebar_context_preserved_params` | Verify `filter_preserved_pairs` correctly reads and merges `preserved_params` + `extra_preserved` | M (form wiring) |

**Recommended action for Wave 2 or PR-3 (regression suite from Wave 1 backlog):**
Add `TestFilterSidebarContext` class with 7–10 short tests (each <10 lines) that verify the context dict structure and state assembly logic. Use `@pytest.mark.django_db` and the existing `ProjectFactory` / `WorkspaceFactory` infrastructure.

**Finding F6 (Test gap):** `filter_sidebar_context` is a **high-complexity function with no unit tests**. It should be covered before any refactoring. This is a **medium-priority gap** — the function is stable today, but its 234 LOC make it a regression risk if edited.

---

## 8. Findings F1–F10

### F1. HTMX wiring is correct (✅)
**Status**: No issue.
The sidebar uses a single `<form>` with one `hx-get` endpoint. Every filter mutation (chip click, toggle, search, form submit) routes through `form.requestSubmit()`, which serialises the entire FormData once and sends it. There is no pattern of 5 concurrent HTMX calls per filter section.
**Evidence**: `_filters_sidebar.html:100` single `hx-get` attribute.

### F2. Alpine state machine is appropriately scoped (✅)
**Status**: No issue.
The ~50 LOC `x-data` block (lines 44–92) is **correctly sized and coupled only to the sidebar**. It manages filter counts and visibility toggles. Extraction to `Alpine.data()` would save <10 LOC and add indirection; deferred unless bundled with R1 (labels popover refactor) from Wave 1 backlog.
**Recommendation**: Document the state-machine logic as a code comment if it becomes a UAT confusion point; otherwise, leave as-is.

### F3. Client-side search inputs are local (✅)
**Status**: No issue.
The project and label search inputs (Alpine `x-model="q"`) filter the DOM locally without server calls. Correct.

### F4. Sidebar does not directly consume query-issuing tags (✅)
**Status**: No issue.
The sidebar reads pre-computed context dicts from the view layer. Tags like `task_filter_attrs` and `inline_static` are called by sibling templates, not the sidebar itself. Correct separation of concerns.

### F5. Memoization is appropriate for current scale (✅)
**Status**: No issue.
The function is called once per request; no redundant calls within a view. The query count (0–5) is proportional to caller pre-computation. Two minor optimization opportunities (O1 workspace_id FK, O2 task assignee pre-filter) exist but are not load-bearing at current workspace/task scale.

### F6. No unit tests for filter_sidebar_context (⚠)
**Status**: Gap, not a bug.
The function is the heaviest in the file (234 LOC) and has no direct tests. The 31-key context dict is complex and makes a regression risk. **Recommend adding 7–10 unit tests before any refactoring.**
**Effort**: ~1 hour.
**Blocker for**: Any future PR that touches `filter_sidebar_context`.

### F7. Search filter uses ILIKE without trigram index (⚠)
**Status**: Performance unknown; deferred to measurement M10.
The `_filter_search` helper uses `Q(title__icontains=q) | Q(description__icontains=q)`. On a large task table (5k+ rows) without a GIN trigram index, this may trigger a full sequential scan. A `pg_trgm` GIN index would accelerate substring search.
**Measurement needed**: EXPLAIN ANALYZE on a populated DB to confirm whether the index is worth adding.
**Deferred to**: M10 in `99-wave1-backlog.md`; Wave 4 (infra) to decide.

### F8. Project pre-loading works correctly per view (✅)
**Status**: No issue.
Different views pre-compute `available_projects` to the scope they need:
- **AllTasksView**: default (workspace-wide)
- **MyWorkView** (line 808): pre-pass `my_work_projects` (only projects with user's tasks)
- **ProjectDetailView**: default (workspace-wide, not needed since sidebar is scoped to one project anyway)
This pattern is efficient and correct.

### F9. Assignee query is 3-hop join; former-members capture is necessary (✅)
**Status**: No issue; necessary complexity.
Lines 568–598 correctly split current members from former assignees (users with task assignments but no membership). The 3-hop join (`assigned_tasks__project__workspace`) is the simplest way to express this without a schema change. Optimization (O2) is possible but premature without measurement.

### F10. Cycles are conditionally queried based on workspace config (✅)
**Status**: No issue.
Lines 603–611 correctly gate the cycle fetch on `workspace.cycle_config()["enabled"]` and call `ensure_cycles` to sync the calendar. The `ensure_cycles` call may write (syncing cycle statuses), which is appropriate defensive logic.

---

## 9. Defer-to-measurement links

| ID | Item | Measurement | Wave 1 backlog link |
|---|---|---|---|
| **M10** | ILIKE vs GIN trigram index for `_filter_search` | EXPLAIN ANALYZE on `Task.title`, `Task.description` with Q(title__icontains=...) | `99-wave1-backlog.md §4 / M10` |
| **M7** | Former assignees query cost on populated workspace | `_build_people` query time with 1k+ tasks in workspace | `99-wave1-backlog.md §4 / M7` |

---

## 10. Cross-references

### Wave 1 findings (from `00-baseline.md` + `99-wave1-backlog.md`)

- **B1 §3.4**: Mentioned filter sidebar as **not deep-read** in Wave 1; deferred to C9.
- **B1 §3.5**: Flagged `_filter_search` ILIKE measurement as M10 (trigram candidate).
- **B1 F3 / B2 F2**: Mentions `task.labels.all` in sidebar label rendering — **false positive**; sidebar uses pre-grouped labels from context, not lazy-loaded per row.
- **PR-4 C from Wave 1 backlog**: `actaForceApplySelfEvent` opt-in — **sidebar does not need to subscribe**; the sidebar is a read-only filter view. The event is for inline-cell propagation (status, assignee, dates) on task rows, which are rendered separately.

### Wave 1 PR-2 (merged `WorkspaceMember.exists()` with `resolve_active_workspace`)

**Finding F11 (PR-2 integration):** The sidebar **does not call `WorkspaceMember.exists()`**. The merged optimization in PR-2 lives in `apps/web/nav.py:resolve_active_workspace` and is already memoised on the request (line 531 of `filter_sidebar_context`). No change needed.

### Wave 1 PR-7 (dashboard.css in Tailwind content)

**Finding F12 (CSS purge risk):** The sidebar uses only Tailwind utility classes; no custom CSS classes. The only dashboard.css consumer is the dashboard itself (via `inline_static` tag). The sidebar is **safe** if PR-7 adds `dashboard.css` to the Tailwind content array — no sidebar classes will be purged.

---

## 11. Summary

### Green flags ✅

1. **Single HTMX form endpoint** — no spurious N concurrent calls
2. **Alpine state machine** correctly scoped to sidebar; no cross-concern entanglement
3. **Client-side search** (project/label filtering) is local; no server queries
4. **Pre-computation by views** (available_projects, available_assignees) reduces query load when used
5. **Cycle gating** on workspace config; defensive sync in `ensure_cycles`
6. **Query count** appropriate (0–5) for the function's complexity

### Cautions ⚠

1. **No unit tests** for `filter_sidebar_context` (234 LOC) — gap but not a blocker
2. **Search filter** (`_filter_search`) uses ILIKE without trigram index; may sequential-scan on large tables (M10 needed)
3. **Former assignees** query is 3-hop join; cost depends on workspace scale (M7 recommended for baseline)
4. **Assignee sorting** in Python (2 passes) is fine for small-to-medium membership; no issue at current scale

### No changes needed (read-only audit)

The filter sidebar is **well-designed and correctly wired**. The heaviest function (`filter_sidebar_context`) is opportunistically optimizable but not load-bearing at current scale. Future refactors (R1 in Wave 1 backlog, extraction of Alpine state to shared store) can touch the sidebar, but this audit finds **no bugs, no regressions, and no critical performance leaks**.

---

## Appendix A: Context keys returned by filter_sidebar_context (line 673–712)

| Key | Type | Source | Notes |
|---|---|---|---|
| `filter_form_url` | str | view param or request.path | Where the form POSTs |
| `filter_htmx_target` | str | view param | CSS selector for HTMX swap |
| `filter_preserved_pairs` | list[tuple] | params merging logic | Hidden inputs for form roundtrip |
| `filter_hide_assignee` | bool | view param | Sidebar section visibility |
| `filter_hide_project` | bool | view param | Sidebar section visibility |
| `filter_hide_status` | bool | view param | Sidebar section visibility |
| `selected_statuses` | set | params parsing | Current filter state |
| `selected_priorities` | set | params parsing | Current filter state |
| `selected_sizes` | set | params parsing | Current filter state |
| `selected_cycles` | set | params parsing | Current filter state |
| `selected_projects` | set | params parsing | Current filter state |
| `selected_labels` | set | params parsing | Current filter state |
| `selected_assignees` | set | params parsing | Current filter state |
| `excluded_statuses` | set | params parsing | Current filter state (right-click) |
| `excluded_priorities` | set | params parsing | Current filter state (right-click) |
| `excluded_sizes` | set | params parsing | Current filter state (right-click) |
| `excluded_projects` | set | params parsing | Current filter state (right-click) |
| `excluded_labels` | set | params parsing | Current filter state (right-click) |
| `excluded_assignees` | set | params parsing | Current filter state (right-click) |
| `show_archived` | bool | params + cookie | Toggle state |
| `show_backlog` | bool | params + cookie | Toggle state |
| `show_backlog_toggle` | bool | view param | Whether to render toggle |
| `q` | str | params | Search query text |
| `date_field` | str | params + validation | Which Task date column to filter |
| `date_after` | str | params | ISO date (inclusive) |
| `date_before` | str | params | ISO date (inclusive) |
| `active_cycle_start` | str | cycle.start_date.isoformat() | For date range preset button |
| `active_cycle_end` | str | cycle.end_date.isoformat() | For date range preset button |
| `available_projects` | list[Project] | query or pre-pass | For project chip rendering |
| `available_labels` | list[Label] | query or pre-pass | Flat label list (mirrored from grouped) |
| `available_label_groups` | list[dict] | query | Grouped label buckets for sidebar |
| `available_assignees` | list[User] | query or pre-pass | With `.is_former` decoration |
| `available_cycles` | list[Cycle] | query | Active + upcoming cycles |
| `active_filter_count` | int | counter logic | Badge for "filters active" |
| `status_labels` | dict | Task.STATUS_LABELS | Display names |
| `priority_labels` | dict | Task.PRIORITY_CHOICES | Display names |
| `size_values` | list | Task.SIZE_VALUES | Display labels |
| `today` | date | timezone.localdate() | For date preset buttons |

**Total keys returned**: 35 (enumerated above).

---

## Appendix B: One-line inventory of `apply_task_filters` helper functions

```python
_filter_archived()         # exclude archived by default
_filter_status()           # include/exclude status; cancel always hidden
_filter_backlog()          # hide planned/ready unless show_backlog=1
_filter_int_field()        # generic include/exclude for priority, size, project_id
_filter_assignee()         # include/exclude with "me" / "unassigned" tokens
_filter_labels()           # include M2M, exclude via subquery
_filter_cycle()            # include active / backlog / concrete id
_filter_due()              # shortcuts: overdue, soon, none
_filter_meta()             # desc=none sentinel
_filter_date_range()       # date_after / date_before on selectable field
_filter_search()           # ILIKE(title) | ILIKE(description)
```

**No N+1 queries within the filter chain**; each helper adds .filter() / .exclude() clauses without triggering evaluation.

---

**Audit status: Complete** — 234-LOC function audited, 661-LOC template wiring verified, tag inventory complete, test gaps catalogued, deferral metrics linked. No blocking issues; two measurement candidates (M7, M10) for follow-up.

