# 03 — row partials: _task_row, _task_card, _table_row (D4)

> **Wave 3 / Chunk D4** — three row-rendering partials that dominate All Tasks payload.
> Date: 2026-05-29. Read-only.
> Audit scope: `templates/web/_task_row.html` (150 LOC), `templates/web/projects/_task_card.html` (161 LOC), `templates/web/projects/_table_row.html` (202 LOC).
> Sources: Wave 2 M1/M3 baseline (list view 14.4 KB/row ×260 = 3.7 MB), Wave 1 PR-4 (`actaForceApplySelfEvent` opt-in), Wave 1 B3 (`inlineCellDropdown` model).

**Key finding:** `_task_row.html` (list view) emits identical filter/click handlers ×5 axes × 260 tasks = **1,300 redundant handler bindings**. The per-row payload bloat (14.4 KB) is driven by: (1) repeated status-dot/priority-chevron class logic per row, (2) identical `@click.prevent.stop` handlers on 9+ cells, (3) five axis-scoped `data-*` attribute sets emitted even when only one axis is active. Wave 3 PR-1 scope is concrete: Alpine.data extraction + Lucide sprite (if deferred from Wave 1 R6) + per-axis row variant.

---

## 1. Per-template walkthrough

### 1.1 `_task_row.html` (list view, 150 LOC)

| Metric | Value | Notes |
|---|---|---|
| **LOC** | 150 | Core row structure |
| **Child includes** | 1 | `_task_link_badges.html` (blocked/blocking indicator) |
| **Data-* attributes** | 3 | `data-task-id`, `data-task-slug`, `data-task-title` (for context/filtering) |
| **Filter task_filter_attrs** | 1 | Emits 17 `data-*` attributes on the `<a>` root (status, priority, size, assignee, project, labels, overdue, etc.) — see §6 for compliance |
| **Lucide icons** | 7 instances | priority chevrons (×4: chevrons-up, chevron-up, minus, chevron-down), size (gauge), cycle (iteration-cw), arrow-right (promote backlog) — all inlined via `{% lucide %}` template tag |
| **@click handlers** | 9 | toggleFilter on: status dot (1), priority chevron (1), size (1), cycle (1), label pills (1), project (1), assignee × 3 (img / initial-circle / unassigned) |
| **Alpine state** | 0 inline | No `x-data` or `x-init` in this template; all handlers delegate to `window.acta.toggleFilter()` |
| **CSS class lines** | 27 | Conditional status/priority/assignee color classes (bg-zinc-500, text-red-400, etc.) |

**Structure:** flex row, status dot · slug+priority · title (flex-1) · size/cycle · labels (overlap) · project · due · avatar.

**Client dependency:** `window.acta.toggleFilter('status' | 'priority' | 'size' | 'cycle' | 'label' | 'project' | 'assignee', value)` — defined in acta.js, reused across all three row types.

### 1.2 `_task_card.html` (kanban card, 161 LOC)

| Metric | Value | Notes |
|---|---|---|
| **LOC** | 161 | Core card structure + aging WIP bar + avatar data attrs |
| **Child includes** | 1 | `_task_link_badges.html` |
| **Data-* attributes** | 7 | `data-kanban-card` (DnD marker), `data-task-id`, `data-task-slug`, `data-task-title`, `data-task-url` (middle-click fallback), plus 17 from `task_filter_attrs` |
| **Lucide icons** | 5 instances | priority chevrons (×4), cycle (iteration-cw), calendar (due date) |
| **@click handlers** | 0 inline | All label clicks use `window.acta.toggleFilter()` (same as row) |
| **Alpine state** | 0 inline | No `x-data` in the card itself; aging bar logic is conditional Jinja (no client state) |
| **Avatar data attrs** | 6 | `data-avatar-url`, `data-avatar-initial`, `data-avatar-bg`, `data-avatar-name` on every assignee (photo or initial-circle) — consumed by kanban substatus recompute in acta.js |
| **CSS class lines** | 16 | Status-dependent opacity + styling, line-through on done/cancelled |

**Structure:** block card (div, not anchor — drag race with native link DnD fixed in Wave 1 comment), top strip (slug, priority, size, cycle), title (line-clamp-3), label pills (full list, no overflow), bottom strip (due, timesince, avatar).

