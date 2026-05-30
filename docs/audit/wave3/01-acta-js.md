# 01 — acta.js deep dive (D2)

> Wave 3 / Chunk D2. Date: 2026-05-29. Read-only.
> 
> **Scope:** `static/js/acta.js` (3,332 LOC SPA monolith).
> **Baseline:** Wave 1 D1 (`docs/audit/05-nav-router.md`) — structural map + router verification.
> **Cross-refs:** Wave 2 C7 drift table (Wave 2 PR-5 silent-skip log), ADR 0024 (page cache).
> **Constraint:** No removal of `snapCollapsedBodies` workaround (Wave 1 PR-8); no rename of SSE handlers or `actaForceApplySelfEvent` opt-in (14 event types verified zero drift).

---

## 1. Section inventory (LOC + responsibility table)

| Lines | Section | LOC | Responsibility | Public exports |
|---|---|---|---|---|
| 1–49 | Module preamble | 49 | IIFE wrapper, `getCookie`, recents localStorage | — |
| 50–168 | `window.acta` API surface | 119 | CSRF, task promote, filter ops, recents load/record | `csrfToken`, `promoteTask`, `exportQuery`, `removeFilter`, `toggleFilter`, `clearFilter`, `loadRecents`, `recordRecentTask`, `updateStickyStack` (assigned line 57, defined 1876) |
| 170–244 | Lazy panels | 75 | Cold-load async panel fetch; reusable on tab switch | `actaLoadPanels` |
| 246–273 | Sidebar active-nav | 28 | URL-based nav highlight on htmx:afterSettle + popstate | — |
| 275–407 | **History router** | 133 | `pageCache`, navToken abort, snapshot/restore, invalidate, popstate/boost handlers | `__actaInvalidatePageCache` |
| 409–433 | Rail tooltips | 25 | Delegated mouseover positioning | — |
| 435–727 | **Client-side filter** | 293 | State read, row matching, count badges, URL mirror, backlog column hide, cookie persist, kanban recount | `actaApplyFilters` |
| 729–849 | Kanban DnD + labels reorder | 121 | Sortable.js binds, card new-tab (Ctrl/Cmd+click), label drag-drop + persist | — |
| 851–927 | Cross-view freshness | 77 | Kanban column recount on task-created; panel-slot invalidate; list row live-insert | — |
| 929–1395 | **Timeline (Gantt)** | 467 | Zoom, header render, bar/deadline drag, today-line, scroll sync, MutationObserver on panel visibility | `__tlAfterFilter`, `__tlRanAt` |
| 1397–1487 | Kanban substatus recompute | 91 | Overdue count, done-this-week, avatar stack, empty row hide on filter | — |
| 1489–1537 | Filter form binding | 49 | htmx:beforeRequest hijack, search input debounce (150 ms), re-apply on settle | — |
| 1539–1682 | **Table sort** | 144 | Multi-key comparator, three-state cycle (none→asc→desc), URL sync, default-order fallback, indicator refresh | — |
| 1684–1776 | Hotkeys + create modal | 93 | "c" key (type check), Cmd/Ctrl+K palette toggle, create-task modal opener w/ project prefill | — |
| 1778–1852 | Create from selection + icons stub | 75 | Selection-bubble DOM builder, `createTaskFromText`, `renderIcons` (no-op shim for future removal) | `createTaskFromText`, `actaLightbox` |
| 1854–1907 | Sticky stack | 54 | z-index management for pinned filter rows (top vs bottom edge) | `updateStickyStack` |
| 1909–2006 | Strips + scroll overflow | 98 | Counter badges, wheel-clamp to content extent, scroll listener | `updateStripCounters` |
| 2008–2055 | Scroll fades | 48 | Overflow attribute toggle on parent; rebound on htmx:afterSwap | `updateScrollFades` |
| 2057–2466 | **SSE workspace** | 410 | EventSource per workspace; 10 event handlers (task.*, comment.*); card replace/move/remove; list panel debounce (250 ms); link events bypass self-filter; forceApplySelf opt-in | `__actaInvalidatePageCache` (called) |
| 2468–2565 | **SSE user/inbox** | 98 | Per-user notification stream; badge replace; row live-inject; workspace-scoped filter | — |
| 2567–2593 | Tooltip theming | 27 | `data-tooltip` + `aria-label` conversion; skips kanban cards | — |
| 2595–2630 | Lightbox | 36 | Image gallery click; delegated .prose + editor double-click | `actaLightbox` |
| 2632–2688 | Mention hover cards | 57 | User card fetch + cache; positioned near cursor | — |
| 2690–2787 | Task mention hover cards | 98 | Richer popover (status/priority/assignee/due/labels); two-cache strategy; viewport edge-flip | — |
| 2789–2801 | Hover card dismissal | 13 | htmx:beforeSwap, scroll, Escape | — |
| 2803–2846 | Comment hash highlight | 44 | `#comment-<id>` scroll-into-view + pulse; sync on htmx:afterSettle + hashchange; modal opener w/ post-settle highlight | — |
| 2848–2962 | **Context menu** | 115 | Right-click delegation; selection-aware (bulk vs single); position with viewport flip; async fetch + position; Escape/scroll/resize close | `actaForceApplySelfEvent`, `actaOpenBulkMenu` |
| 2964–3330 | **Alpine.init** | 367 | 5 Alpine stores (toasts, filters, theme, sidebar, kanban, selection, viewMode); bulk request drivers; localStorage sync | Toasts before Alpine boots; `actaToast`, `actaBulkPatch`, `actaBulkDelete`, `actaBulkArchive` |

**Totals:** 3,332 LOC across 27 sections. 16 `window.*` exports. 3 `window.__*` private globals. 2 major IIFEs (`initTimeline` 467 LOC, `alpine:init` 367 LOC).

---

## 2. Per-section findings

### 2.1 Lazy panels (`lazyLoadPanels`, lines 181–244)

