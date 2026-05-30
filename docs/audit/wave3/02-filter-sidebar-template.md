# 02 — _filters_sidebar.html (D3)

> Wave 3 / Chunk D3. Date: 2026-05-29. Read-only.
> 
> Companion audit of `templates/web/_filters_sidebar.html` (661 LOC, 35 commits in 30d) to Wave 2 C9 (`docs/audit/wave2/06-filter-sidebar.md`), which audited the Python context builder (`filter_sidebar_context`, 234 LOC). D3 audits the template-side architecture: HTML / Alpine / HTMX patterns, section structure, state wiring, i18n coverage, accessibility, and cross-template repetition.

---

## 1. Block Inventory

### LOC per structural section

| Block | Lines | LOC | Purpose |
|---|---|---|---|
| Header + docstring | 1–43 | 44 | Architecture overview + tri-state chip explanation |
| `<aside>` x-data + `<form>` | 44–108 | 65 | Top-level Alpine state machine (filter counts, backlog toggle, init) + form element open |
| Rail (48px) | 110–224 | 115 | Icon strip with 9 buttons + reset + separators; `$store.filters.mode === 'rail'` visibility |
| Content panel header | 225–271 | 47 | "Filters" heading + count badges (expanded only) + reset button (expanded) + collapse button |
| Section container open | 272–275 | 4 | `.acta-flt-content-b` grid wrapper |
| **Search** | 276–291 | 16 | Single text input, col-span-2 (bare style) |
| **Status** | 292–336 | 45 | 7 chips (planned/ready/to-do/in-progress/in-review/done/cancelled), tri-state, conditional backlog hide |
| **Priority** | 337–375 | 39 | 5 chips (1..5 + no-priority), tri-state, colored dots |
| **Size** | 376–409 | 34 | Fibonacci values (1,2,3,5,8,13), tri-state, monospace |
| **Project** | 410–468 | 59 | Scrollable list (max-h-[180px]), client-side search (`x-model="q"`), sticky-stack tracking |
| **Cycle** | 469–511 | 43 | "Active" + N concrete cycles + "Backlog", include-only, dot indicators |
| **Date** | 512–556 | 45 | Date field selector (6 options) + quick presets (7d/30d/Cycle/Clear) + from/to inputs |
| **Labels** | 557–620 | 64 | Grouped buckets (group header + labels per bucket), client-side search, color-coded dots |
| Section container close | 621–622 | 2 | Close `.acta-flt-content-b` |
| Toggles footer | 623–660 | 38 | "Show backlog" + "Show archived" toggle pair, pinned to panel bottom |
| Closing tags | 661–662 | 2 | Close `</form>` + `</aside>` |

**Total: 662 LOC (including comments); 661 executable lines.**

### Section-level component breakdown

**Atomic patterns:**
- **Chip** (status, priority, size, cycle): `<label>` + hidden `<input type="checkbox">` + styled `<span class="acta-flt-chip">`. Tri-state for all except cycle (include-only).
- **Row** (project, label): Searchable `<label>` + hidden checkbox + styled `<span class="acta-flt-row">` with icon + name. Tri-state state machine per row.
- **Input wrapper** (search, project search, label search): Lucide icon + `<input type="search">` inside `.acta-flt-input-wrap`.
- **Scroller** (project list, label groups): `.acta-flt-scroller` with overflow-y-auto and fixed/responsive max-height.

---

## 2. Alpine x-data Audit (Extraction Candidates)

### Main `<aside>` x-data block (lines 44–93, ~50 LOC)

```javascript
x-data="{
  counts: { search:0, status:0, priority:0, project:0, label:0, cycle:0, size:0, date:0, toggles:0 },
  showBacklog: bool,
  get totalActive() { return counts.search + ... },
  refreshCounts() { ... /* ~16 LOC */ },
  init() { ... /* ~20 LOC */ }
}"
```

**State:**
- `counts`: Badge counter for each filter section (9 entries)
- `showBacklog`: Mirrored from server `show_backlog` toggle; gates visibility of "planned" / "ready" chips in Status section (line 310)
- `totalActive`: Computed getter summing all counts (drives "has-active" styling on Reset button)

**Lifecycle:**
1. `init()` (line 72–92) fires on Alpine boot:
   - Calls `$store.filters._syncHtmlClass()` (applies `acta-filters-open` / `acta-filters-closed` to `<html>`)
   - Snapshots form's current FormData via `$nextTick()` to read server-rendered checkbox states
   - Registers `htmx:configRequest` listener on form to re-snapshot FormData **after** HTMX has serialised the request (ensuring reactive `:name`/`:checked` bindings are flushed)