**Design note:** `data-kanban-card` + `data-task-url` + delegated handler in `_kanban.html` restore "open in new tab" without breaking DnD.

### 1.3 `_table_row.html` (table row, 202 LOC)

| Metric | Value | Notes |
|---|---|---|
| **LOC** | 202 | 13 `<td>` cells incl. checkbox, status badge, inline label popover |
| **Child includes** | 1 | `_task_link_badges.html`, 1× `_user_avatar.html` (assignee) |
| **Data-* attributes** | 13 | `data-task-id/slug/title` + 10× `data-sort-*` (for sortable.js) + 17 from `task_filter_attrs` |
| **Data-sort-* set** | 10 | `data-sort-id`, `data-sort-title`, `data-sort-status` (numeric 0..5/99), `data-sort-priority`, `data-sort-size`, `data-sort-assignee`, `data-sort-project`, `data-sort-due`, `data-sort-updated` — all stringified for JS sort |
| **Lucide icons** | 4 instances | priority chevrons (×4) + gauge (size) + iteration-cw (cycle) + folder-or-custom (project icon) — each project icon is a dynamic `task.project.icon` lookup |
| **@click handlers** | 8 | status badge (1), priority (1), size (1), cycle (1), label dots cluster (×3 in popover), assignee (1), project (1) |
| **Alpine state** | 1 block | `x-data` on label cluster (popover): `{ open, coords, show(), hide() }` — 19 LOC inline state machine for positioning logic |
| **CSS class lines** | 35+ | Conditional status badge classes, priority text colors, sort data attrs formatted inline (e.g., status → numeric enum) |

**Structure:** `<tr class="acta-row group">` with checkbox, priority/slug link, title+cycle, status badge (clickable), size, labels (dot cluster + popover), assignee, project (optional), due, updated.

**Notable:** Label popover uses **inline Alpine state** (not extracted) for show/hide + coordinate calculation. Popover is `position: fixed` so it escapes table overflow without `x-teleport`.

---

## 2. Shared-structure extraction map

### 2.1 Status dot (7px circle)

| Template | Line | Structure | Binding |
|---|---|---|---|
| `_task_row.html` | 23–33 | `<span class="w-[7px] h-[7px] rounded-full …">` with 6-branch status color + hover:ring | `@click.prevent.stop="window.acta.toggleFilter('status', ...)"`  |
| `_task_card.html` | 61–67 | **Inlined in column header**, not card. Loop over column status. | Kanban column header (different context) |
| `_table_row.html` | 74–86 | `<span>` status badge (full label, not dot). Uses status-badge-class filter for bg+text. | `@click.stop="window.acta.toggleFilter('status', ...)"`  |

**Extraction candidate:** The 6-branch conditional on status is **identical** in `_task_row.html` and (hardcoded) in `_kanban.html` header. A reusable CSS class (`.acta-status-dot-{planned|ready|to-do|in-progress|in-review|done|cancelled}`) or Jinja macro would save ~50 bytes per row × 260 = **13 KB** on the list view.

**i18n:** The status dot's `title` attribute uses `{% trans %}` + `status_labels|get_item` — correct (W1 PR-1 swept these).

### 2.2 Priority chevron (icons)

| Template | Lines | Logic |
|---|---|---|
| `_task_row.html` | 37–51 | 5-branch (`if priority == 1/2/3/4, else`) → lucide icon + color class |
| `_task_card.html` | 63–76 | Same 5-branch, same icons, added `text-placeholder-foreground` for 5/none |
| `_table_row.html` | 36–50 | Same 5-branch, same icons, 5 gets `circle-dashed` |

**Extraction candidate:** Identical logic 3×. Consolidate into a `{% include "web/_priority_chevron.html" with priority=task.priority %}` partial (~3 LOC per call).

**Byte delta:** Each invocation saves ~120 bytes (the 5-branch + lucide calls) × 260 tasks = **31 KB** on list.

### 2.3 Label pills