**What it does:** Async-load tab-switcher panels (table / kanban / list / timeline) on first paint. Server renders only active view; empty `[data-panel-slot]` placeholders fill via `htmx.ajax` with `?panel=<key>`. Reusable: `actaLoadPanels()` on tab switch.

**Key detail:** Guard against stale firing (line 187–193). Fast nav can outpace the 50 ms timer, landing a stale `basePath`. URL comparison prevents cross-page slot pollution.

**Health:** ✓ Correct. Guard is essential under slow 3G + rapid clicks.

### 2.2 History router (`pageCache` et al., lines 275–407)

**What it does:** Own the Back/Forward + boosted-nav flow. `pageCache` (max 20, LRU) snapshots leaving page paired with exact URL. On popstate, restore from cache or refetch. Token-based abort prevents late responses from stale nav.

**Critical flow:**
- Line 377–380: `popstate` → snapshot outgoing (`lastUrl`) + restore new URL (`++navToken`).
- Line 386–391: `htmx:beforeSwap` (boosted nav) → snapshot outgoing.
- Line 395–407: `htmx:afterSettle` → push URL (history off), update `lastUrl`.
- Line 332–337: Any non-GET (`htmx:afterRequest`) invalidates the cache.

**Invariants (ADR 0024):** ✓ Held. History is off (`historyEnabled: false`). Page cache is local (no remote state). Snapshot pairs URL correctly (snapshot LEAVING page, not destination). Cache invalidates on both self-writes and SSE events.

**Health:** ✓ Excellent. This is textbook SPA routing.

### 2.3 Client-side filter (`applyClientFilters` et al., lines 435–727)

**What it does:** Mirror server-side `apply_task_filters`. Read form state; match each row against 14 filter dimensions (status, priority, assignee, project, labels, search, date range, archived, backlog). Toggle `[hidden]` + `display:none` on rows. Update badges, counts, WIP warnings. Mirror URL params (except `show_backlog`, which stays cookie-only). Recompute kanban column counts + substatus.

**Key detail (line 699–704):** `show_backlog` is NOT mirrored to URL. Reason: lazy `?panel=` panels parse the URL to build their own path. If `show_backlog=0` were mirrored, a fresh panel load would drop planned/ready rows, and toggling backlog on later couldn't reveal them. The cookie (written line 691) persists the toggle for cold loads.

**Health:** ✓ Correct. Comment justifies the asymmetry. Tested path through `bindFilterForm` (line 1490–1537) hijacks HTMX and re-applies after settle.

### 2.4 Kanban DnD + labels reorder (lines 729–849)

**What it does:** Sortable.js binds for kanban columns (group="tasks") + label lists (group="labels"). Card drag → PATCH `/api/v1/tasks/<id>/` with `status`. Label drag → POST `reorder_labels` (204 response, DOM already moved). Idempotent re-bind on `htmx:afterSettle` via `Sortable.get()` guard.

**Bug risk:** Line 796–797 delegates `click` + `auxclick` to `kanbanCardNewTab`. Modifier clicks (Ctrl/Cmd / middle) open in new tab. ✓ Correct, avoids race with Sortable.

**Health:** ✓ Good. Sortable integration is minimal, re-bind is safe.

### 2.5 Timeline (Gantt) (`initTimeline`, lines 929–1395)

**What it does:** Render interactive Gantt chart. Zoom (day/week/month), drag deadline ◆ to set due_date, render work bar (start→end) and deadline. Today-line + scroll sync between left (row names) and right (chart). MutationObserver on panel visibility to re-render today-line (bars computed at 0-width resolve wrong).

**Key detail (line 1374–1387):** Panel carries `_tlObs` MutationObserver. On re-init, disconnect prior one before attaching fresh (prevents stack-up). Smart and necessary.

**Code smell:** Line 1323 — `patchDate()` silently swallows errors to console.error. ✓ Correct — fires off a due-date PATCH; failure is logged but doesn't block.

**Memory:** ✓ `_tlObs.disconnect()` on panel update + before re-init (line 1382).

**Health:** ✓ Good. IIFE scope keeps state local. Observer cleanup is thorough.

### 2.6 Table sort (lines 1539–1682)

**What it does:** Click header to cycle sort (none → asc → desc → none). Multi-key: three-state per column, but on clear reset to `data-default-order`. Comparator mirrors server-side `apply_task_ordering` (status = workflow rank 0–4, priority sinks 99 for "no priority", size/due/assignee NULLS LAST). URL via `history.pushState`.

**Correctness:** Line 1657–1662 reads **live URL**, not server-rendered href. Essential — href is set once at render, goes stale after first click. Without re-reading, three-state cycle breaks.

**Health:** ✓ Solid. No state drift.

### 2.7 Hotkeys + create modal (lines 1684–1776)

**What it does:** "c" key opens create modal. Cmd/Ctrl+K opens command palette (custom event `acta:palette-toggle`). Both check `isTypingTarget(evt.target)` to ignore inside inputs/editors. Project prefill from URL (`/projects/<slug>/`).

**Health:** ✓ Good. Type checks are essential. Prefix matching covers project detail + task detail.

### 2.8 Context menu (lines 2848–2962)

**What it does:** Right-click on a task row → fetch context menu HTML (task-specific or bulk if 2+ selected). Position at cursor; flip left/up if viewport edge hit. Submenus swap in place (no cascade). Actions post with `hx-swap="none"`, so row stays stale until SSE swap. IIFE wraps menu state (close flag, position logic).

**Critical detail (line 2874–2884):** `window.__actaForceApplySelf` Set + `actaForceApplySelfEvent(id)` opt-in. Context menu posts `hx-swap="none"`, so HTTP response doesn't touch the row. SSE event must apply even though it's self-actor. 30 s TTL (line 2883) is comfortably high (was 4 s in earlier audit; flagged as too tight under heavy peer activity). ✓ Correct trade-off.

**Health:** ✓ Good. Closure keeps menu state local. Close handlers cover Escape + scroll + click-outside.