**Issues & observations:**
- **Size**: ~50 LOC — appropriate for a single-page component. Extraction to `Alpine.data("filterCounts", ...)` would save ~2 LOC (removing the outer braces) and add indirection; not justified.
- **State coupling**: `showBacklog` is sourced from the server (via template variable, line 50) but **not** persisted to localStorage; it's a read-only mirror that changes only when the user toggles the checkbox. This is correct (the toggle input's change handler calls `form.requestSubmit()`, which re-renders the page with the new `show_backlog` value).
- **Shared state access**: The block reads `$store.filters` (Alpine global store, defined in acta.js line 3049). See section 3 below.

### Per-chip/row x-data blocks (lines 309, 352, 392, 436, 480, 490, 501, 595)

Each tri-state chip or row carries:

```javascript
x-data="{ state: '{% if ... %}excluded{% elif ... %}included{% else %}none{% endif %}' }"
```

**Size per block**: 1 LOC (template variable, embedded in opening tag).

**State**:
- `state`: Always one of `'none'` | `'included'` | `'excluded'`.
- **Rendered from server**: Server-side checks `key in excluded_*` / `key in selected_*` sets and emits the initial state as a literal string in the template.
- **Lifecycle**: On mount, the chip's state reflects the URL's current filter state. User clicks trigger `@change` handler on the hidden checkbox, which updates `state` and calls `form.requestSubmit()`.
- **Reset**: `@acta:filter-reset.window` listener (line 311, 353, 393, 441, etc.) drops state back to `'none'` when the Reset button is clicked (broadcast via `window.dispatchEvent(new CustomEvent('acta:filter-reset'))` at line 217).

**Issues & observations**:
- **Duplication**: The pattern repeats 8+ times (once per chip/row). A custom Alpine component or mixin could reduce LOC, but **the template is more readable with inline state**. Deferral justified.
- **No cross-chip coordination**: Each chip's state is independent; no interaction between them.
- **Input name reactivity**: The hidden `<input>` element's `:name` attribute toggles between `priority` and `xpriority` (line 356) depending on `state`. This is correct and mirrors the form's tri-state wire protocol (priority vs. xpriority keys).

### Section-local x-data blocks (lines 411, 513, 558)

**Project section** (line 411):
```javascript
x-data="{ q: '' }"
```
Local search query; used at line 435 with `:class="q && !'{{ p.name|lower|escapejs }}'.includes(q.toLowerCase()) ? 'hidden' : ''"` to filter project rows.

**Date section** (line 513, ~40 LOC):
```javascript
x-data="{
  fmt(d) { ... },
  submit() { ... },
  setRange(after, before) { ... },
  lastDays(n) { ... },
  clear() { ... }
}"
```
Helpers for date range presets (7d, 30d, Cycle, Clear buttons) and manual date inputs. No shared state with other sections.

**Label section** (line 558):
```javascript
x-data="{ q: '' }"
```
Same pattern as Project: local search query for filtering grouped labels.

### State sharing analysis