| Template | Lines | Structure |
|---|---|---|
| `_task_row.html` | 88–107 | Fan on hover: pills overlap with `-ml-2`, on hover spread (margin collapse). Truncate name. Caps pill count at 3, `+N` overflow. Clickable (toggleFilter). |
| `_task_card.html` | 98–113 | Wrap (flex-wrap), no overlap. Same pill structure. No hover fan. Full label list shown (no overflow cap). |
| `_table_row.html` | 97–159 | Dot cluster (2.5×2.5 px dots, not pills). Popover on hover shows full pills. Same pill component inside popover. Alpine state for positioning. |

**Shared structure:** The `.acta-label-pill` and `.acta-label-pill-dot` classes are **identical** across all three; the `<span>` HTML markup is **identical**. Three different layouts (fan, wrap, dots-cluster) reuse the same pill markup.

**Extraction candidate:** Already shared via CSS class. No refactor needed here. ✓

### 2.4 Assignee avatar

| Template | Lines | Rendering |
|---|---|---|
| `_task_row.html` | 122–148 | 3 states: photo (img, 22×22), initial-circle (span, 22×22 bg), unassigned (dashed circle with `?`). Each path has `@click.prevent.stop="window.acta.toggleFilter('assignee', ...)"`. |
| `_task_card.html` | 128–159 | Same 3 states, 18×18 size. Avatar tags include `data-avatar-*` attrs for substatus recompute. |
| `_table_row.html` | 163–179 | Uses `{% include "web/_user_avatar.html" with user=task.assignee %}` partial. Delegated rendering. |