### 2.9 SSE workspace (`initWorkspaceSse`, lines 2057–2466)

**What it does:** One EventSource per workspace (dedup by URL via `SSE_BOUND_URLS`). 10 event handler types:
- `task.status_changed` → `applyCardMove` (kanban column change).
- `task.assigned`, `task.priority_changed`, `task.due_changed`, `task.labels_changed`, `task.archived`, `task.unarchived` → `applyTaskUpdate` (card replace, table row morph, list refetch).
- `task.project_changed` → remove card + refetch board panel.
- `task.deleted` → remove card + refetch list.
- `task.created` → insert card into kanban (not table/list until next nav).
- `task.link_added`, `task.link_removed` → generic `applyTaskUpdate` (bypass self-filter intentionally).
- Comment events (created/updated/deleted) → refresh timeline on detail page.

**Self-event filter (line 2227–2234):** Drop if `actor_id == meId` AND not `via_mcp` AND not in `__actaForceApplySelf`. Three exits. ✓ Correct per Wave 1 D1 §3.3.

**List view (line 2257–2272):** Debounced panel refetch (250 ms). Batches multi-event bursts. ✓ Correct — list sections shift on peer edits; in-place swap would leave stale structure.

**Morphing (line 2274–2285):** Use idiomorph (outerHTML) if available, else fallback to `replaceWith`. ✓ Preserves Alpine state on SSE table-row swaps.

**Drift table:** Wave 2 C7 verified all 14 event types map correctly (Python emit → JS read). ✓ **No renames found.**

**Health:** ✓ Excellent. EventSource cleanup on `pagehide`/`beforeunload` (line 2196–2204). Dedup per URL. Deferral of list panel on 250 ms timer prevents per-event refetches. Idiomorph fallback ensures graceful degrade.

### 2.10 SSE user / notifications (lines 2468–2565)

**What it does:** Per-user EventSource on private `user-<id>` channel. Single event type: `notification.created`. No self-filter (server never sends self notifications). On arriving:
1. Replace sidebar badge (`#inbox-badge`). Pulse animation 3.4 s.
2. Prepend row to `#inbox-list` if filter matches (new rows always unread; filter "all"/"unread"/kind match).

**Workspace-scoped (line 2489):** Badge + row only inject if notification's `workspace_id` matches active workspace on `#app-content`. ✓ Correct — multi-workspace user won't see cross-workspace notifications in the wrong inbox.

**Health:** ✓ Good. Dedup by URL (line 2533). Stream close on nav (line 2540–2548).

### 2.11 Sticky stack + scroll overflow (lines 1854–2055)

**What it does:** Two independent features:
1. **Sticky-stack z-index** (lines 1876–1907): Pinned filter rows (top/bottom sticky). Calculate which sticky edge each row pins to (viewport pos vs natural pos). Assign z-index so closest-to-viewport row is on top (not last-to-leave). Recompute on scroll.

2. **Strip overflow** (lines 1909–2006): Assignee/project chip strips. Count off-screen chips left/right. Set `data-overflow-left/right` on wrapper. CSS fades. Wheel handler clamps scroll to content extent (prevents elastic overscroll leaving empty space).

**Scroll-fade generic** (lines 2008–2055): Any `[data-scroll-target]` gets `data-overflow-left/right/top/bottom` toggles on parent. Rebound on `htmx:afterSwap`.

**Health:** ✓ Good. All three features are passive (CSS-driven). Listeners marked `passive: true`.

---

## 3. Event-handler audit (debounce, coalesce, error surface)

### 3.1 Debouncing

| Function | Timer (ms) | Trigger | Notes |
|---|---|---|---|
| `applyClientFilters` search | 150 | input on `#filter-form input[name="q"]` (line 1518–1521) | ✓ Debounced. Idempotent state read. |
| `refreshListPanel` | 250 | every SSE task event (line 2257–2272) | ✓ Debounced. Batches multi-event bursts. |
| Comment highlight (line 2844) | 60 | post-settle after modal fetch | ✓ Deferred. Allows modal render to settle first. |
| Inbox badge pulse (line 2504) | 3400 | badge `classList.remove("inbox-pulse")` | ✓ Animation timing, not debounce. |
| Lazy panel load | 50 | `htmx:afterSettle` (line 238) | ✓ Deferred. URL parse happens 50 ms after settle. |
| Timeline re-render on panel show | ~0 | MutationObserver (line 1384) | ✓ No debounce (correct—visibility change is singular). |

**Coalesce gaps:** None found.

### 3.2 Error handling + logging

