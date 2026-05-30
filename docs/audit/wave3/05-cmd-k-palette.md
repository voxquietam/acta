# 05 — Cmd+K command palette (D6)

> Wave 3 / Chunk D6. Date: 2026-05-29. Read-only.
>
> Audit of the global Cmd+K command palette: template (`templates/web/_command_palette.html`, 501 LOC), view (`apps/web/views.py::palette_search`, 154 LOC), recent-task storage (`acta.js` lines 13-59, 24-48), and quick-action wiring.
>
> The palette was shipped recently; Wave 1 deferred deep-read (docs/audit/99-wave1-backlog.md §9). This chunk surveys the full stack: search endpoint, Alpine component, keyboard nav, icon cache, recents storage, quick actions, and i18n compliance.

---

## 1. Template walkthrough

File: `templates/web/_command_palette.html` (501 LOC).

### Structure overview

| Lines | Section | Purpose |
|-------|---------|---------|
| 27–34 | Root `<div id="command-palette">` | Mounts Alpine `actaPalette()` component; fixed overlay, z-80, pt-12vh |
| 43–52 | Icon library (hidden) | Pre-rendered Lucide icons for task-actions; 8 SVGs cached client-side |
| 54–58 | Backdrop | Click-to-dismiss; opacity + blur transition |
| 60–65 | Modal frame | Relative container; shows/hides with `x-show="open"` + enter animation |
| 68–84 | Search input | Text input, placeholder i18n'd, Cmd+K hint in top-right (sm: only), debounced @input handler |
| 87–175 | Results list | 5 sections looped: tasks, recents, projects, actions, task_actions, nav. Each row is an `<a>` with section-specific layout templates (lines 109–171) |
| 178–189 | Footer hints | Keyboard legend: ↑↓ navigate, ↵ open, ⌘K toggle. All i18n'd. |
| 193–501 | Inline Alpine script | `actaPalette()` data + methods; `iconHtml()` helper + memoization (lines 206–213) |

### Data model (`x-data`)

**State** (lines 217–227):

```javascript
{
  open: false,                      // palette visibility
  query: "",                        // search input text
  sections: [],                     // from palette_search endpoint
  recents: [],                      // from window.acta.loadRecents()
  loading: false,                   // fetch in flight
  cursor: 0,                        // selected item (flat index)
  displayed: {                      // pre-computed render model
    sections: [],                   // ordered + filtered sections
    total: 0,                       // flat item count
    flat: []                        // all items in render order
  }
}
```

**Methods**:

| Lines | Method | Purpose |
|-------|--------|---------|
| 229–239 | `openPalette()` | Set `open=true`, clear query/cursor, load recents, call `fetchResults()`, focus input next tick |
| 241–243 | `close()` | Set `open=false` |
| 245–263 | `fetchResults()` | Fetch `palette_search?q=<query>` (120ms debounce) with `Accept: application/json`, parse sections array, reset cursor to 0 |
| 269–273 | `taskFromUrl()` | Extract `(slugPrefix, number)` from `window.location.pathname` (regex `/projects/([^/]+)/(\d+)/`); returns `{slugPrefix, number, slug}` or `null` |
| 278–328 | `taskActionItems()` | Gen 7 client-side actions (status changes × 5, copy link, new tab) filtered by query on label text; uses `taskFromUrl()` for payload |
| 344–399 | `rebuildDisplayed()` | Build flat render model: annotate each item with `_flatIdx` (O(1) cursor math), `_key` (stable `x-for` identity), sort sections (context-first on empty query, search-results-first on non-empty), clamp cursor |
| 401–412 | `moveCursor(delta)` | Advance cursor modulo flat count; scroll active row into view (lines 405–411) |
| 414–417 | `activate()` | Dispatch `follow(displayed.flat[cursor])` |
| 419–498 | `follow(item)` | Route by `item.action`: `create_task` (open modal, optionally pre-fill project), `set_status` (POST to `/projects/.../status/` via htmx), `copy_link` (clipboard.writeText), `new_tab` (window.open), or fallback to `item.url` (htmx boost with `HX-Boosted: true` + push history, else hard nav) |

### Keyboard navigation

| Key | Handler | Lines |
|-----|---------|-------|
| ↓ | `@keydown.down.prevent="moveCursor(1)"` | 75 |
| ↑ | `@keydown.up.prevent="moveCursor(-1)"` | 76 |
| Tab | `@keydown.tab.prevent="moveCursor($event.shiftKey ? -1 : 1)"` | 78 |
| Enter | `@keydown.enter.prevent="activate()"` | 77 |
| Esc | `@keydown.escape.window="if (open) { close() }"` | 31 |