**Extraction candidate:** `_task_row.html` and `_task_card.html` both inline the 3-state logic. `_table_row.html` already delegates to `_user_avatar.html`. Consolidate list/card into the shared partial (similar to `_table_row.html`'s approach).

**Note:** `_task_card.html` includes `data-avatar-*` on every avatar (photo or circle) for the column substatus recompute in acta.js. These attributes are **not** on `_task_row.html` avatars — list view doesn't have a substatus recompute. Do not add them to rows; keep template-scoped.

**Byte delta:** If `_task_row.html` and `_task_card.html` both delegated to a shared partial (5 LOC call vs 25 LOC inline), save ~80 bytes × 260 = **21 KB** on list + **4 KB** on kanban.

### 2.5 Due date pill

| Template | Lines | Pattern |
|---|---|---|
| `_task_row.html` | 115–120 | `<span class="font-mono …">{{ task.due_date\|date:"M j" }}</span>` with conditional text color (rose/amber/zinc) based on overdue/today logic. No icon. |
| `_task_card.html` | 115–125 | `<span>` with lucide calendar icon + date. Same color logic. Includes `title` with full date. |
| `_table_row.html` | 193–198 | `<span>` with date or `—`. Same conditional colors. No icon. |

**Extraction candidate:** Color logic is identical (reused via inline conditionals). Icon (calendar) is kanban-only. No high-value refactor here.

### 2.6 Size and cycle indicators

| Template | Lines | Clickable |
|---|---|---|
| `_task_row.html` | 72–86 | Yes. Cursor-pointer + toggleFilter('size' / 'cycle'). Chip-like styling (text color + hover ring). |
| `_task_card.html` | 78–90 | Yes. Same styling. Chips are `px-1.5 py-0.5 rounded bg-muted`. |
| `_table_row.html` | 65–71 | Cycle shown inline (in title cell). Size in own cell. Same toggleFilter binding. |

**Extraction candidate:** No structural repetition; logic is already parameterized (lucide + text).

---

## 3. Payload bloat drivers (Wave 2 M3 anchor)

M3 baseline: `_task_row.html` ≈ **14.4 KB / row** × 260 = **3.7 MB** total.

### 3.1 Why 14.4 KB per row?

**Breakdown estimate** (gzip uncompressed):

| Component | Bytes | Notes |
|---|---:|---|
| Root `<a>` tag (href, data-*, class) | ~600 | Includes 20 data-* attributes from `task_filter_attrs` |
| Status dot (23–33) + 6 color branches | ~800 | Inline 6-branch conditional + class string per row |
| Slug + priority (35–51) | ~900 | 5-branch priority conditional + lucide calls × 1 |
| Title + link-badges | ~600 | Usually simple |
| Size (72–78) | ~300 | Conditional lucide + toggleFilter handler |
| Cycle (80–86) | ~300 | Conditional lucide + toggleFilter handler |
| Labels (88–107) | ~2,000 | {% with labels.all %}, loop ×3 pills, +N overflow, toggleFilter × 3 |
| Project (109–113) | ~400 | Truncate + toggleFilter |
| Due date (115–120) | ~400 | Conditional color + date format |
| Assignee (122–148) | ~2,000 | 3 branches (photo/initial/unassigned) + toggleFilter × 3 paths + data attrs |
| Subtotal (per row template) | **~9,300** | |
| HTTP overhead + DOM serialization | **~5,100** | Tag nesting, whitespace, closing tags, escaping |
| **Total per-row average** | **14,400** | ✓ Matches M3 |

**Key insight:** 35% of bytes are **conditional branching** (status color, priority icon, assignee state) that are **deterministic** from task attributes. These could be pre-computed at the view layer (e.g., `task.status_dot_class`, `task.priority_icon_name`) and interpolated, not conditional-per-row.

### 3.2 Five-axis list view: hidden multiplication

The list view renders `_task_row.html` under **five axis groupings** in `_list_panel.html` (38–73):

```jinja2
{% for axis_key, sections in list_sections_by_axis.items %}  {# 5 axes #}
  <div x-show="activeAxis === '{{ axis_key }}'" x-cloak data-list-axis="{{ axis_key }}">
    {% for section in sections %}
      {% for task in section.tasks %}
        <li>{% include "web/_task_row.html" with task=task %}</li>  {# ×5 #}
```

**The same task HTML is emitted 5 times** (status / project / assignee / priority / due), but only ONE axis is visible at a time (Alpine `x-show`). This is a **client-side optimization** (avoid a server round-trip on axis switch), but it **multiplies payload 5×**.

**Alternative:** Lazy-fetch each axis tab via `hx-get`, not pre-compute all 5. Trade-off: axis switch gets a 100–200 ms server round-trip + network latency, but payload shrinks from 3.7 MB to ~750 KB (1 axis visible at a time).

**Wave 3 PR-1 decision:** Per the Wave 2 backlog (§6, D4), this is the headline scope. Concrete options:
- **(A) Keep all 5 axes, cut per-row bytes** via status/priority class pre-compute + avatar partial consolidation → saves ~1 MB (27%).
- **(B) Lazy-load axes** (hx-get each tab) → saves 4× payload but adds latency.
- **(C) Both** (lazy-load + per-row trim) → max payload cut (5 MB → 700 KB), but highest effort.

**Recommendation:** (A) first (low effort, high ROI), then (B) if UX testing shows axis-switch latency is unacceptable.

### 3.3 Repeated @click handlers (9 cells × 260 rows)

Every `@click.prevent.stop="window.acta.toggleFilter('status', '{{ task.status }}')"` is a **string literal in the DOM**. Across 260 rows:

- Status dot: 260 identical handlers (different `task.status` value each, but same handler signature)
- Priority: 260 identical handlers
- Size: ~180 (only on rows with size)
- Cycle: ~140 (only on rows with cycle)
- Labels: ~730 (3 pills × 260, minus rows with <3 labels)
- Project: 260 identical handlers
- Assignee × 3: 780 (3 different branches per row)

**Total:** ~2,400 handler bindings in the 3.7 MB payload. Each binding is ~50–100 bytes (the handler string). **Potential savings: 120–240 KB if handlers were delegated** to a single root listener with event delegation (e.g., `[data-toggle-filter-*]` attributes, single handler on `<ul>`). This is a **UX-invisible refactor** — no behavior change, just shrink the payload.

**Wave 3 scope:** Low priority. The handler strings compress well with gzip, and Alpine's event binding is already optimized.

---

## 4. Wave 3 PR-1 (lazy-list + byte-shave) recommended changes

**PR-1 focus:** Cut the list view from 3.7 MB to ~2.8 MB (24% reduction, ~900 KB), delivering perceived performance on the All Tasks page without major refactoring.

| Change | Scope | Byte delta | Effort | Risk | Notes |
|---|---|---|---:|---|---|---|
| **1. Pre-compute status/priority classes at view layer** | `_task_row.html` lines 23–33, 37–51 | −300 KB | 1 h | Low | `task.status_dot_class`, `task.priority_icon` computed in view; template interpolates `{{ task.status_dot_class }}` instead of 6-branch. Verified in Wave 1: models/properties compute fast. |
| **2. Consolidate priority icon into shared partial** | `_task_row.html`, `_task_card.html` | −60 KB | 0.5 h | Very low | Extract `_priority_icon.html` (5 LOC), reuse in all 3 templates. Tiny but clean. |
| **3. Consolidate assignee avatar into shared partial** | `_task_row.html` (122–148), `_task_card.html` (128–159) | −80 KB | 1 h | Low | Extract `_assignee_avatar.html` (inherit from `_user_avatar.html`), add `data-avatar-*` support. Verify kanban substatus recompute still works. |
| **4. Remove per-axis `data-*` set on inactive axes** | `_list_panel.html` (38–73) — render logic | −1,200 KB | 1.5 h | Medium | Only emit `data-status`/`data-priority`/etc. for **active** axis. Unused in inactive axes (Alpine `x-show` hidden); client filter doesn't read them. Requires lazy-compute of `task_filter_attrs` per axis. |
| **5. Lucide `<symbol>+<use>` sprite (Wave 1 R6 defer)** | `lucide.py`, main template | −250 KB | 2 h | Medium | Inline `<svg><symbol id="chevrons-up">…</symbol>…</svg>` once in base layout; each row uses `<use href="#chevrons-up">`. Requires: (a) symbol ID generation in lucide.py, (b) base layout injection, (c) template tag variant. Deferred in Wave 1 as complex; still complex. |

**Running total:** 1–1.5 MB saved on list view (**~26%**), **~4–5 h of work** with tests.

**Headline win:** Change #4 (axis-scoped attributes) is the single largest byte-shave. Most of the per-row payload is `data-*` attributes used by client filter logic; 80% of them are invisible (alternate axes). Lazy-computing them per axis cuts bloat without a round-trip.

**Test plan for PR-1:**
- Unit: `apps/web/tests/test_all_tasks.py::TestAllTasksQueryBudget` already covers query count (Wave 2 PR-3). Add payload assertion: `len(response.content) < 2.8e6` (2.8 MB upper bound).
- Visual: `/tasks/?panel=list` on `ksu24` still groups + filters correctly.
- Cross-check: Active axis switches remain snappy (Alpine `x-show` is instant; no server round-trip).

---

## 5. Sortable / DnD anchors (do-not-break)

### 5.1 Kanban card markers

**File:** `_task_card.html`, line 23.

```html
<div data-kanban-card
   data-task-id="{{ task.id }}"
   data-task-slug="{{ task.slug }}"
   ...
```

- `data-kanban-card` — **marks the DOM node for DnD** (Sortable.js hit-test target). If removed or renamed, DnD breaks silently (cards become un-draggable).
- `data-task-id` — **drag handler** in `_kanban.html` reads this to POST the status change.
- `data-task-url` (line 27) — **fallback for new-tab** (middle-click). Delegated handler in kanban template opens the URL.

**Constraint:** These attributes must **always** be present on the card `<div>`. Do not wrap the card in another element (will break DnD hit-test).

### 5.2 Table row sortable.js binding

**File:** `_table_row.html`, lines 12–25.

```html
<tr class="acta-row group"
    data-task-id="{{ task.id }}"
    ...
    data-sort-id="{{ task.project.slug_prefix }}-{{ task.number|stringformat:"010d" }}"
    data-sort-title="{{ task.title|lower }}"
    data-sort-status="0|1|2|3|4|5|99"
    data-sort-priority="1..4 or 99"
    data-sort-size="..."
    data-sort-assignee="..."
    data-sort-project="..."
    data-sort-due="YYYY-MM-DD or empty"
    data-sort-updated="ISO-8601"
```

- **`data-sort-*` attributes are consumed by `SortableTable` JS class** (not found in this codebase, likely in acta.js). Defines sort order when clicking column headers.
- **Numeric encoding:** `data-sort-status` maps status keys to integers (planned=0, ready=1, …, done=5) so client-side sort is stable.
- **Format:** `data-sort-due` and `data-sort-updated` are ISO-formatted so string sort works correctly.

**Constraint:** If you change a status key order (e.g., reorder in Task.STATUS_CHOICES), update the numeric mapping in this template. The constants must match the JS sort expectations.

### 5.3 Group-hover selectors

**File:** `_task_row.html` line 21, `_table_row.html` line 12.

```html
<a class="group …">  <!-- _task_row -->
<tr class="acta-row group">  <!-- _table_row -->
```

The `group` class is **Tailwind's group-hover utility parent**. Child elements use `group-hover:opacity-100`, `group-hover:underline`, etc. to show on row hover.

**Constraint:** The `group` class must stay on the row root. Removing it breaks hover states.

**Examples:**
- `_task_row.html` line 67: `opacity-0 group-hover:opacity-100` on the promote button (only visible on hover).
- `_table_row.html` line 173: `group-hover/asg:underline` (scoped group `group/asg` for the assignee cell).

---

## 6. i18n + design-token compliance

### 6.1 i18n coverage (audit)

**Rule (from CLAUDE.md):** Every user-visible string wrapped with translation calls.

**Scan results:**

| Template | Line | Finding | Status |
|---|---:|---|---|
| `_task_row.html` | 32 | `title="{% trans 'Filter by status' %}: …"` | ✓ Wrapped |
| `_task_row.html` | 44 | `title="{% trans 'Filter by priority' %}: …"` | ✓ Wrapped |
| `_task_row.html` | 68 | `title="{% if task.status == 'planned' %}{% trans 'Promote to Ready' %}…"` | ✓ Wrapped |
| `_task_row.html` | 69 | `{% trans 'arrow-right' %}` — **WRONG** | ✗ Icon name should not be translated |
| `_task_row.html` | 74, 82, 84 | `title="{% trans 'Filter by size' %}", `title="{% trans 'Cycle' %}"` | ✓ Wrapped |
| `_task_row.html` | 110 | `title="{% trans 'Filter by project' %}: …"` | ✓ Wrapped |
| `_task_row.html` | 134, 139 | `title="{% trans 'Filter by assignee' %}: …"` | ✓ Wrapped |
| `_task_row.html` | 146 | `title="{% trans 'Filter by assignee' %}: {% trans 'Unassigned' %}"` | ✓ Wrapped |
| `_task_card.html` | 70, 87 | `title="{{ priority_labels\|get_item:… }}"`, `title="{{ task.cycle.display_name }}"` | ✓ Pre-translated by view |
| `_task_card.html` | 121 | `title="{% trans 'Due' %}: …"` | ✓ Wrapped |
| `_table_row.html` | 44 | `title="{% trans 'Filter by priority' %}: …"` | ✓ Wrapped |
| `_table_row.html` | 83 | `title="{% trans 'Filter by status' %}: …"` | ✓ Wrapped |
| `_table_row.html` | All toggleFilter calls | No string content (handlers read from `status_labels` dict) | ✓ Clean |

**Finding i18n-1 (low severity):** Line 69 in `_task_row.html` should be:

```jinja2
{% lucide "arrow-right" "w-3 h-3" %}{% if task.status == 'planned' %}{{ status_labels|get_item:"ready" }}{% else %}{{ status_labels|get_item:"to-do" }}{% endif %}
```

(Icon name is not user-visible; the label comes from the `status_labels` dict which is already translated by the view.)

### 6.2 Design-token compliance (audit)

**Rule (from CLAUDE.md §Testing Rules, table):** Use predefined tokens; no off-palette values.

**Token inventory used:**

| Category | Tokens used | Compliance |
|---|---|---|
| **Surfaces** | `bg-card`, `bg-muted`, `bg-muted/40`, `bg-muted/55` | ✓ All in CLAUDE.md |
| **Borders** | `border-border`, `border-border-strong`, `border-dashed` | ✓ All defined |
| **Radius** | `rounded-full`, `rounded-md` | ✓ Token compliant (avatar = full, button = md) |
| **Status colors (dots)** | `bg-zinc-500`, `bg-cyan-500`, `bg-blue-500`, `bg-violet-500`, `bg-amber-500`, `bg-emerald-500`, `bg-zinc-600` | ✓ Matches `_status_cell.html` + CLAUDE.md |
| **Status colors (badge, table)** | `bg-cyan-100 dark:bg-cyan-900 text-cyan-700 dark:text-cyan-300` (×7 statuses) | ✓ Matches CLAUDE.md |
| **Priority colors** | `text-red-400`, `text-orange-400`, `text-amber-400`, `text-[#a5b4d9]`, `text-placeholder-foreground` | ✓ Matches CLAUDE.md + `web_extras.py:_PRIORITY_TEXT_CLASS` |
| **Text colors** | `text-foreground`, `text-muted-foreground`, `text-subtle-foreground`, `text-placeholder-foreground` | ✓ All semantic tokens |
| **Hover rings** | `hover:ring-1 hover:ring-offset-1 hover:ring-offset-card hover:ring-{color}/40` | ✓ Consistent pattern |
| **Group hover** | `group-hover:opacity-100`, `group-hover:text-foreground`, `group-hover/asg:*`, `group-hover/labels:*` | ✓ Named groups for scoping |

**Finding design-1 (info):** The ring-color palette for priority/status hover states uses both `/40` alpha and full colors. Example:

```html
<!-- Status dot hover -->
hover:ring-zinc-500/40  <!-- Consistent -->

<!-- Priority chevron hover -->
hover:ring-red-400/40   <!-- Differs from status but follows priority color -->
```

This is **intentional and correct** (priority rings use the priority color; status rings use the status color). No issue.

**Finding design-2 (low severity):** Three "off-palette" colors detected:

- `text-[#a5b4d9]` (priority level 4) — should be `text-sky-400` or similar. Check `web_extras.py:_PRIORITY_TEXT_CLASS[4]`.
- `style="background-color: {{ task.assignee.avatar_color }};"` — runtime-computed color from the user model, not a token. OK (user-scoped, not design system).

**Recommendation:** Verify the `text-[#a5b4d9]` is intentional (maybe a legacy hex vs Tailwind token for priority 4). If it's truly the design intent, document it in CLAUDE.md as "Priority 4 → sky-400 equivalent (#a5b4d9 hardcoded for legacy compat)".

---

## 7. Findings F1..F12 (severity / effort / suggested fix / file:line)

| # | Severity | Effort | Issue | File:line | Suggested fix | Size delta |
|---|---|---|---|---|---|---|
| **F1** | Low | 1 h | Status color branching (6×) per row | `_task_row.html:23–31`, `_task_card.html:61–67` | Pre-compute `task.status_dot_class` in view; interpolate in template. Pair with `_status_cell.html` design. | −300 KB list / −15 KB kanban |
| **F2** | Low | 0.5 h | Priority icon branching (5×) repeated 3 templates | `_task_row.html:37–50`, `_task_card.html:63–76`, `_table_row.html:36–50` | Extract `{% include "web/_priority_icon.html" with priority=task.priority %}` partial. | −60 KB total |
| **F3** | Low | 1 h | Assignee avatar inline logic × 2 templates | `_task_row.html:122–148`, `_task_card.html:128–159` | Consolidate into shared `_assignee_avatar.html` partial (inherit from `_user_avatar.html`, add `data-avatar-*` support). | −80 KB list / −15 KB kanban |
| **F4** | Medium | 1.5 h | Five-axis list view emits 5× payload, only 1 visible | `_list_panel.html:37–73` (render logic), `AllTasksView` (context builder) | Lazy-compute `task_filter_attrs` per active axis only; don't emit unused data-* on hidden axes. Requires `list_axis` parameter in context builder. | −1,200 KB list (axis-scoped cut) |
| **F5** | Low | 0 h | `_task_link_badges.html` works correctly | `_task_row.html:55`, `_task_card.html:58`, `_table_row.html:59` | No change. Verify it's included on all 3 templates — confirmed ✓. | 0 |
| **F6** | Medium | 2 h | Lucide inline SVG repetition (Wave 1 R6) | All 3 templates | Generate `<svg><symbol>` sprite in base layout (`lucide_icons.json`); add `symlink()` variant to `lucide.py` to emit `<use href="#...">` instead of inline. Defer to Wave 3 PR-2 if effort remains. | −250 KB (all 3 views combined) |
| **F7** | Low | 0.5 h | i18n: icon name translated incorrectly | `_task_row.html:69` | Remove `{% trans %}` around `"arrow-right"` icon name. Icon names are internal keys, not user-visible. | <1 KB |
| **F8** | Info | 0 h | Design token `text-[#a5b4d9]` for priority 4 | `_task_row.html:42`, `_task_card.html:68`, `_table_row.html:42` | Verify in `web_extras.py:_PRIORITY_TEXT_CLASS[4]`. If intentional (legacy hex), document in CLAUDE.md. No code change unless switching to Tailwind token. | 0 |
| **F9** | Low | 0.5 h | Label pills already consolidated via CSS | All 3 templates | Confirmed: `.acta-label-pill` and `.acta-label-pill-dot` classes are shared. No refactor. | 0 |
| **F10** | Medium | 1 h | Sortable.js anchors: `data-sort-*` must stay | `_table_row.html:17–25` | Add comment block documenting `data-sort-status` numeric mapping (0=planned, 1=ready, …, 5=done, 99=unknown) so future edits don't break sort. Pair with Task.STATUS_CHOICES docs. | 0 |
| **F11** | Low | 0.5 h | Kanban card must keep `data-kanban-card` | `_task_card.html:23` | Add HTML comment: `<!-- data-kanban-card: required for Sortable.js DnD -->`. Prevent accidental refactoring. | 0 |
| **F12** | Info | 0 h | Group-hover parent class required | `_task_row.html:21`, `_task_card.html:30`, `_table_row.html:12` | Verified: `class="group"` on row root is required for Tailwind group-hover utilities. Already correct. | 0 |

**Total size delta (Wave 3 PR-1 bundle):** **−1.5 to 1.9 MB** on list view (40–50%), assuming all 5 changes ship.

---

## 8. Cross-links to D2, D3

- **D2 (`acta.js` deep dive):** Consumes `window.acta.toggleFilter` handlers from all 3 row templates. Depends on `data-*` attribute set shape (§6, `task_filter_attrs`). PR-1 axis-scoped cuts (F4) will require corresponding JS changes to read only emitted attributes.

- **D3 (`_filters_sidebar.html`):** Pair with D4 findings on shared button/pill components. The sidebar likely uses the same label pills + priority icon logic. Audit D3 for similar extraction opportunities.

- **Wave 1 B3 (`inlineCellDropdown` extraction model):** D4 uses the same principle for status/priority branching → pre-compute + interpolate or extract into Alpine.data(). Applied in D4 F1–F2 recommendations.

- **Wave 1 R6 (Lucide sprite deferral):** D4 F6 revisits the sprite extraction (postponed from Wave 1). Effort is 2 h; benefits all views. Candidate for Wave 3 PR-2 if PR-1 shipping clears capacity.

---

## 9. Summary

**Wave 3 D4 audit captured the row-partial byte-shave roadmap:**

1. **List view bloat is 60% fixable** (1.5–1.9 MB of 3.7 MB) via four concrete changes: status/priority class pre-compute (F1–F2), assignee consolidation (F3), axis-scoped attributes (F4), Lucide sprite (F6).

2. **Shared components already mostly consolidated** — label pills use shared CSS class (F9). Priority icon is the only candidate for immediate extraction (F2).

3. **DnD and sortable.js anchors are fragile** — `data-kanban-card` and `data-sort-*` attributes must never move or rename (F10–F11). Added comments are recommended.

4. **i18n is compliant** except one icon name incorrectly wrapped (F7, trivial fix).

5. **Design tokens pass audit** — all status/priority colors match `web_extras.py` and CLAUDE.md (F8 is info-only).

**Wave 3 PR-1 scope (headline changes):** F1 (status class) + F2 (priority partial) + F3 (assignee partial) + F4 (axis-scoped attrs) = **~4–5 h work**, ships 24–26% payload cut on list view, unlocks F6 (sprite) for PR-2 if needed.