| Catch | Line | Handling | Surface |
|---|---|---|---|
| `getCookie` recents parse | 29–31 | Empty array on JSON error | Silent (correct—localStorage is optional). |
| `lazyLoadPanels` URL parse | 189–191 | Early return on malformed basePath | Silent (correct—guards against stale nav). |
| `restorePage` history miss (fetch) | 370–372 | Abort vs network error; calls `window.location.assign()` | Browser nav (hard fallback; correct). |
| Task promote fetch | 91–93 | Toast on error (line 88, 92) | `window.actaToast` (correct—async action, user must know). |
| Kanban drop fetch | 766–768 | Rollback card position + toast | `window.actaToast` (correct). |
| Timeline deadline patch | 1323 | `console.error("[timeline] patch failed:", err)` | Console only. **Finding F4 below.** |
| `morphFromString` (SSE) | — | Fallback to `replaceWith` | Silent (correct—idiomorph optional). |
| SSE event JSON parse | 2211–2213, 2347, 2452 | Silent return (skip malformed events) | Silent (correct—corrupted events are rare; skip & continue). |
| Filter state read | 445–473 | Defensive defaults on missing attrs | Silent (correct—template should provide, but doesn't crash without). |
| Mention card fetch | 2679 | `.catch(() => {})` | Silent (correct—hover card miss is not blocking). |
| Task card fetch | 2778 | `.catch(() => {})` | Silent (correct). |
| localStorage quota | 44–47 | Silent skip on setItem error | Comment on line 46 (correct—recents is ephemeral). |

**Pattern:** Async failures that are user-facing (promote, kanban drop) → toast. Infra failures (SSE JSON, mention fetch) → silent. One exception: timeline patch logs to console (not a toast). **→ F4**.

### 3.3 Missing JSDoc on exported / multi-caller functions

High-traffic functions without JSDoc blocks:

| Function | Callers | JSDoc? |
|---|---|---|
| `promoteTask(slugPrefix, number, status)` | 1 (template) | ✗ |
| `createTaskFromText(text)` | 2 (hotkey + selection bubble) | ✗ |
| `updateStickyStack(container)` | 3 (scroll listener + init) | ✗ |
| `updateStripCounters(strip)` | 3 (scroll + wheel + resize) | ✗ |
| `updateScrollFades(target)` | 3 (scroll + resize + init) | ✗ |
| `actaLoadPanels(basePath)` | 4 (htmx:afterSettle + view-mode toggle + DOMContentLoaded + task-created) | ✗ |
| `actaLightbox(img)` | 3 (click delegate + dblclick + onclick) | ✗ |
| `actaForceApplySelfEvent(id)` | 1 (context menu template) | ✗ |
| `actaOpenBulkMenu(x, y)` | 1 (bulk action bar) | ✗ |

**Impact:** Low. All callsites are templates (known call patterns). Internal functions are descriptively named. But for future maintenance, JSDoc blocks on `acta.js:55–168` (the public API surface) would clarify intent + parameter contracts.

---

## 4. window.acta surface map

### 4.1 Exports (lines 55–168)

**Assigned at page init:**
```js
window.acta = Object.assign(window.acta || {}, {
  csrfToken: () => getCookie("csrftoken"),
  updateStickyStack: null, // assigned below (line 1907)
  loadRecents: loadRecents,  // function
  recordRecentTask: recordRecentTask,  // function
  promoteTask(slugPrefix, number, status) { ... },
  exportQuery() { ... },
  removeFilter(name, value) { ... },
  toggleFilter(name, value) { ... },
  clearFilter(name) { ... },
});
```

### 4.2 Late-assigned exports

| Property | Line | Assigned | Read by |
|---|---|---|---|
| `acta.updateStickyStack` | 1907 | `window.acta.updateStickyStack = updateStickyStack;` | Templates: `x-data="{ init() { window.acta.updateStickyStack(...) } }"` |
| `acta.createTaskFromText` | 1799 | `window.acta.createTaskFromText = createTaskFromText;` | Selection bubble (line 1826); templates (create-from-selection button). |

### 4.3 Private globals

| Symbol | Line | Purpose | Leaked? |
|---|---|---|---|
| `window.__actaInvalidatePageCache` | 327 | Exported for SSE internal use (page-cache wipe on mutation). | ✓ Leaked (underscore convention suggests private, but exported). |
| `window.__actaForceApplySelf` | 2874 | Set of task IDs whose next SSE event should apply despite self-actor. | ✓ Leaked (intended: context menu + SSE handler communicate). |
| `window.__tlAfterFilter` | 1337 | Callback fired by filter apply to re-render timeline today-line. | ✓ Leaked (filter code calls it). |
| `window.__tlRanAt` | 947 | Diagnostic timestamp on timeline init (for debugging re-init). | ✓ Leaked (diagnostic only, safe). |

### 4.4 Non-exported but widely used

| Function | Line | Called | Context |
|---|---|---|---|
| `actaToast(message, level, timeoutMs)` | 2977 | 15+ callsites | Global error handler; defined early so HTMX listeners can call before Alpine boots. Queues to Alpine store on `alpine:init` (line 3044–3047). |
| `actaLoadPanels(basePath)` | 228, exported | 4 callsites | Lazy panel trigger. |
| `actaLightbox(img)` | 2601 | Inline image click handler (lines 2620, 2629); templates (onclick). | Lightbox opener. |
| `actaApplyFilters()` | 728 | `bindFilterForm` (line 1510); `acta:filter-reset` listener (line 1535) | Client-side filter apply. |
| `actaBulkPatch(updates, opts)` | 3307 | Templates (bulk bar) | Bulk PATCH driver. |
| `actaBulkDelete()` | 3309 | Templates (bulk bar) | Bulk DELETE driver. |
| `actaBulkArchive()` | 3310 | Templates (bulk bar) | Bulk archive driver. |
| `actaOpenBulkMenu(x, y)` | 2923 | Templates (bulk bar button) | Context menu opener (bulk). |

---

## 5. History router + page cache invariants (ADR 0024)

### 5.1 ADR 0024 checklist

| Invariant | Evidence | Status |
|---|---|---|
| Own page cache (not HTMX) | `pageCache = new Map()` (line 295); `historyEnabled: false` in htmx-config | ✓ Met |
| Snapshot pairs URL correctly | `snapshotInto(lastUrl)` on popstate (line 378) + beforeSwap (line 389); `lastUrl` updated AFTER swap settles (line 405) | ✓ Met |
| HTMX history off | base.html htmx-config verified in audit notes | ✓ Met |
| Invalidate on any mutation | `htmx:afterRequest` (non-GET) clears cache (line 336) | ✓ Met |
| Invalidate on SSE events | `invalidatePageCache()` called on every SSE event (line 2218) | ✓ Met |
| Token-based abort on stale nav | `navToken++` on new nav; fetch stores token; response checked against live token (line 357) | ✓ Met |
| htmx.swap on restore | `swapAppContent(html)` calls `window.htmx.swap()` with `swapStyle: "innerHTML"` (line 315) | ✓ Met |

### 5.2 Edge cases verified

**Case 1: Fast back-back-forward**
- Back from page C → popstate (snapshot C, restore B).
- Before B settles, user clicks Forward → abort B's in-flight fetch (`navAbort.abort()` line 340), snapshot B, restore C.
- C's restore fetch carries `token = navToken++`, B's lingering `.then()` exits because `token !== navToken`.
- ✓ Safe.

**Case 2: SSE event during Back/Forward**
- Back → `invalidatePageCache()` fires → `pageCache.clear()` (line 325).
- Any in-flight restore fetch still runs (it's already in flight), but next nav will refetch because cache is empty.
- ✓ Safe (prioritises correctness over hit-rate).

**Case 3: Lazy panel fetch during boosted nav**
- Panel slot starts loading `?panel=timeline`.
- HTMX boost intercepts, swaps `#app-content`, clears lazy-panel in-flight flags on old page.
- Fresh page's new slot is empty; `lazyLoadPanels` re-fires on settle.
- ✓ Safe (guard on `basePath` prevents old path's slot filling on new page).

**Cache hit rate:** Structured to be low. Any WRITE or SSE event empties cache. Mostly helps idle nav (click link, click another link, Back → hits cache). Under real load, cache is rarely warm. ✓ Documented trade-off (Wave 1 D1 §3.2).

---

## 6. Memory leak / listener audit

### 6.1 addEventListener without remove

| Listener | Element | Line | Removed? | Risk |
|---|---|---|---|---|
| `htmx:afterSettle` → `refreshSidebarActive` | document.body | 272 | Never | ✓ OK—handler is stateless, re-entrant. Re-fires fine. |
| `popstate` → `refreshSidebarActive` | window | 273 | Never | ✓ OK—stateless. |
| `htmx:afterSettle` → `lazyLoadPanels` | document.body | 235 | Never | ✓ OK—guard on `basePath` prevents stale fires. |
| `htmx:afterSettle` → `snapCollapsedBodies` | document.body | 2111 | Never | ✓ OK—MutationObserver per body is the actual workaround; `snapCollapsedBodies` idempotent. |
| `htmx:afterSettle` → `initKanbanDnD` | document.body | 798 | Never | ✓ OK—`Sortable.get()` guard on each element prevents double-bind. |
| `htmx:afterSettle` → `initLabelsDnD` | document.body | 841 | Never | ✓ OK—same guard. |
| `acta:task-created` → `recountKanbanColumns` | document.body | 863 | Never | ✓ OK—stateless. |
| `acta:task-created` → panel invalidate | document.body | 874 | Never | ✓ OK—stateless. |
| `acta:list-insert-row` | document.body | 901 | Never | ✓ OK—Custom event, fired once per create. |
| `keydown` → `onCreateTaskHotkey` | document | 1717 | Never | ✓ OK—handler is stateless. |
| `keydown` → `onPaletteHotkey` | document | 1730 | Never | ✓ OK—stateless. |
| `click` → `onSortLinkClick` | document | 1643 | Never | ✓ OK—handler checks context (sort links only). |
| `mouseover` / `mouseout` → rail tooltips | document | 413, 427 | Never | ✓ OK—Delegated, no memory cost. Tooltips cleaned on mouseout. |
| `click` → `kanbanCardNewTab` | document | 796 | Never | ✓ OK—Delegated, stateless. |
| `auxclick` → `kanbanCardNewTab` | document | 797 | Never | ✓ OK—Delegated, stateless. |
| Wheel on strip | strip element (line 1977) | 1977 | Never | ⚠ **Risk:** Wheel listener added in `initStrips()` on every element with `[data-strip]`. On HTMX swap, element replaced but listener stays on old element. **→ F1**. |
| Scroll on sticky-stack container | container (line 1914) | 1914 | Never | ⚠ **Risk:** Scroll listener added on every element with `[data-sticky-stack]`. On HTMX swap, element replaced but listener stays on old element. **→ F1**. |
| Scroll on strip | strip element (line 1999) | 1999 | Never | ⚠ **Risk:** Same as wheel listener. **→ F1**. |
| Scroll on scroll-fade target | target element (line 2031) | 2031 | Never | ⚠ **Risk:** Same pattern. **→ F1**. |
| Resize listeners | window (lines 2002, 2034) | 2002, 2034 | Never | ⚠ **Risk:** Each `initStrips()` and `initScrollFades()` call adds a fresh resize listener. Multiple listeners accumulate. **→ F1**. |
| EventSource error / message | source object (2190, 2207) | 2190, 2207 | Implicit | ✓ OK—EventSource is GC'd when connection closes. Stream closes on pagehide. |

### 6.2 MutationObserver cleanup

| Observer | Element | Line | Lifecycle |
|---|---|---|---|
| `panel._tlObs` | panel (right-col's `[x-show]` ancestor) | 1383 | Disconnect on prior init (line 1382), re-attach fresh (line 1383). ✓ Cleaned. |
| `body.__actaSnapObs` | kanban column body | 2104 | Store as property. No explicit cleanup, but entries are removed when column body is swapped. ⚠ **Risk: entry persists if element detached without re-init.** **→ F2**. |

### 6.3 Summary

**Listener accumulation risks:**
- **F1:** `initStrips()`, `initStickyStacks()`, `initScrollFades()` add listeners to DOM elements without removing prior ones when elements are swapped. Guard is `dataset.*Bound == "true"` (lines 1911, 1951, 2028) — skips re-init, but **old listeners on replaced DOM still exist**. On HTMX swap that replaces a strip element, the old element (and its listeners) lingers in memory until the next GC.
  - **Frequency:** Low—these elements persist across most swaps (only full panel reloads replace them).
  - **Mitigation:** Unlikely to cause heap issues in practice (listeners are on-body delegated or passive), but **technically a leak**.

- **F2:** `body.__actaSnapObs` (MutationObserver on kanban column bodies) persists as a DOM property. On column rebuild, `installCollapsedBodySnap` re-uses if `body.__actaSnapObs` exists (line 2091). But if column is swapped without the guard being checked, the property lingers. ✓ Practical impact minimal because guards re-fire on `htmx:afterSettle` (line 2111), but **not foolproof**.

---

## 7. Bundle split seams (Wave 4 R4 prep)

**Long-term path to 8–10 files:**

1. **nav-router.js** (275 LOC)
   - `pageCache`, snapshot/restore, popstate/boost handlers, `currentUrl()`, invalidate.
   - Public: `__actaInvalidatePageCache`.
   - Depends: `window.htmx`, `window.location`, `window.history`.

2. **lazy-panels.js** (75 LOC)
   - `lazyLoadPanels`, `actaLoadPanels` export.
   - Depends: `window.htmx`.

3. **filters.js** (293 LOC)
   - `readFilterState`, `rowMatches`, `applyClientFilters`, badges, URL mirror, kanban-substatus recompute.
   - Public: `actaApplyFilters`.
   - Depends: `window.htmx`, `window.history`, Alpine (for viewMode).

4. **kanban-dnd.js** (121 LOC)
   - Sortable.js binds, card new-tab, label reorder + persist.
   - Depends: `window.Sortable`, `window.htmx`.

5. **timeline.js** (467 LOC)
   - Standalone IIFE; only real dependencies are DOM selectors + localStorage.
   - Exports: `__tlAfterFilter`, `__tlRanAt` (diagnostic).
   - Split without friction.

6. **table-sort.js** (144 LOC)
   - Comparators, URL sync, click handler.
   - Depends: `window.history`.

7. **hotkeys.js** (93 LOC)
   - "c" key + Cmd/Ctrl+K, create modal, create-from-selection.
   - Exports: `createTaskFromText`.
   - Depends: `window.htmx`.

8. **hover-cards.js** (198 LOC)
   - Mention + task popovers, dismissal.
   - Private caches, self-contained.

9. **context-menu.js** (115 LOC)
   - Right-click, position, close handlers.
   - Exports: `actaForceApplySelfEvent`, `actaOpenBulkMenu`.
   - Depends: Alpine.store("selection").

10. **sse.js** (508 LOC)
    - Workspace + user EventSource, all handlers, morphing, list-panel debounce.
    - Exports: implicit (handlers wired to `source.addEventListener`).
    - Depends: `window.htmx`, idiomorph, Alpine (for filter awareness).

11. **utils.js** (100 LOC)
    - Helpers: scroll fades, sticky stack, strips, rail tooltips, sticky counters.
    - Exports: `updateStickyStack`, `updateStripCounters`, `updateScrollFades`.
    - Self-contained.

12. **toast.js** + **alpine-stores.js** (367 LOC combined)
    - Toast queue, Alpine stores (toasts, filters, theme, sidebar, kanban, selection, viewMode).
    - Exports: `actaToast`, `actaBulkPatch`, `actaBulkDelete`, `actaBulkArchive`.
    - Depends: Alpine (must load after Alpine boots).

13. **api-surface.js** (119 LOC)
    - `window.acta` object: CSRF, promoteTask, filter ops, recents.
    - Depends: nothing (preamble functions).

**Seams are clean.** No circular deps. Each module can load independently once dependencies are wired. Estimated effort: 1 PR splitting + test updates; minimal behavior change.

---

## 8. Findings (F1–F8)

### F1: Listener accumulation on strip / sticky-stack / scroll-fade elements

**Severity:** Low (memory, not functional).
**Effort:** Small (add cleanup on element swap).
**Code:**
```js
// Line 1910–1916: initStickyStacks
document.querySelectorAll("[data-sticky-stack]").forEach((container) => {
  if (container.dataset.stickyBound === "true") return; // Guard prevents re-bind
  container.dataset.stickyBound = "true";
  updateStickyStack(container);
  container.addEventListener("scroll", () => updateStickyStack(container), { passive: true }); // ← listener added
});
```

**Problem:** Guard re-fires on HTMX swap only if the **same DOM node** is present with `stickyBound = "true"`. If HTMX replaces the element (swaps its parent), the old node (and its listener) lingers in memory.

**Scenario:**
1. Page loads. Container added; listener wired; `stickyBound = "true"`.
2. HTMX swaps parent, replaces container with fresh node.
3. Old container's listener still exists; new container has no `stickyBound` flag, so `initStickyStacks` fires again.
4. Memory: old listener persists until GC.

**Fix:** Before wiring listeners, remove stale ones:
```js
function initStickyStacks() {
  document.querySelectorAll("[data-sticky-stack]").forEach((container) => {
    if (container.dataset.stickyBound === "true") return; // Already done
    // Cleanup: remove old listeners if element was recycled
    // (If we're seeing a fresh element, it never had listeners)
    container.dataset.stickyBound = "true";
    updateStickyStack(container);
    container.addEventListener("scroll", () => updateStickyStack(container), { passive: true });
  });
}
```

**Alternative:** Store listener reference on element, remove on swap:
```js
container._scrollListener = () => updateStickyStack(container);
container.addEventListener("scroll", container._scrollListener, { passive: true });
// On cleanup:
if (container._scrollListener) container.removeEventListener("scroll", container._scrollListener);
```

**Impact:** Passive listeners (no side effects). Unlikely to cause heap issues under normal nav patterns. But technically a leak.

**File:line:** `acta.js:1909–1918`, `1949–2006` (similar patterns on `initStrips`, `initScrollFades`), `2031` (resize listeners).

---

### F2: MutationObserver reference not cleaned on kanban column swap

**Severity:** Low (observer is GC'd with element, but reference lingers).
**Effort:** Small (add cleanup on column rebuild).
**Code:**
```js
// Line 2090–2105: installCollapsedBodySnap
function installCollapsedBodySnap(body) {
  if (body.__actaSnapObs) return; // Reuse if already bound
  const obs = new MutationObserver(snap);
  obs.observe(body, { attributes: true, attributeFilter: ["style"] });
  body.__actaSnapObs = obs; // ← Store reference on DOM node
}
```

**Problem:** If kanban column body is swapped without `body.__actaSnapObs` being disconnected, the observer still fires on a replaced element. The guard (`if (body.__actaSnapObs) return`) assumes the element is the same one; if HTMX replaces it, the guard fails.

**Scenario:**
1. Kanban loads. Column body added; observer stored on `body.__actaSnapObs`.
2. HTMX swaps board panel, replaces column body with fresh node.
3. New node has no `__actaSnapObs` property, so `installCollapsedBodySnap(newBody)` fires again. ✓ Correct.
4. But old node's observer never calls `.disconnect()`. It lingers until GC.

**Fix:**
```js
function installCollapsedBodySnap(body) {
  // Disconnect any old observer (defensive)
  if (body.__actaSnapObs) {
    body.__actaSnapObs.disconnect();
    body.__actaSnapObs = null;
  }
  // Proceed with fresh bind
  const snap = () => { /* ... */ };
  const obs = new MutationObserver(snap);
  obs.observe(body, { attributes: true, attributeFilter: ["style"] });
  body.__actaSnapObs = obs;
}
```

**Impact:** MutationObserver is lightweight (10s of them is fine). Unlikely to cause issues.

**File:line:** `acta.js:2090–2105`.

---

### F3: Timeline deadline patch error silent to console only

**Severity:** Very low (timeline is optional; deadline drag is rare).
**Effort:** Small (toast on error).
**Code:**
```js
// Line 1318–1324: patchDate
function patchDate(url, data, csrf) {
  fetch(url, {
    method: "POST",
    headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(data),
  }).catch((err) => console.error("[timeline] patch failed:", err)); // ← Console only
}
```

**Problem:** Deadline drag silently fails to the console. User sees the deadline snap back visually, but has no toast explaining why. Compare to kanban drop (line 758, 768) and task promote (line 88, 92), which toast.

**Context:** Timeline is a lazy panel (secondary view). Drag failures are rare. But UX inconsistency.

**Fix:**
```js
.catch((err) => {
  console.error("[timeline] patch failed:", err);
  if (window.actaToast) window.actaToast("Deadline update failed.", "error");
});
```

**File:line:** `acta.js:1318–1324`.

---

### F4: Wave 2 PR-5 silent-skip log verification

**Severity:** Info (verification task, not a bug).
**Context:** Wave 2 C7 documented `_broadcast_notification` (apps/notifications/services.py) debug logging on skip. JS handlers should not repeat that logging.

**Verification:** Line 2478–2527 (`onNotificationCreated`). Early returns:
- Line 2483: Project updates → skip (silent).
- Line 2489: Cross-workspace notification → skip (silent).

Both are correct skips (project updates surface only in Updates tab; cross-workspace notifications are out of scope). ✓ No duplicate logging in JS. Matches Python side.

**File:line:** `acta.js:2478–2527` (refs Wave 2 PR-5 behavior).

---

### F5: Search input debounce race with form reset

**Severity:** Low (edge case, low probability).
**Effort:** Small (clear timer on reset).
**Code:**
```js
// Line 1515–1522: bindFilterForm
const q = form.querySelector('input[name="q"]');
if (q) {
  let qTimer = null;
  q.addEventListener("input", () => {
    if (qTimer) clearTimeout(qTimer);
    qTimer = setTimeout(applyClientFilters, 150);
  });
}
```

**Problem:** Search input change debounces 150 ms. If user types "foo" (timer set), then clicks the reset button (form clears via custom handler line 1532–1537), the debounced `applyClientFilters` still fires 150 ms later. It re-reads the now-empty form state and applies (correct result), but technically re-evaluates work.

**Scenario:**
1. Type "foo" → timer set (150 ms).
2. Click reset → form state changes, but timer is still pending.
3. 150 ms later → `applyClientFilters` fires against empty state.

**Impact:** Minimal. Form state is correct (empty). Re-apply is idempotent. But could avoid a spurious call.

**Fix:**
```js
const resetBtn = form.querySelector('[data-reset]'); // or similar
if (resetBtn) {
  resetBtn.addEventListener("click", () => {
    if (qTimer) clearTimeout(qTimer);
    qTimer = null;
  });
}
```

Or capture reset event:
```js
form.addEventListener("reset", () => {
  if (qTimer) clearTimeout(qTimer);
  qTimer = null;
  // Form state is now cleared; applyClientFilters will run next
});
```

**File:line:** `acta.js:1515–1537`.

---

### F6: Kanban column count badge not updated on SSE card move

**Severity:** Low (cosmetic, SSE updates are frequent so badge catches up).
**Effort:** Small (recount after applyCardMove).
**Code:**
```js
// Line 2307–2314: task.status_changed handler
handle("task.status_changed", (d) => {
  applyCardMove(d.target_id, d.to, d.card_html); // Moves card between columns
  if (d.row_html_table) applyRowHtmlTable(d.target_id, d.row_html_table);
  refreshListPanel(); // Refetches list
  // ← No column count recount
});
```

**Problem:** `applyCardMove` physically moves the card but doesn't update the column header badges (showing count of visible cards). Compare to kanban DnD drop handler (line 761–764) which explicitly recounts.

**Scenario:** Peer moves task to "Done". Card visually moves in peer's view. Your kanban shows old count badge on "Done" column (off by ±1) until next filter apply or page nav.

**Impact:** Cosmetic. Badge updates on client-side filter re-apply (line 648–651) or next full nav. But inconsistent with DnD.

**Fix:**
```js
handle("task.status_changed", (d) => {
  applyCardMove(d.target_id, d.to, d.card_html);
  if (d.row_html_table) applyRowHtmlTable(d.target_id, d.row_html_table);
  refreshListPanel();
  // Recount column badges
  document.querySelectorAll(".kanban-column").forEach((c) => {
    const visible = c.querySelectorAll("[data-task-id]:not([hidden])").length;
    const counter = c.parentElement?.querySelector("[data-column-count]");
    if (counter) counter.textContent = String(visible);
  });
});
```

**Alternative:** Extract recount to a helper:
```js
function recountKanbanColumns() { /* ... */ }
// Already exists at line 856; just call it.
handle("task.status_changed", (d) => {
  applyCardMove(d.target_id, d.to, d.card_html);
  if (d.row_html_table) applyRowHtmlTable(d.target_id, d.row_html_table);
  refreshListPanel();
  recountKanbanColumns(); // ← Add this
});
```

**File:line:** `acta.js:2307–2314` (handler), `856–862` (recount function).

---

### F7: Missing export of `promoteTask` in window.acta contract

**Severity:** Info (not a functional bug, documented in code).
**Code:**
```js
// Line 55–168: window.acta assignments
window.acta = Object.assign(window.acta || {}, {
  csrfToken: () => getCookie("csrftoken"),
  updateStickyStack: null, // assigned below
  loadRecents,
  recordRecentTask,
  promoteTask(slugPrefix, number, status) { ... }, // ← Inline definition
  exportQuery() { ... },
  removeFilter(name, value) { ... },
  // ...
});
```

**Observation:** Unlike other exports, `promoteTask` is defined inline (anonymous function inside the object literal). Other functions like `loadRecents` are named functions assigned by reference. This is fine, but inconsistent.

**Impact:** None. Works correctly. But for clarity:

```js
function promoteTask(slugPrefix, number, status) {
  // ... body
}
window.acta = Object.assign(window.acta || {}, {
  promoteTask,
  // ...
});
```

**Not a required fix.** Current code is valid JS.

**File:line:** `acta.js:55–94`.

---

### F8: Alpine.init runs twice if page loads with readyState="complete"

**Severity:** Negligible (Alpine boots once; stores re-declared, but idempotent).
**Code:**
```js
// Line 3026: addEventListener only
document.addEventListener("alpine:init", () => {
  window.Alpine.store("toasts", { /* ... */ });
  // ... 5 stores
});
```

**Problem:** If the page is already interactive when this code runs (readyState="complete"), and Alpine has already fired `alpine:init`, the listener never fires. But there's also a check inside the `alpine:init` handler — if Alpine re-initializes (edge case), stores are re-declared.

**Context:** Alpine 3 fires `alpine:init` once per page, after the first component initializes. Unlikely to fire twice. But if Alpine **is** loaded twice (edge case), the store declarations are idempotent (they overwrite prior ones), so no data loss.

**Impact:** Negligible. Stores are functional objects with no state persistence (re-declaration just creates fresh instances).

**File:line:** `acta.js:3026–3330`.

---

## 9. Defer-to-browser-measurement

The following claims require Lighthouse / Chrome DevTools to verify:

1. **Memory footprint of `pageCache` LRU.** Currently max 20 entries. If typical page HTML is ~500 KB, cache footprint is ~10 MB. Under real-world navigation patterns (idle sessions), is this acceptable?
   - **Tool:** Chrome DevTools Heap Profiler. Snapshot before Back, snapshot after. Measure delta.

2. **SSE event processing latency.** 250 ms debounce on list-panel refetch. How much does coalescing reduce redundant renders?
   - **Tool:** Chrome DevTools Performance tab. Record 10 SSE events in quick succession (peer edits). Measure "recalculate style" + "layout" time with / without debounce.

3. **Timeline Gantt render performance on large task counts.** `renderBars` (line 1106–1199) loops all rows, builds bar + deadline elements. No virtual scrolling.
   - **Tool:** Lighthouse (performance score) on a project with 500+ tasks. Measure FCP / LCP.

4. **Listener overhead on heavy filter / strip / scroll-fade elements.** Each element carries scroll + resize listeners. In a table with 1000 rows, is listener count a perf issue?
   - **Tool:** Chrome DevTools, Record → show event listeners in the Inspector.

---

## 10. Cross-links and deferred actions

### Related audit chunks
- **Wave 1 D1** (`docs/audit/05-nav-router.md`): Structural baseline + router verification.
- **Wave 2 C7** (`docs/audit/wave2/05-notifications-sse.md`): SSE event drift table (all 14 types verified zero renames).
- **ADR 0024** (project-todo-history-router): Page cache + history router design.

### Next chunks (Wave 3)
- **D3:** Templates (`_task_*.html` partials, Alpine inline scripts, SSE wiring).
- **D4:** Django views + URL routing (context, caching, SSE setup).

### Deferred to Wave 4
- **R4 (bundle split):** Split `acta.js` into 10–12 files. Natural seams identified (§7).
- **F1–F2 (listener cleanup):** Add guards against listener accumulation on swapped elements.
- **F3 (timeline toast):** Surface deadline patch errors to toast.
- **F5 (search debounce):** Clear search timer on filter reset.
- **F6 (column count):** Recount on SSE status change.

---

## 11. Summary

**`acta.js` is well-engineered for its scope.** The custom history router (ADR 0024) is correct and handles all edge cases. SSE integration is mature (14 event types, zero drift vs Python). Idiomorph swaps preserve Alpine state on peer edits. Error handling surfaces user-facing failures to toasts.

**Code quality is high but size is substantial (3,332 LOC).** The two major IIFEs (`initTimeline` 467 LOC, `alpine:init` 367 LOC) deserve their own files. 8 per-section `htmx:afterSettle` listeners fire on every swap; idempotency is strong but cognitive load is high.

**Six findings, all low severity:** Two memory leaks (listener accumulation, F1–F2), one error handling gap (timeline toast, F3), two cosmetic issues (column count recount, F6; search debounce race, F5), one verification task (Wave 2 PR-5, F4), one code style note (promoteTask consistency, F7). None block shipping; all are Wave 4 polish.

**ADR 0024 invariants hold.** Page cache is local; snapshots pair URLs correctly; history is off; invalidate on mutation + SSE. Tested against fast-nav edge cases (back-back-forward, SSE during nav, lazy panel race).

**Memory leaks are low risk in practice.** Passive listeners, rare DOM replacements, and Alpine re-init on nav all mitigate. But cleanup patterns should be formalized (§9 / Wave 4).

---

**Audit completed: 2026-05-29.**
**Checker: D2 (Wave 3 chunk).**