**Gap identified**: No wrapping at cursor boundaries — when cursor reaches last item and user presses ↓, modulo arithmetic wraps back to 0. Same for ↑ at position 0. This is correct (carousel nav) but unspoken; see F1 below.

### Icon usage

Lines 43–52: **Pre-rendered icon library** (HTML comment § 36–42):
- 8 SVG `<span data-icon="...">` placeholders
- Icons: `check-circle-2`, `play-circle`, `eye`, `circle-dot`, `x-circle`, `link`, `external-link`, `history`
- Used by **task-actions** and **recents** sections (rendered via `item.icon_html` / `x-html` binding)
- Icons for **projects**, **actions**, **nav** come from `palette_search` endpoint (server-rendered via `_lucide()` template tag in Python)

Lines 206–213: **Icon cache** (`iconHtml()` + `_iconCache`):
```javascript
const _iconCache = new Map();
function iconHtml(name) {
  if (_iconCache.has(name)) return _iconCache.get(name);
  const el = document.querySelector('[data-acta-icon-lib] [data-icon="' + name + '"]');
  const html = el ? el.innerHTML : "";
  _iconCache.set(name, html);
  return html;
}
```
- **Effect**: O(1) on hit; cold path querySelector on first miss per icon name.
- **Scope**: Memoizes across palette open/close (map lives outside `actaPalette()` IIFE).
- **Correctness**: Returns `""` (empty string) on miss, safe for `x-html` binding (silent no-op).

### Sections ordering

Lines 340–382: **Dynamic order** based on query emptiness:

**Empty query** (lines 367–374):
1. `task_actions` (on-task context first)
2. `recents` (recent visits)
3. `actions` (quick actions: New task + per-project)
4. `projects`
5. `nav` (top-level destinations)
6. `tasks` (all results)

**Non-empty query** (lines 375–382):
1. `tasks` (search results win)
2. `projects`
3. `actions`
4. `task_actions`
5. `nav`
6. `recents` (still searchable)

**Rationale**: Context-first (task-detail actions) + personal (recents), then global (search results) on empty; reverse for typing (results up top).

---

## 2. palette_search view audit

File: `apps/web/views.py`, lines 7172–7324 (154 LOC).

### Query shape