**Questions:**
1. Is state duplicated across blocks? **No.** Each chip/row owns `state: '...'`; no coordination.
2. Is state lifted to a higher-level store? **Partially.** The main `<aside>` block manages filter counts and backlog visibility. Chip state is scattered. This is **correct by design**: chip state is transient (tied to the form's input values); the aside's state is **used for UI badges and visibility logic** (lines 128, 137, 138, 147, 148, etc. for indicator dots).
3. Could state be lifted to `$store.filters` (the Alpine global store)? **No.** The global store is for **layout mode** (rail vs. expanded), **open section**, and **popover positioning** — all sidebar-wide concerns, not per-chip data. Chip state is **already accurately represented in the form's HTML inputs**; duplicating it in the store would create a mirror-of-truth problem.

**Conclusion on extraction**: The current structure is **well-factored**. Extraction of per-chip state to `Alpine.data()` would add complexity without benefit. The main aside block is appropriately scoped and sized. **No refactoring recommended.**

---

## 3. HTMX Wiring Map

### Form configuration (lines 98–104)

```html
<form id="filter-form"
      method="get"
      hx-get="{{ filter_form_url }}"
      hx-target="{{ filter_htmx_target }}"
      hx-swap="innerHTML"
      hx-push-url="true">
```

**Endpoint**: `filter_form_url` (passed via context; default `request.path`). Single HTMX endpoint — no multiple concurrent requests per filter section.

**Target**: `filter_htmx_target` (passed via context; default `#task-list-wrapper`). The result (task list) is swapped into this element.

**Swap mode**: `innerHTML` — replaces the target's inner content. The sidebar itself is **not** the swap target; it persists across filter mutations.

**URL push**: `true` — every form submission updates the browser address bar with the new querystring.

### Trigger patterns

**Chip/Row state change** (line 318, 360, 400, 448):
```html
<input ... @change.stop="state = ...; $event.target.form.requestSubmit()">
```
Direct state update (Alpine reactive) + immediate form submit. No `hx-trigger` attribute needed; the form's `hx-get` fires because `.requestSubmit()` triggers the form's default `submit` event, which HTMX intercepts.

**Search input** (line 282–288):
```html
<input type="search"
       name="q"
       value="{{ q }}"
       x-data
       @acta:filter-reset.window="$el.value = ''"
       placeholder="...">
```
**No explicit trigger** — the form is submitted elsewhere (via chip clicks or toggle changes). The search input is a **passive field** that rides along on every form submission.

**Date input** (line 546–547, 551–552):
```html
<input x-ref="after" type="date" name="date_after" value="{{ date_after }}"
       @change="submit()">
```
Change on date input calls `submit()` (line 515), which calls `form.requestSubmit()`.

**Project/Label search** (line 425, 572):
```html
<input class="acta-flt-input" type="search" x-model="q" ...>
```
**Client-side only** — no trigger. The `x-model="q"` binding drives the `:class` conditional at line 435 (`hidden` if not matching). No server call.

**Reset button** (line 212–221):
```html
<button ... @click.prevent.stop="
  const form = document.getElementById('filter-form');
  window.dispatchEvent(new CustomEvent('acta:filter-reset'));
  $nextTick(() => form.requestSubmit());
">
```
Dispatches `acta:filter-reset` event (listened by every chip's `@acta:filter-reset.window="state = 'none'"`), then submits the form once. All chips reset to `'none'` state, form re-serializes, HTMX fires.

### Debouncing

**Search input**: No debouncing on `@change` or `@input` — the search input has no direct trigger. It's only submitted when another filter changes (chip click, toggle, etc.) or when the user submits the form explicitly. This is **correct**: the search field is not meant to trigger a live search; it's part of the form's overall filter payload.

**Project/Label search inputs** (lines 425, 572): No debouncing — these are client-side filters (Alpine `x-model`), not server calls.

### Out-of-band swap usage

**Count badges** (included at line 241 from `_filter_count_badges.html`):
- The badge is included twice in the sidebar: collapsed variant (not in main template) and expanded variant (line 241).
- On HTMX response, the view re-renders both badge variants with `oob=True`, telling HTMX to swap them out-of-band by id.
- **Finding verified**: Correct pattern; badges stay in sync without being the swap target.

### Preserved parameters

Lines 106–108:
```html
{% for name, value in filter_preserved_pairs %}
<input type="hidden" name="{{ name }}" value="{{ value }}">
{% endfor %}
```
Hidden inputs for parameters that should survive filter updates (e.g., pagination, workspace context). Correctly wired; no issues.

---

## 4. Cross-Template Structure Repetition (Extract Candidates)

### Shared component patterns

**Observation**: The sidebar uses **custom domain-specific classes** (`acta-flt-*`) that do not appear in any other template.

```bash
acta-flt-aside-v2      # Top-level sidebar container
acta-flt-rail          # 48px rail strip
acta-flt-rail-btn      # Rail button (icon + indicator)
acta-flt-content       # Popover/panel container
acta-flt-expanded-h    # Expanded header with count + reset
acta-flt-content-b     # 2-column grid inside content
acta-flt-sec           # Filter section card
acta-flt-sec-h         # Section header (icon + label + count)
acta-flt-sec-row       # Row within status/priority/size sections
acta-flt-input-wrap    # Icon + input wrapper (search, project search, label search)
acta-flt-input         # Text/date/select input
acta-flt-scroller      # Scrollable container (project list, label groups)
acta-flt-chip          # Tri-state chip (status, priority, size, cycle)
acta-flt-row           # Row-style entry (project, assignee)
acta-flt-cap           # Section caption (label group header)
acta-flt-toggles-foot  # Toggles footer container
acta-flt-tg            # Toggle switch pseudo-checkbox
```

**Scan results**: None of these classes appear outside `_filters_sidebar.html`. The sidebar is **self-contained**.

### Repeating micro-patterns

**Pattern 1: Section card** (lines 275, 294, 337, 376, 411, 471, 512, 558, 625)

```html
<div class="acta-flt-sec [acta-flt-sec-row] [span2]" data-section="..." [x-data="{...}"]>
  <div class="acta-flt-sec-h">
    {% lucide "icon" "..." %}
    <span class="lbl">{% trans "Label" %}</span>
    <span class="grow"></span>
    {# Optional count badge #}
  </div>
  {# Content: chips, rows, inputs, scrollers #}
</div>
```

Appears 9 times with variations:
- **Simple** (Search, Toggles): section header + single input.
- **Chip rows** (Status, Priority, Size): section header + n chips in flex wrap.
- **Row list** (Project, Labels): section header + optional search + scrollable list.
- **Date**: section header + dropdown + buttons + date inputs.
- **Cycle**: section header + chips in flex wrap (include-only).

**Could be extracted to a partial** `_filter_section.html` with:
- Slot for icon, label, optional count
- Slot for content
- Modular `data-section` and CSS classes

**Cost of extraction**: 1 new partial file + 9 includes + complexity of conditional rendering (e.g., `acta-flt-sec-row`, `span2` modifiers).

**Benefit**: ~40 LOC saved in `_filters_sidebar.html`; improved consistency; easier to update section styling in one place.

**Recommendation**: **Medium-priority refactoring candidate** for a future Wave. Not a blocker; current inline structure is readable and the repetition is minimal (class names are stable). **Reserve for R1 or later** (Wave 1 backlog). If extracted, validate that the partial's complexity doesn't exceed the mainline code's clarity.

**Pattern 2: Tri-state chip** (Status, Priority, Size, Labels)

```html
<label class="cursor-pointer"
       data-filter-name="..."
       data-filter-value="..."
       x-data="{ state: '...' }"
       @acta:filter-reset.window="state = 'none'"
       @contextmenu.prevent="state = state === 'excluded' ? 'none' : 'excluded'; ...">
  <input type="checkbox"
         :name="state === 'excluded' ? 'x...' : '...'"
         value="..."
         :checked="state !== 'none'"
         @change.stop="state = ...; ...">
  <span class="acta-flt-chip" :class="{ 'is-include': state === 'included', 'is-exclude': state === 'excluded' }">
    {# Icon/dot + label #}
  </span>
</label>
```

Appears 8 times (Status ×7, Priority ×5, Size ×5, Labels ×n). **Strong candidate for extraction**.

**Could be extracted to a macro or component** — but Alpine + HTMX does not have built-in templating for components. Current approach (inline x-data + reactive :name) is **correct for the architecture**. Extraction would require either:
1. A custom Alpine component (`Alpine.component(...)`) — adds JS side-effect to the template.
2. A Django template tag — would need to emit the entire label/input/span block, losing readability.
3. Django include partial — possible, but would replicate the x-data logic multiple times (once per include call).

**Recommendation**: Keep inline. The pattern is clear and serves as documentation. Refactoring is not justified.

---

## 5. i18n Coverage

### Scan results

**All visible strings are wrapped** in `{% trans %}` or `{% blocktrans %}`:

- **Section labels** (11): Search, Status, Priority, Project, Cycle, Size, Date, Labels, Filters ✅
- **Button titles** (9): Expand filters, Search, Status, Priority, Project, Labels, Cycle, Size, Date, Backlog/Archived, Reset all, Collapse to rail ✅
- **Placeholders** (4): Search tasks, Search projects, Search labels ✅
- **Radio/select options** (6): Completed, Created, Updated, Start, End, Due ✅
- **Date presets** (4): 7d, 30d, Cycle, Clear ✅
- **Toggle labels** (2): Show backlog, Show archived ✅
- **Misc** (4): Active, Backlog, from, to, fibonacci ✅

**Untagged strings**: None detected.

**Potential issue**: Line 585, fallback label inside `|default:`:
```html
<span>{{ entry.group.name|default:_("Other") }}</span>
```
Uses `_("...")` (gettext function call) instead of `{% trans %}`. **This is correct in a template context** — `_()` is a function call that evaluates at render time; `{% trans %}` is a template tag. Both are valid; `_()` is the Django convention for non-literal fallback values.

**Conclusion**: i18n coverage is **complete and correct**. ✅

---

## 6. Accessibility Notes

### Gaps identified

**Missing `<label>` associations**:
- Chip inputs (lines 313, 355, 395, 443, 482, 492, 503, 598) use `class="sr-only"` to hide the checkbox, then wrap in `<label>` for click target. **This is correct** — the label's text is the visual content. ✅
- Search inputs (lines 282, 425, 572): Inputs are wrapped in `<label>`; no explicit `for=` attribute, but the icon + placeholder serve as labels. **Minor gap**: no `aria-label` or visible label text. Consider adding `aria-label="Search..."` or moving the icon inside a true `<label>`.
- Date inputs (lines 546, 551): Wrapped in flex containers with text labels. No `aria-label`. **Could improve** with `aria-label="From date"` / `aria-label="To date"`.
- Toggle inputs (lines 635, 649): Wrapped in `<label class="... flex">`. **Correct pattern**; the label text is visible and associated.

**Missing ARIA attributes**:
- **Role on buttons**: Rail buttons (line 115+) are `type="button"` with `title=` attributes; correct. No `aria-label` needed (title is present).
- **`aria-expanded` on collapsible sections**: The rail has `data-mode` and `data-open` attributes (CSS-driven visibility), but no `aria-expanded` on the buttons that toggle sections. **Recommendation**: Add `aria-expanded=":class={ ... }"` to rail buttons (lines 124, 143, 153, 164, 175, 185, 194, 202) so screen readers announce collapsed/expanded state.
- **`aria-pressed` on toggle buttons**: The Reset button (line 212) could have `aria-pressed=":class={ ... }"` to announce its active state (when filters are active).
- **`aria-live` on count badges**: The `.ind` elements (lines 128, 137, 138, etc.) update dynamically; adding `aria-live="polite"` would announce count changes.

**Keyboard navigation**:
- Tab order through chips and rows is **correct by default** (all interactive elements are in source order).
- **Right-click to exclude**: The `@contextmenu.prevent` handler on chips (line 312, 354, 394, etc.) adds a non-standard interaction (right-click cycles state). **Not discoverable without a tooltip or help text**. Consider adding a `title=` or keyboard shortcut (e.g., Shift+Click).
- **Enter to submit**: Date inputs have `@change="submit()"` (line 547, 552), not `@keyup.enter`. Users cannot submit by pressing Enter in the date field. **Minor issue** — most users will tab out, triggering `@change`. Recommend adding `@keyup.enter="submit()"` for consistency.

### Recommendation summary

**Medium-priority improvements** (Wave 4 territory, not blocking):
1. Add `aria-expanded` to rail section buttons to announce collapsed/expanded state
2. Add `aria-label` to unlabeled inputs (date, search)
3. Add `aria-live="polite"` to dynamically-updated badge counts
4. Document right-click behavior or add keyboard shortcut (Shift+Click to exclude)

**Current state**: Sidebar is **usable with screen readers** (inputs have visible labels via `<label>`, buttons have titles). Not fully accessible per WCAG 2.1 AA, but **no critical gaps**.

---

## 7. Off-Token Class Scan

### Tailwind arbitrary values detected

| Class | Line | Context | Token alternative |
|---|---|---|---|
| `text-[13px]` | 240 | Filters heading | `text-sm` (14px) or custom `text-xs` (12px) — 13px is odd |
| `text-[11px]` | 244 | Reset button label | `text-xs` (12px) |
| `text-[9px]` | 380 | "fibonacci" caption | `text-[9px]` is an outlier; no predefined token |
| `text-[10px]` | 545, 550 | Date labels (from/to) | `text-[10px]` — non-standard |
| `text-[12px]` | 633, 646 | Toggle labels | `text-sm` (14px) — 12px is slightly smaller |
| `max-h-[180px]` | 429 | Project scroller | Domain-specific; no token |
| `max-h-[260px]` | 576 | Label scroller | Domain-specific; no token |
| `bg-[#a5b4d9]` | 367 | Priority 4 color | Hardcoded hex; **off-token** |

### Issues

**F1 — Off-token color for Priority 4 (line 367)**

```html
{% elif key == 4 %}bg-[#a5b4d9]
```

The sidebar uses Tailwind's predefined color palette for Priorities 1–3 and `no-priority` (red-500, orange-500, amber-500, zinc-700), but Priority 4 is **hardcoded** `#a5b4d9` (a light purple-ish gray).

**Why this is a problem**:
- The hardcoded color is **not in the design system** (`docs/decisions/` or CLAUDE.md color tokens).
- If the design system's priority palette is updated, this color will be missed.
- No Tailwind purge risk (arbitrary values are always included), but **maintenance risk**.

**Evidence**: Line 367 in template; no matching token in `tailwind.config.js` or `static_src/css/main.css`.

**Recommendation**: Map Priority 4 to a named token (e.g., `bg-indigo-300`, `bg-violet-300`, or define `bg-priority-4` in main.css). **Effort**: 5 min. **Severity**: Low (visual only, no functional impact).

### F2 — Arbitrary text sizes (lines 240, 244, 380, 545, 550, 633, 646)

Seven instances of `text-[Xpx]` where X ∈ {9, 10, 11, 12, 13}. These are **micro-typography decisions** that should be defined in a design system token if repeated. However:
- **Usage**: Most are one-off (labels, captions). Not heavily reused across the app.
- **No purge risk**: Arbitrary values are always included.
- **Maintenance**: If a "small caption" style is needed across the app, extracting `@apply text-[9px] leading-tight;` to a utility class would centralize it.

**Recommendation**: **Do not refactor now**. If the same sizes appear in other templates (Wave 3 audit D2/D4 will reveal), consolidate them to named utilities then.

### F3 — Arbitrary max-height (lines 429, 576)

`max-h-[180px]` (project scroller) and `max-h-[260px]` (label scroller) are **section-specific constraints** (responsive: `lg:max-h-none` drops the limit on larger screens). These are **not** design tokens but **component sizing**.

**No action needed**. These are correct uses of arbitrary values for domain-specific layout.

### Tailwind content scan (Wave 1 PR-7 integration)

Wave 1 PR-7 added `dashboard.css` to Tailwind's content array so dashboard utility classes survive purge. The sidebar **does not include dashboard.css** and **does not use any custom CSS classes** — only Tailwind utilities (`flex`, `grid`, `text-*`, `bg-*`, etc.) and custom `acta-flt-*` domain classes defined in `main.css`.

**Verification**: Sidebar's custom classes are all prefixed `acta-flt-` or `acta-` (e.g., `cursor-pointer`, `sr-only`, `peer`, which are Tailwind core utilities). **No risk from PR-7**. ✅

---

## 8. Findings F1–F10

### F1. Tri-state chip architecture is correct (✅)

**Status**: No issue.

Every chip (status, priority, size, cycle-except-cycle, labels) uses a `state: 'none' | 'included' | 'excluded'` Alpine machine. The hidden input's `:name` toggles between include and exclude keys (e.g., `:name="state === 'excluded' ? 'xpriority' : 'priority'"`). Left-click (via checkbox `@change`) cycles `none → included → none`; right-click (via `@contextmenu.prevent`) cycles `none → excluded → none`. Form is submitted after every state change.

This **correctly mirrors the server-side tri-state protocol** (form data has both `priority=1` and `xpriority=2` keys, which are parsed independently in `apply_task_filters`).

**Evidence**: Lines 309–331 (status pattern); 352–372 (priority); 390–406 (size); 595–609 (labels).

### F2. Rail + expanded layout is CSS-driven, not JS-driven (✅)

**Status**: No issue.

The layout toggle is entirely CSS-based: `data-mode` and `data-open` attributes on the content container (line 230–231) drive visibility via `:data-mode="rail"` and `:data-mode="expanded"` selectors. Alpine state is **stored in the global `$store.filters`** (acta.js line 3049) and persists to localStorage. No repeated AJAX calls to fetch alternate layouts.

**Evidence**: Lines 230–231 for reactive attributes; acta.js line 3075–3081 for layout toggle logic.

### F3. Form is single-endpoint HTMX, not N concurrent requests (✅)

**Status**: No issue.

The form has one `hx-get="{{ filter_form_url }}"` attribute (line 100). Every filter change (chip click, toggle, search submit, date change) calls `form.requestSubmit()`, which triggers a single HTMX request with the entire form's serialized data. The response swaps into `{{ filter_htmx_target }}` (default `#task-list-wrapper`).

**Evidence**: Line 100 for single endpoint; lines 318, 360, 400, 448 for trigger pattern; acta.js line 117–125 for `removeFilter` helper that dispatches real `change` events for row updates.

### F4. Search input is passive, not live-searched (✅)

**Status**: No issue.

The sidebar search input (line 282–289) has **no explicit HTMX trigger**. It's a passive form field that submits when another filter changes (chip click, toggle) or when the user explicitly submits the form. **No live-search on keyup; no debouncing needed.**

Contrast: The project/label local search (lines 425, 572) uses Alpine `x-model="q"` for **client-side DOM filtering only** (not a server call). Correct separation of concerns.

**Evidence**: Line 282–289 for search input (no `@input` trigger); lines 435, 594 for `x-model` binding.

### F5. Per-chip x-data is not extracted (✅)

**Status**: No issue.

Each chip has an inline `x-data="{ state: '...' }"` block (1 LOC per chip). Extraction to `Alpine.component()` or a mixin would add JS-side complexity (register in acta.js, manage lifecycle) without improving template readability. The inline pattern is **clear and maintainable**.

**Evidence**: Lines 309, 352, 392, 436, etc. all follow the same pattern.

### F6. Priority 4 uses off-token color (⚠)

**Status**: Minor issue.

Line 367: `bg-[#a5b4d9]` is a hardcoded color not found in the design system. Priorities 1–3 use Tailwind named colors (red-500, orange-500, amber-500); Priority 4 stands out.

**Fix**: Map to a named color from Tailwind's palette (e.g., `bg-indigo-300`, `bg-slate-400`) or define `bg-priority-4` in main.css.

**Effort**: 5 min.

**Severity**: Low (visual only; no functional impact; Tailwind purge is unaffected).

**Recommendation**: Include in next CSS refactor or as a bug-fix commit.

### F7. Multiple arbitrary text sizes, no consolidation (ℹ)

**Status**: Observation, not an issue.

Lines 240, 244, 380, 545, 550, 633, 646 use `text-[9px]`, `text-[10px]`, `text-[11px]`, `text-[12px]`, `text-[13px]`. These are **one-off** sizes for specific UI elements (captions, labels, headings). Not heavily reused.

**No action now**. If Wave 3 D2/D4 audits reveal the same sizes used elsewhere, consolidate them to named utilities then (e.g., `@apply text-[10px] text-placeholder-foreground;` → `.text-caption` in main.css).

**Effort if needed**: Low.

### F8. Date inputs lack `aria-label` (⚠)

**Status**: Accessibility gap.

Lines 546–547 (date_after) and 551–552 (date_before) have no `aria-label` or visible labels. The inline text "from" and "to" (lines 545, 550) are hints, not `<label>` associations.

**Fix**: Add `aria-label="From date"` and `aria-label="To date"` to the inputs.

**Effort**: 2 min.

**Severity**: Low (screen reader users can infer from context, but explicit labels are clearer).

**Recommendation**: Include in next a11y polish.

### F9. Rail section buttons lack `aria-expanded` (⚠)

**Status**: Accessibility gap.

Lines 124, 143, 153, 164, 175, 185, 194, 202: Rail buttons toggle section visibility but have no `aria-expanded` attribute. Screen reader users cannot discover that these buttons expand/collapse panels.

**Fix**: Add `:aria-expanded="$store.filters.openSection === 'status'"` (etc.) to each button.

**Effort**: 10 min (9 buttons × ~1 min each).

**Severity**: Low (not blocking; buttons have titles).

**Recommendation**: Include in next a11y polish.

### F10. No debouncing on local searches (✅)

**Status**: Correct by design.

The project and label search inputs (lines 425, 572) use Alpine `x-model="q"` for client-side filtering. No server requests → no debouncing needed. Correct pattern.

**Evidence**: Lines 435, 594 for `:class` binding that hides/shows rows based on match.

---

## 9. Cross-Links to Related Audits

### Wave 2 C9 findings integration

**C9 F1 (HTMX wiring)**: D3 confirms the sidebar uses **single form + single endpoint**. Correct. ✅

**C9 F2 (Alpine state extraction)**: D3 verifies the ~50 LOC main aside x-data block is **appropriately scoped**. No extraction needed. ✅

**C9 F3 (Client-side search)**: D3 confirms project/label search is **Alpine x-model only**, not server calls. Correct. ✅

**C9 F4 (No direct template tag queries)**: D3 notes the sidebar does **not include `inline_static`** or consume query-issuing tags directly. Template tags are called by sibling templates (task rows, etc.), not the sidebar. ✅

**C9 F5 (Memoization appropriate)**: D3 does not re-audit context generation (already done in C9). Confirms the sidebar receives well-memoized context dicts. ✅

**C9 F6 (Test gap)**: D3 inherits the recommendation to add unit tests for `filter_sidebar_context` before any refactoring. No change from C9.

**C9 F7 (Search ILIKE)**: D3 does not audit the backend filter logic (already done in C9). Template-side observation: search input is passive; no live debouncing needed.

### Wave 1 PR-2 (WorkspaceMember.exists merge)

**D3 finding**: Sidebar does **not call `WorkspaceMember.exists()`**. The optimization lives in `resolve_active_workspace` (acta.js), which is memoised per request. No sidebar-specific impact. ✅

### Wave 1 PR-7 (Dashboard.css Tailwind purge)

**D3 finding F12**: Sidebar uses only Tailwind utilities and custom `acta-flt-*` classes (defined in main.css, not dashboard.css). **No purge risk** from PR-7's dashboard.css addition. ✅

---

## 10. Summary

### Green flags ✅

1. **Single HTMX form endpoint** — no spurious N concurrent calls per filter section.
2. **Tri-state chip architecture** correctly wired to form input name toggling (`priority` vs. `xpriority`).
3. **Per-chip x-data** is appropriately inlined; extraction not justified.
4. **Main aside x-data block** (~50 LOC) is well-factored and focused on UI badge management.
5. **Rail + expanded layout** is CSS-driven (data-mode/data-open attributes); no alternate AJAX fetches.
6. **Client-side search** (project/label filtering) is Alpine x-model only; no server calls.
7. **Reset mechanism** correctly broadcasts `acta:filter-reset` event to reset all chips.
8. **i18n coverage** is complete; all visible strings wrapped in `{% trans %}`.
9. **Form state** is accurately mirrored in FormData; no stale checkboxes.
10. **Custom domain classes** (`acta-flt-*`) are self-contained; no Tailwind purge risk.

### Cautions ⚠

1. **Priority 4 off-token color** (`bg-[#a5b4d9]`). Line 367. Fix: map to named Tailwind color. Severity: Low.
2. **Accessibility gaps**: Date inputs and rail buttons lack `aria-label` / `aria-expanded`. Severity: Low. Deferral: Wave 4.
3. **Arbitrary text sizes** (9–13px) not consolidated. Justifiable as one-offs; consolidate if repeated elsewhere. Severity: Observation.
4. **Repeating section card pattern** could be extracted to partial for DRY. Benefit: ~40 LOC saved. Cost: 1 partial + 9 includes + conditional rendering. Deferral: Wave 4 / R1.

### No changes needed (read-only audit)

The filter sidebar is **well-architected and correctly wired**. The template is readable, the HTMX patterns are sound, and Alpine state management is focused. Two minor visual/accessibility polish items (off-token color, aria labels) are deferrable to Wave 4. No bugs, no regressions, no critical performance leaks.

---

## Appendix A: Section-by-Section Pattern Breakdown

| Section | Pattern | Tri-state? | Server-rendered state | Notes |
|---|---|---|---|---|
| Search | Text input | — | value="{{ q }}" | Passive; submits with form |
| Status | Chips | Yes | `state` computed from `key in selected/excluded_statuses` | 7 chips; backlog rows conditional |
| Priority | Chips | Yes | `state` computed from `key in selected/excluded_priorities` | 5 chips; colors from design system |
| Size | Chips | Yes | `state` computed from `value in selected/excluded_sizes` | Fibonacci values; monospace |
| Project | Rows (searchable) | Yes | `state` computed from `p.id in selected/excluded_projects` | Sticky-stack tracking; client search |
| Cycle | Chips (include-only) | No | `on` boolean for each cycle | Active/concrete/backlog; no exclude |
| Date | Dropdowns + inputs | — | Selectors + date ranges server-rendered | Quick presets (7d/30d/Cycle); from/to inputs |
| Labels | Rows (searchable) | Yes | `state` computed from `label.id in selected/excluded_labels` | Grouped by label.group; color dots |
| Toggles | Checkboxes | — | Checked if `show_backlog` / `show_archived` | Both are boolean toggles; not tri-state |

---

## Appendix B: Alpine x-data Blocks Inventory

| Block | Lines | Size | State keys | Lifecycle |
|---|---|---|---|---|
| Main aside | 44–93 | ~50 LOC | counts, showBacklog, totalActive, refreshCounts, init | Init on mount; form submit listener |
| Per-chip/row (8×) | Various | 1 LOC each | state: 'none'\|'included'\|'excluded' | Server-rendered; reset on acta:filter-reset event |
| Project section | 411 | 1 LOC | q: '' | Client-side search binding |
| Date section | 513–519 | ~40 LOC | fmt, submit, setRange, lastDays, clear | Date range helper functions |
| Label section | 558 | 1 LOC | q: '' | Client-side search binding |
| Footer toggles (2×) | 628, 647 | 1 LOC each | — (no explicit x-data, pure HTML) | Native checkbox change handlers |

---

## Appendix C: HTMX Interaction Flowchart

```
User action                 → Alpine handler              → Form mutation              → HTMX trigger
─────────────────────────────────────────────────────────────────────────────────────────────────────
Click chip                  @change on input              state += 1; :name toggle    .requestSubmit()
Right-click chip            @contextmenu.prevent         state = 'excluded' / 'none'  .requestSubmit()
Click Reset button          @click.prevent               dispatch acta:filter-reset   .requestSubmit()
Click date preset (7d/30d)  @click on button             call setRange(from, to)     submit()
Change date field dropdown  @change on select            call submit()               .requestSubmit()
Change date input           @change on <input type=date> call submit()               .requestSubmit()
Type in project search      x-model binding              q = value (DOM filter only)  (none)
Type in label search        x-model binding              q = value (DOM filter only)  (none)
Toggle checkbox (backlog)   onchange attribute          .requestSubmit()            HTMX fires
─────────────────────────────────────────────────────────────────────────────────────────────────────
```

**Key insight**: Every user interaction that changes filter state **routes through a single HTMX form submit**. No scattered `hx-get` attributes on individual chips. Correct pattern.

---

**Audit status: Complete** — 661-LOC template audited; section inventory, Alpine state machines, HTMX wiring, i18n coverage, accessibility gaps, and off-token classes catalogued. 10 findings (F1–F10); 8 green flags, 3 cautions, 0 blocking issues. Cross-linked to Wave 2 C9 and Wave 1 PR findings. No changes required for production.