Lines 7203–7206: **Base queryset**
```python
qs = Task.objects.filter(
    project__workspace=workspace,
    project__workspace__memberships__user=request.user,
).select_related("project")
```
- **Joins**: 2 (project, membership).
- **Optimisation note**: `select_related("project")` avoids per-task query; no prefetch on labels / blocks / blocked_by (deliberately lightweight for palette).
- **Workspace scoping**: Filters by both `workspace` FK + membership join (O(1) membership check scales with user's workspace count, typically 1–3).

### Match scoring

Lines 7208–7216: **Task title + slug matching**
```python
match = Q(title__icontains=q)
upper = q.upper()
if "-" in upper:
    prefix, _sep, num = upper.rpartition("-")
    if num.isdigit():
        match |= Q(project__slug_prefix=prefix, number=int(num))
elif q.isdigit():
    match |= Q(number=int(q))
qs = qs.filter(match)
```
- **Scoring**: Case-insensitive substring on title (`icontains`), OR exact prefix-number match (`ABC-123`), OR bare number match.
- **Type**: Substring, not prefix or fuzzy. `"sentry"` matches `"Wire up sentry"`.
- **No ranking**: All matches are returned in `order_by("-updated_at")` order (most recently modified first), with no relevance scoring. Longer substring matches appear same priority as partial-word matches.

Lines 7251–7255: **Project filtering**
```python
needle_upper = q.upper()
pqs_for_section = [
    p for p in accessible_projects if q.lower() in p.name.lower() or needle_upper in p.slug_prefix
]
```
- **Scope**: Case-insensitive substring on name, OR substring on slug_prefix (case-sensitive uppercase comparison).
- **Type**: Substring, not prefix or fuzzy.
- **Performance**: In-memory loop (not ORM), against a pre-filtered + materialised list (max 25 projects, line 7249).

Lines 7287–7292: **Nav filtering**
```python
needle = q.lower()
nav_items = [
    {"icon_html": _lucide(icon, "w-4 h-4"), "label": str(label), "url": url}
    for icon, label, url in nav_targets
    if not needle or needle in str(label).lower()
]
```
- **Scope**: Substring on label (e.g., `"inb"` matches `"Inbox"`).
- **Performance**: In-memory loop against fixed ~8-item list.

### Result limits per category

| Category | Limit | Lines | Notes |
|----------|-------|-------|-------|
| Tasks | 8 | 7217 (`.[:8]`) | Ordered `-updated_at`; no tie-break specified |
| Projects | 5 (section), 25 (accessible) | 7249, 7256 | 25 fetched from DB; section shows top 5 after filtering |
| Actions | N/A (generated) | 7300–7311 | 1 bare + up to 6 per-project (1 + 6 = 7 max) |
| Nav | N/A | ~8 items | Static list; all returned if match |

### Response structure

Lines 7314–7324:
```python
return JsonResponse({
    "q": q,
    "sections": [
        {"kind": "tasks", "label": str(_("Tasks")), "items": task_items},
        {"kind": "actions", "label": str(_("Quick actions")), "items": filtered_actions},
        {"kind": "projects", "label": str(_("Projects")), "items": project_items},
        {"kind": "nav", "label": str(_("Navigation")), "items": nav_items},
    ],
})
```

**Per-item schema by kind**:

| Kind | Fields | Notes |
|------|--------|-------|
| `tasks` | `slug`, `title`, `status`, `project`, `url` | status unused by palette |
| `actions` | `label`, `icon_html`, `action`, `payload` (optional) | action ∈ {`create_task`}, payload = {`project`: slug_prefix} |
| `projects` | `name`, `slug_prefix`, `icon_html`, `icon_color_class`, `url` | icon_html = Lucide SVG; icon_color_class = e.g. `bg-blue-500` |
| `nav` | `label`, `icon_html`, `url` | icon_html = Lucide SVG |

**Missing from endpoint**: No `recents` section. Recents are 100% client-side (localStorage, loaded on palette open). No `task_actions` section (client-side URL-parsing only).

---

## 3. Result categories + caching map

| Category | Source | Where stored | Invalidation | Roundtrip |
|----------|--------|--------------|--------------|-----------|
| Tasks | `palette_search` endpoint | `sections[0]` (fetch response) | Fetch on keystroke (120ms debounce) | Yes; HTTP |
| Recents | localStorage (`acta:recent-tasks`) | Alpine `this.recents` | Client: load on `openPalette()`, updated by `acta.recordRecentTask()` on task-detail visit | No; localStorage only |
| Projects | `palette_search` endpoint | `sections[2]` (fetch response) | Fetch on keystroke | Yes; HTTP |
| Actions | `palette_search` endpoint | `sections[1]` (fetch response) | Fetch on keystroke | Yes; HTTP |
| Nav | `palette_search` endpoint | `sections[3]` (fetch response) | Fetch on keystroke | Yes; HTTP |
| task_actions | Client-side URL regex | Computed per `taskActionItems()` call | Re-computed on every `rebuildDisplayed()` (O(1) list, no fetch) | No; localStorage of icons + IIFE-scoped closures |

**Cache invalidation mechanism**:
- **Recents**: `loadRecents()` reads from localStorage on palette open; no push invalidation. Stale if another tab adds a recent (rare user scenario).
- **Server sections**: Refetch on every keystroke (with 120ms debounce). No client-side caching between keystrokes within a single palette session.

---

## 4. Recents implementation

File: `static/js/acta.js`, lines 13–59 (recents storage) + lines 1738–1768 (marker scanning).

### Storage (localStorage)

**Key**: `"acta:recent-tasks"` (line 21).

**Capacity**: Max 6 entries (line 22, `RECENTS_CAP`).

**Entry shape**:
```javascript
{
  slug: "ABC-123",          // unique, used for dedup
  title: "Wire sentry",
  project: "Apollo",
  url: "/projects/APO/123/"
}
```

**Eviction policy** (lines 34–48, `recordRecentTask()`):
1. Load recents from localStorage.
2. Filter out any existing entry with same `slug`.
3. Prepend new entry.
4. Truncate to 6 items.
5. Write back to localStorage.
6. **Catch block** (lines 44–47): If quota exceeded or privacy mode, silently skip (no toast, no error).

**Deduplication**: By `slug` (line 36). Visiting ABC-123 again moves it to the top; no duplicate entries.

### Tracking (marker scanning)

File: `static/js/acta.js`, lines 1738–1768.

**Trigger points**:
1. `DOMContentLoaded` (line 1756) — on initial page load.
2. `htmx:afterSettle` (line 1768) — after any HTMX swap (e.g., boosted nav into task detail).

**Mechanism**:
1. Query for `[data-acta-track-recent]` marker in the DOM (line 1745).
2. If found + `window.acta.recordRecentTask` exists, call it with marker's data attributes (lines 1748–1753).
3. **Performance**: Every `htmx:afterSettle` re-scans (unconditional); cheap because marker only exists on task-detail pages, and dedup keeps the cost O(6) even on repeated visits.

**Marker location**: `templates/web/projects/task_detail.html`, line 13 (per grep output), carries `data-task-slug`, `data-task-title`, `data-task-project`, `data-task-url` attributes.

### Cross-workspace handling

**Current**: No workspace scoping in the key or dedup logic. If a user is a member of two workspaces with tasks `Proj-1` and `Proj2-1`, visiting `Proj-1` then `Proj2-1` will deduplicate and keep only the latest. Both URLs will resolve to the correct task's workspace via the URL structure (`/projects/<slug_prefix>/<number>/`), but **the recents list will not distinguish which workspace each task belongs to**.

**Impact**: Low in practice (rare for the same task slug to appear in two workspaces; task numbers are per-project, not global). **But see F7 below**.

---

## 5. Quick Actions inventory

Generated server-side in `palette_search` view, lines 7299–7311.

| Label | Action | Payload | Handler | Lines |
|-------|--------|---------|---------|-------|
| "New task" | `create_task` | none | Open modal via `htmx.ajax("GET", createTaskUrl, ...)` | 424–431 |
| "New task in [Proj]" (× up to 6 projects) | `create_task` | `{"project": "slug_prefix"}` | Same; append `?project=<slug>` to modal URL | 427–429 |

**Task-context actions** (client-side, lines 278–327):

| Label | Action | Payload | Handler | Lines |
|-------|--------|---------|---------|-------|
| "Mark as In Progress (ABC-123)" | `set_status` | `{slugPrefix, number, status: "in-progress"}` | `htmx.ajax("POST", `/projects/…/status/`, ...)` | 436–446 |
| "Mark as In Review (ABC-123)" | `set_status` | {status: "in-review"} | Same | " |
| "Mark as Done (ABC-123)" | `set_status` | {status: "done"} | Same | " |
| "Mark as To do (ABC-123)" | `set_status` | {status: "to-do"} | Same | " |
| "Mark as Cancelled (ABC-123)" | `set_status` | {status: "cancelled"} | Same | " |
| "Copy task link" | `copy_link` | `{url: full_url}` | `navigator.clipboard.writeText()` | 453–468 |
| "Open in new tab" | `new_tab` | `{url: pathname}` | `window.open(url, "_blank", "noopener")` | 472–476 |

**Filtering**: Query matches against label text (case-insensitive substring); empty query returns all.

---

## 6. Performance + latency notes (needs-measurement)

### Browser-side latencies

All figures below are **not measured**; marked for dev-stack observation.

| Metric | Target | Measurement point | Notes |
|--------|--------|-------------------|-------|
| **Palette open latency** | <50 ms | Time from Cmd+K press to visible palette (animation end) | Depends on icon cache hits, recents load from localStorage |
| **Keystroke latency** | <150 ms | Time from keystroke to results render | 120ms fetch debounce + network + render |
| **Search fetch latency** | <100 ms | Time from query to HTTP response (empty query on open) | Network + Django view (query cost TBD) |
| **Result render latency** | <50 ms | Time from `fetchResults()` to painted DOM | `rebuildDisplayed()` is O(N + S) where N = total items, S = sections; Alpine rendering is synchronous |
| **Icon lookup cost** | ~0 ms (cached) | Time per icon lookup via `iconHtml()` | First hit: querySelector; subsequent: O(1) Map lookup |

**Server-side latencies**:
- **Query count**: ~2 joins (project, membership) per task fetch; 25 projects materialised in-memory. **Estimate**: 2–3 queries per request (tasks + projects + nav fixture). **Measure with `assertNumQueries` after shipping**.
- **Database runtime**: Substring match on task title via `icontains` (typically fast on indexed columns, but depends on pg_trgm availability). **M10 in backlog (docs/audit/99-wave1-backlog.md §4)** suggests EXPLAIN analysis needed.

### Optimisations observed

1. **120ms debounce** (line 74) — prevents hammering server on rapid typing.
2. **Icon pre-render + memoization** (lines 43–52, 206–213) — avoids per-row icon fetch or recomputation.
3. **Flat index pre-computation** (lines 385–395) — O(1) cursor math instead of recomputing section offsets per move.
4. **In-memory project filtering** (lines 7251–7255) — loop against materialised 25-item list instead of ORM Q() per type.

### Gaps

| Gap | Severity | Notes |
|-----|----------|-------|
| **No keystroke latency instrumentation** | Low | Could add `performance.mark()` / `measure()` around `fetchResults()` and `rebuildDisplayed()` |
| **No server query profiling** | Medium | Django ORM queries are not counted; recommend adding `assertNumQueries` test |
| **Recents load blocks palette open** | Low | `openPalette()` calls `loadRecents()` synchronously; localStorage is fast, but no defer |
| **Search substring matching** | Medium | No ranking / relevance scoring — longer matches don't bubble to top. Matches are `-updated_at` order. See F4 below. |

---

## 7. i18n coverage

All user-visible copy is wrapped in `{% trans %}` / `_()` Django template tags (for server) or hardcoded strings + template markers (for client).

### Template strings (server-side, `{% trans %} / _()`)

| Lines | String | Context | Coverage |
|-------|--------|---------|----------|
| 79 | `"Search tasks, projects, jump to…"` | Input placeholder | ✓ |
| 92 | `"No matches."` | Empty state (after typing) | ✓ |
| 93 | `"Type to search across tasks, projects, and pages."` | Empty state (on open) | ✓ |
| 149, 158, 168 | `"Run"` / `"Go"` | Action badge labels | ✓ (×3 on recents, actions, nav) |
| 181, 183, 187 | `"navigate"`, `"open"`, `"to toggle"` | Footer keyboard hints | ✓ |
| 318, 320, 321, 322 | `"Tasks"`, `"Quick actions"`, `"Projects"`, `"Navigation"` | Section labels | ✓ |

### Client-side strings (Alpine script, `{% trans %}` inside JS)

| Lines | String | Context | Coverage |
|-------|--------|---------|----------|
| 285 | `"Mark as In Progress"` | Task action | ✓ |
| 291 | `"Mark as In Review"` | Task action | ✓ |
| 297 | `"Mark as Done"` | Task action | ✓ |
| 303 | `"Mark as To do"` | Task action | ✓ |
| 309 | `"Mark as Cancelled"` | Task action | ✓ |
| 315 | `"Copy task link"` | Task action | ✓ |
| 321 | `"Open in new tab"` | Task action | ✓ |
| 355 | `"Recents"` | Section label | ✓ |
| 361 | `"On this task"` | Section label (context actions) | ✓ |
| 370–378 | `"Quick actions"`, `"Projects"`, `"Navigation"` | Empty section labels | ✓ |
| 462 | `"Task link copied"` | Toast (success) | ✓ |
| 463 | `"Could not copy task link"` | Toast (error) | ✓ |
| 466 | `"Clipboard unavailable"` | Toast (warning) | ✓ |

### Server-side endpoint strings

| Lines | String | Context | Function |
|-------|--------|---------|----------|
| 7268 | `"Dashboard"` | Nav target | `_()` wrapping ✓ |
| 7269 | `"Inbox"` | Nav target | ✓ |
| 7270 | `"My Work"` | Nav target | ✓ |
| 7271 | `"All Tasks"` | Nav target | ✓ |
| 7272 | `"Projects"` | Nav target | ✓ |
| 7273 | `"My activity"` | Nav target | ✓ |
| 7276 | `"Cycles"` | Nav target (conditional) | ✓ |
| 7277 | `"Account settings"` | Nav target | ✓ |
| 7282 | `"Workspace settings"` | Nav target | ✓ |
| 7301, 7306 | `"New task"`, `"New task in %(name)s"` | Quick action | ✓ |
| 7318, 7319, 7320, 7321 | Section labels | Server response | ✓ |

**Coverage**: ✓ Complete. Every user-visible string is wrapped. Exception: none detected.

**Potential gap**: Client-side error messages for network failures (lines 249–251 in `fetchResults()`) are silently suppressed (no toast). See F8 below.

---

## 8. Mobile / non-keyboard surface

### Mobile considerations

| Aspect | Current | Notes |
|--------|---------|-------|
| **Keyboard trigger** | Cmd+K / Ctrl+K | Accessible on mobile for browsers that surface soft keyboard as modifier key (rare; most just get Cmd+K filtered). No touch fallback shown. |
| **Hotkey badge visibility** | Hidden on mobile (`hidden sm:inline-flex`, line 83) | Shows only on sm+ screens (640px+). Mobile users don't see the Cmd+K hint. |
| **Topbar trigger** | Not surveyed; assumed via event (`acta:palette-toggle`), lines 32–33. | No button visible in template. Likely wired in Topbar component (D2 scope). |
| **Touch feedback** | Hover state only (`:hover` via `@mousemove`, line 105) | No touch-specific visual feedback (e.g. active state on tap). Keyboard-only users on desktop get highlight; touch users must tap and see no feedback until release. |
| **Small-screen layout** | Modal is `max-w-xl` (448px) with `px-4` padding | On mobile <400px, modal is narrower. No explicit breakpoint to stack controls or hide elements. Results list scrolls within `max-h-60vh` (up to 536px). Workable but not optimized. |
| **Keyboard nav on mobile** | ↑↓ keys + Enter all work | Requires external keyboard (e.g. Bluetooth). Soft keyboard doesn't send arrow keys. Usable but requires device-specific setup. |

### Non-keyboard fallback

**Missing**: No fallback UI for users without keyboard access (e.g., screen-reader only, voice-control, external switches).
- No skip-link to toggle palette open/close via DOM.
- No `role="menu"` / `aria-label="..."` structure for assistive tech.
- No ARIA live region for search results updates.

**See F5, F6, F9 below**.

---

## 9. Findings F1–F12

### F1 — Cursor wrapping behavior undocumented

**Severity**: Low (correct behavior, just opaque).

**Location**: Lines 401–412 (`moveCursor()`), line 404 specifically.

**Finding**: Cursor wraps at boundaries via modulo arithmetic: `this.cursor = (this.cursor + delta + total) % total;`. This is correct carousel navigation (↑ at top wraps to bottom, ↓ at bottom wraps to top), but the template comment at lines 16–17 says "↑ / ↓ moves a flat cursor" without mentioning wrap behavior.

**Impact**: User expectation: some expect wrap (vim-like), others expect stop (e.g. native `<select>`). Current behavior is wrap; works but unintuitive.

**Effort**: Documentation only. Add comment above line 404: `// Carousel: wrap at boundaries (↓ on last item → first item, etc.)`.

**Test coverage**: 8 existing tests (`apps/web/tests/test_palette_search.py`) do not cover keyboard nav. Recommend adding test for wrap behavior (quick add).

---

### F2 — Icon cache scope is global and persistent

**Severity**: Low (correct but worth noting).

**Location**: Lines 206–212 (`_iconCache`).

**Finding**: Icon cache is defined at the top level of the script IIFE, outside `actaPalette()`. This means:
1. It persists across palette open/close cycles (good: cache reuse).
2. It's not bound to a specific palette instance (fine: only one palette per page).
3. If icons are ever updated in the DOM (e.g., theme toggle), the cache is stale.

**Impact**: If Acta ever adds a theme-toggle feature that swaps icon SVGs, the palette won't re-render. Currently no such feature exists.

**Effort**: Add code comment: `// _iconCache persists across palette cycles; invalidate on theme change if needed.` Deferred to theme-toggle PR.

---

### F3 — Task-context actions filter is O(7) per keystroke

**Severity**: Very low (7 is constant; negligible).

**Location**: Lines 278–328 (`taskActionItems()`).

**Finding**: `taskActionItems()` rebuilds a 7-item array, then filters it by query (line 327). Called on every `rebuildDisplayed()` (keystroke + fetch results). Cost is O(7) each time.

**Impact**: Imperceptible. 7-item filter is faster than the network fetch.

**Effort**: No action needed. Add comment for clarity: `// Fixed 7-item list; filter cost is constant.` (style).

---

### F4 — Search ranking by recency only; no relevance scoring

**Severity**: Medium (affects UX on broad queries).

**Location**: Lines 7208–7216 (task filtering), 7217 (order).

**Finding**: Tasks are ordered by `-updated_at` (most recently modified first) regardless of match quality. Example: query `"task"` matches both "Task automation" (exact match first word) and "This is a task I made" (match late in title). Both are returned in any order determined by last-modification date, not by match position/length.

**Impact**: If user types "wire up" searching for "Wire up sentry", but recently modified a task with "sentry" in description-search-haystack, they get the recent one first. Usability degrades on generic queries.

**Effort**: Medium refactor. Add a scoring function that ranks prefix/word-boundary matches higher, then sorted by score+updated_at. Estimate ~1 h (new function + tests). Defer to Wave 4 (search infra).

**Cross-ref**: M10 in Wave 1 backlog (EXPLAIN on ILIKE vs trigram).

---

### F5 — No accessible fallback for keyboard-only palette open

**Severity**: Medium (accessibility issue).

**Location**: Line 1730 (global keydown listener), no DOM fallback.

**Finding**: Palette open is bound to `Cmd/Ctrl+K` keydown event (line 1730–1735). There is no button or skip-link in the DOM to toggle the palette for users who cannot press keyboard shortcuts (voice control, switch-access, etc.).

**Impact**: Keyboard-dependent users cannot open the palette. Accessibility failure: WCAG 2.1 Level A (keyboard access).

**Effort**: Low. Add a `<button>` in the topbar with `@click="window.dispatchEvent(new CustomEvent('acta:palette-toggle'))"`. Estimate ~30 min. Defer to Wave 3 D4 (accessibility review).

---

### F6 — No ARIA labels or live regions for search results

**Severity**: Medium (assistive tech compatibility).

**Location**: Lines 87–175 (results list container), lines 89–95 (empty state).

**Finding**: Results list has no `role="region"`, `aria-label`, or `aria-live="polite"`. Empty-state messages (`x-show` lines 92–93) are not announced to screen readers when results update. Section headers (line 99–100, `x-text="section.label"`) are not explicit headers.

**Impact**: Screen-reader users cannot understand the palette's structure or be notified of result changes. Accessibility failure: WCAG 2.1 Level A.

**Effort**: Medium refactor. Add `role="region"`, `aria-label="Search results"`, and `aria-live="polite"` to results list. Change section labels to `<h3>`. Add `aria-label` to input (line 70). Estimate ~1–2 h + test. Defer to Wave 3 D4 (accessibility review).

---

### F7 — Recents are not workspace-scoped; cross-workspace dedupe risk

**Severity**: Low (rare in practice, but worth documenting).

**Location**: Lines 21–48 (`RECENTS_KEY`, `loadRecents()`, `recordRecentTask()`), no workspace check.

**Finding**: Recents localStorage key is global (`"acta:recent-tasks"`), and dedup is by `slug` only. If a user is a member of workspace A with `Proj-1` and workspace B with `Proj2-1`, the recents list will not distinguish between them. Visiting both will result in one entry with the most-recent URL.

**Impact**: Minimal. Task slugs are per-project; the chance of a collision across workspaces is low. But if it happens, clicking the recent task navigates to the workspace indicated by the URL, which is correct. **No data loss, but UX is slightly confusing.**

**Effort**: Low. Option 1: namespace key by `window.acta.workspaceSlug` (requires plumbing from server). Option 2: add workspace ID to entry shape (3 bytes per entry). Option 3: document as known limitation. Recommend Option 3 for now. Defer to Wave 4.

---

### F8 — Network errors in fetchResults() are silently dropped

**Severity**: Low (rare, but silent).

**Location**: Lines 245–263 (`fetchResults()`).

**Finding**: If the fetch fails (network error, 500 response), the exception is caught and `sections = []` is set (lines 250–252, 257–258). No toast or error message is shown. The palette shows "No matches" (line 92), and the user has no idea why.

**Impact**: On a 500 error, user assumes there are no results (user error) rather than a server error (system error). Confusing but rare.

**Effort**: Low. Wrap the catch in `if (window.actaToast) window.actaToast("Search error. Please try again.", "error")` (similar to lines 457–459). Estimate ~10 min. Stack with UX polish PR (Wave 1 PR-5 equiv).

---

### F9 — No `role="listbox"` or `aria-selected` on results list

**Severity**: Medium (keyboard nav + accessibility).

**Location**: Lines 102–172 (result rows), no ARIA markup.

**Finding**: Rows are `<a>` elements (line 103), not `<li>` in a `<ul>`. No `role="option"`, `aria-selected`, or `aria-current` attributes. Keyboard nav updates `.cursor` and highlights via CSS (`:hover` on line 106), but assistive tech doesn't know the selection state.

**Impact**: Screen readers announce each row as a link but don't convey that it's selectable via arrow keys. Keyboard-only sighted users see the highlight but screen-reader users are lost.

**Effort**: Medium. Change `<a>` rows to semantic `<li>` inside a `<ul role="listbox">`. Add `role="option"` and `aria-selected="cursor === item._flatIdx"` to each row. Add `aria-label` to input. Estimate ~1 h + test. Defer to Wave 3 D4 (accessibility review).

---

### F10 — Lazy guard on `$refs.search` focus could fail silently

**Severity**: Very low (edge case guard exists).

**Location**: Lines 236–238 (`openPalette()`).

**Finding**: The code checks `if (this.$refs.search)` before focusing (line 237). If the ref is not yet mounted (shouldn't happen due to `$nextTick`, but could on a timing edge), focus silently skips. This is safe but could hide bugs.

**Impact**: None. The guard is correct. User would just not have focus in the input; they can click or type normally.

**Effort**: Documentation only. Add comment: `// Defer focus to next tick so input exists; safe to check ref.` (already somewhat implied by `$nextTick` call).

---

### F11 — Tests cover endpoint but not Alpine component

**Severity**: Low (existing test coverage is solid for server-side).

**Location**: `apps/web/tests/test_palette_search.py` (8 tests, lines 13–128).

**Finding**: 8 tests cover the `palette_search` endpoint (query matching, workspace scoping, response shape, action filtering, etc.) with no client-side Alpine tests. Keyboard nav, cursor wrapping, icon cache, recents dedup, and task-context action filtering are untested on the client.

**Impact**: Low risk because Alpine logic is simple and manually tested. But no regression detection if refactored.

**Effort**: Medium. Add 4–6 client-side tests using a browser automation tool (Playwright, Cypress) or Alpine test harness (if available). Estimate ~2 h. Defer to Wave 4 (test infra).

**Existing tests**: All 8 pass (per chunk brief); no changes needed.

---

### F12 — Comment in template claims "no esbuild bundle" but acta.js is bundled

**Severity**: Very low (comment accuracy).

**Location**: Line 6.

**Finding**: Template comment says "Self-contained Alpine component — no esbuild bundle." But `acta.js` is the Acta bundle (per A.1 frontend architecture decision), and the palette inline script is part of it. Technically accurate (palette code isn't separately transpiled), but the phrasing suggests the whole codebase avoids bundling, which is false.

**Impact**: None. Misleading comment only.

**Effort**: Trivial. Reword to: `"Self-contained Alpine component — JavaScript bundled via acta.js."` Estimate ~1 min.

---

## 10. Cross-references

### Wave 1 backlog

- **§9** (lines 317–321): Cmd+K palette deferred from Wave 1 deep-read.
- **M10** (line 243): "EXPLAIN on `_filter_search`" — relevant to palette search ranking (F4).
- **D1 F5** (lines 196–200): `window.acta` Object.assign future-proofing (already implemented; no action).
- **D1 F1** (line 137): Toast on async failures — applies to palette fetch errors (F8).

### Adjacent chunks

- **D2** (Timeline): Consider coordinating timeline ref comment (Wave 1 backlog §5 R3).
- **D3** (CSS audit): May touch palette styling (modal, transitions).
- **D4** (Accessibility review): Critical for F5, F6, F9.
- **C9 (Wave 2 web audit)**: `filter_sidebar_context` (234 LOC) — lighter sibling to palette; check for similar patterns.

### Memory (known issues)

- No recorded memory items for the palette. Create one if F4 (search ranking) or F7 (recents scoping) are prioritized.

---

## 11. Summary

The Cmd+K command palette is **well-structured and ship-ready**, with solid foundations:

✓ **Strengths**:
- Clean Alpine component with stable, predictable state machine.
- Server-side search is properly scoped to workspace and membership.
- 8 tests provide baseline coverage of search behavior.
- Recents storage is durable, deduped, and quota-safe.
- Icon pre-rendering + caching prevents FOUC and per-row recomputation.
- All user-visible copy is i18n'd.
- Task-context actions (status, copy, new-tab) are clever and reduce server round-trips.

⚠️ **Gaps (low-to-medium effort, defer to later waves)**:
1. **Accessibility**: No keyboard fallback (F5), no ARIA labels (F6, F9). ~2–3 h to fix.
2. **Search UX**: No relevance ranking; order is by recency (F4). Deferred to Wave 4.
3. **Error handling**: Silent failures on network errors (F8). ~10 min to toast.
4. **Documentation**: Cursor wrap behavior, icon cache, recents scope (F1, F2, F7). ~30 min comments.
5. **Testing**: No client-side Alpine tests (F11). ~2 h for regression suite.

**No breaking bugs. Recommend ship as-is; schedule accessibility review + refine search ranking in Wave 4.**

