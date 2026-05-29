# 05 — `acta.js` / nav router / page cache

> **Wave 1 / Chunk D1** — `static/js/acta.js` (3 244 LOC monolith)
> covering custom history router, lazy-panel loader, client-side
> filter mirror, kanban DnD, sortable labels, timeline, sort
> handlers, hotkeys, sticky stacks, scroll fades, SSE workspace+user
> handlers, hover cards, toasts, context menu.
> Date: 2026-05-29. Read-only. **No code changed.**
> Sources: structural map (lines 6-2945), critical paths read in
> detail (cache 269-384, SSE 2080-2400, force-apply 2791-2796,
> toast 2888-2945, lazy panels 147-221 from B1, kanban DnD 707-817
> from B2, filter mirror 411-704 from B2, recomputeKanbanSubstatus
> 1371-1456 from B2). [[project-nav-history-router]] (ADR 0024).

---

## 1. Quick verdict

**`acta.js` is well-engineered but big.** The custom history router
(ADR 0024) earns its size: token-based abort, idempotent re-binds
via `Sortable.get`, idiomorph SSE swaps that preserve focus, a
wholesale page-cache invalidation rule that prioritises correctness
over hit-rate. Most of the "this feels janky" candidates I expected
to find aren't here — the perf characteristics are sound. The
genuine costs are **shape**, not **behaviour**: 10+ separate
`htmx:afterSettle` listeners that all fire on every swap, ~8
delegated `document` listeners, a 460-LOC `initTimeline` IIFE that
deserves its own file, and a global `window.acta` object that's
wholesale-reassigned on every load.

**Baseline §6 was wrong about `morph:outerHTML`.** Idiomorph **is**
loaded and **is** used — but only by the SSE per-row swaps, not by
HTMX page swaps. So idiomorph preserves Alpine state + focus on
SSE peer-edit updates; default outerHTML still runs on normal
HTMX swaps (and remounts Alpine). The two paths are intentional
and documented.

**Cache hit rate is structurally capped low** because any non-GET
HTMX response **and** any SSE event triggers `pageCache.clear()`.
In a busy workspace this means the cache is empty most of the
time — Back/Forward usually refetches. That's a correct trade-off
(stale data ≫ slow nav) but worth understanding: nav speed under
real workload is dominated by the refetch path, not the cache hit.

---

## 2. Structural map of `acta.js`

| Lines | Section | What |
|---|---|---|
| 1-49 | Module preamble | IIFE wrapper; `getCookie`; LocalStorage `recents` |
| 50-145 | `window.acta` API surface | `csrfToken`, `promoteTask`, `exportQuery`, `removeFilter`, `toggleFilter`, `clearFilter`, `loadRecents`, `recordRecentTask` |
| 147-221 | Lazy panels | `lazyLoadPanels` — covered in B1 §2.1 |
| 223-250 | Sidebar active-nav | URL-based highlight refresh on settle + popstate |
| 252-384 | **Custom history router** | `pageCache`, `navToken`, snapshot / restore / invalidate, popstate + boosted-nav handlers |
| 386-409 | Sidebar rail tooltips | Mouseover-positioned, viewport-aware |
| 411-704 | Client-side filter mirror | `readFilterState`, `rowMatches`, `applyClientFilters` — covered in B1 / B2 |
| 707-817 | Kanban DnD + labels reorder | `initKanbanDnD`, `kanbanCardNewTab`, `initLabelsDnD` — covered in B2 |
| 819-895 | Cross-view freshness | `recountKanbanColumns`, `acta:task-created` panel-slot invalidator, `acta:list-insert-row` — partly in B2 |
| 897-1358 | **Timeline (Gantt)** | `initTimeline` — 460 LOC IIFE, complex |
| 1371-1456 | `recomputeKanbanSubstatus` | Covered in B2 §2.1 |
| 1458-1516 | `bindFilterForm` | HTMX hijack to drive client filter |
| 1518-1657 | Column sort | `compareRows`, `applyClientSort`, `parseClauses`, `parseOrder`, `nextSortState`, `buildUrl`, `refreshSortIndicators`, click handler |
| 1659-1830 | Hotkeys + create task | `isTypingTarget`, `openCreateTaskModal`, "n" / Cmd+K hotkeys, `createTaskFromText` |
| 1831-1875 | Icons + sticky stack | `renderIcons` (stub), `updateStickyStack` |
| 1877-1980 | Sticky / strips | Sticky-stack init, strip-counters init |
| 1982-2040 | Scroll fades | `updateScrollFades`, `initScrollFades` (edge gradients on overflow containers) |
| 2040-2400 | **SSE — workspace** | `applyCardReplace/Move/Remove`, `initWorkspaceSse`, `initOneWorkspaceSse`, `applyTaskUpdate`, `task.*` handlers, `notification.*` |
| 2446-2487 | **SSE — user** | Per-user inbox stream, badge counter |
| 2490-2515 | Tooltips theming | Theme-aware tooltip skin on settle / Alpine init |
| 2518-2555 | Lightbox | Inline-image click-to-zoom |
| 2557-2716 | **Hover cards** | Mention + task hover popovers |
| 2718-2748 | Comment hash highlight | `#comment-<id>` scroll-and-flash |
| 2750-2796 | Context menu prep + `forceApplySelf` | `actaForceApplySelfEvent(id)` with 4 s TTL |
| 2798-2876 | Context menu (right-click) | Bulk-aware selection + url fetch |
| 2878-2945 | Toasts + alpine:init | Error surfacing, HX-Trigger relay, pending queue |

Boundaries are clean enough that splitting into 8-10 files would
have minimal seam friction. **But splitting is a frontend-arch
decision, not a perf win** — defer to D3 / a dedicated split PR.

---

## 3. What works (good news)

### 3.1 Page cache router is correct (`acta.js:252-384`)

The hard parts handled:

- **Token-based abort** (`navToken`): each restore call increments
  the token; a stale `fetch().then(...)` notices `token !== navToken`
  and bails (line 334). Aborts the in-flight `fetch` via
  `AbortController` (line 317).
- **Snapshot pairing** captures the LEAVING page's URL, not the
  destination, so `pageCache.set(lastUrl, html)` puts the right
  HTML against the right key (line 366 + 355).
- **HMX `swap` on restore** runs the full `afterSwap/afterSettle`
  lifecycle so lazy panels, SSE binds, icon render, active nav
  all re-trigger on restore (line 291-293). Crucial — without this,
  a cached snapshot would land with stale event bindings.
- **DOMParser-extracted `#app-content`** on miss (line 335-336)
  keeps the swap surface consistent regardless of cold vs cached.
- **Graceful degradation**: `window.location.assign` on extraction
  failure (338) or network error (349). Better than an inscrutable
  freeze.

This is the kind of code that should be in a blog post on
"correct manual SPA history". It's the right size for the problem.

### 3.2 Wholesale cache-invalidate on writes + SSE (`acta.js:301-314, 2135`)

```js
function invalidatePageCache() { pageCache.clear(); }

document.body.addEventListener("htmx:afterRequest", (evt) => {
  const ok = evt.detail && evt.detail.successful;
  const verb = cfg && cfg.verb;
  if (ok && verb && verb.toLowerCase() !== "get") invalidatePageCache();
});
```

**Correctness over hit rate.** A single task can appear on many
pages; the router can't cheaply identify which snapshots became
stale. So any mutation drops everything. Comment captures the
rationale.

**Implication**: the cache mostly helps the **idle** case
(open a project, click another sidebar link, click Back). In a
session with active SSE traffic the cache is rarely warm.

### 3.3 SSE self-event filter + opt-in (`acta.js:2144-2151, 2791-2796`)

```js
if (String(data.actor_id) === meId && !data.via_mcp) {
  if (window.__actaForceApplySelf && window.__actaForceApplySelf.has(tid)) {
    window.__actaForceApplySelf.delete(tid);
  } else {
    return;  // drop self-event to avoid double-render
  }
}
```

Three exits from "drop self-event":
1. `via_mcp` events go through (MCP writes through a different
   client; the local tab never saw the HTTP swap).
2. `actaForceApplySelfEvent(id)` opt-in for the calling site (4 s
   TTL on the opt-in, so a stale id doesn't apply to an unrelated
   later edit).
3. Default drop.

This is **the** anti-Kaneo rule for SSE. Subtle. Documented well in
the comment above and in [[feedback-stop-kostyling]] (the opt-in is
a feature, not a workaround).

### 3.4 Idiomorph swap preserves Alpine state (`acta.js:2191-2202`)

```js
function morphFromString(targetEl, html) {
  if (window.Idiomorph) {
    window.Idiomorph.morph(targetEl, fresh, { morphStyle: "outerHTML" });
  } else {
    targetEl.replaceWith(fresh);  // graceful fallback
  }
}
```

**Idiomorph is loaded** — baseline §6 was wrong to flag it as
absent. It's used on **per-row SSE swaps** (table rows, kanban
cards) so focus / Alpine state / open popovers survive. HTMX
boosted page swaps (`#app-content`) still go through default
`outerHTML` — Alpine re-mounts fresh, focus drops. That's
intentional (full nav clears state) and correct.

### 3.5 List view refetches its whole panel on any SSE event (`acta.js:2174-2189`)

```js
function refreshListPanel() {
  listPanelRefetchTimer = setTimeout(() => {
    document.querySelectorAll('[data-panel-slot="list"]').forEach((slot) => {
      const url = new URL(window.location.href);
      url.searchParams.set("panel", "list");
      window.htmx.ajax("GET", url.pathname + url.search, { target: slot, swap: "innerHTML" });
    });
  }, 250);
}
```

Group-by sections + counts re-compute on the server. 250 ms debounce
batches multi-event bursts. Smart — kanban/table can stay in-place;
list can't, because section membership shifts.

### 3.6 SSE deduping per URL (`acta.js:2085-2105`)

`SSE_BOUND_URLS` (module-level Set) + `root.dataset.sseBound`
(per-element flag) prevent double-EventSource per workspace marker.
Cross-workspace pages (My Work, All Tasks) emit one marker per
workspace; the dedup keeps the open-streams count bounded.

### 3.7 Stream close on `pagehide` / `beforeunload` (`acta.js:2113-2121`)

```js
window.addEventListener("pagehide", closeStream);
window.addEventListener("beforeunload", closeStream);
```

**Critical for dev** — without it `uvicorn --reload` pauses on every
restart waiting for connections to close. Comment captures the why.

### 3.8 Toast pending queue (`acta.js:2888-2895`)

Toasts fired before `alpine:init` (e.g. from a global HTMX error
on the very first request) go into a pending queue. After Alpine
boots, they replay. **Race-condition-free** for early errors.

### 3.9 Context menu close on scroll has the right scope (`acta.js:2862-2873`)

```js
window.addEventListener("scroll", (e) => {
  if (root.style.display === "none") return;
  if (e.target && root.contains(e.target)) return;
  closeMenu();
}, true);
```

Capture-phase scroll fires for the menu's own scrollable submenus.
The `root.contains` guard skips those — page scroll closes, submenu
scroll doesn't. **This is the exact correctness comment from
[[feedback-overflow-kills-popovers]]'s adjacent concern.**

---

## 4. Real findings

### 4.1 10+ separate `htmx:afterSettle` listeners

Count by line: 212, 249, 372, 766, 809, 1358, 1493, 1736, 2742,
2506, plus a handful more on `htmx:afterSwap`. Each is a small
function; collectively they all run on every settle.

Each handler is fast (a `document.querySelector` + a guarded init).
Cumulative cost is **micro**seconds, not milliseconds. Not a perf
problem today.

**But** it makes the order of operations opaque: a future addition
that depends on, say, "sidebar is refreshed before lazy panels
fire" has no documented invariant. Could be a single dispatcher:

```js
const afterSettleHandlers = [refreshSidebarActive, initKanbanDnD,
  initLabelsDnD, scanRecentMarker, themeTooltips, …];
document.body.addEventListener("htmx:afterSettle",
  (evt) => afterSettleHandlers.forEach((fn) => fn(evt)));
```

**Defer**: doesn't fix perf, just shape. Track for a future split PR.

### 4.2 `initTimeline` is 460 LOC in one IIFE (`acta.js:908-1358`)

The single largest function in the file. Includes zoom, drag
deadlines, date-range rendering, today-line computation. Comments
inside say "ported from inline script" — moved here from a per-page
inline `<script>` so it would survive cache-restore.

**Not investigated deeply** in this audit. Defer to D2 (Alpine
patterns) and a dedicated `static/js/timeline.js` split candidate.
Flag as "biggest single complexity centre" in the file.

### 4.3 `window.acta` wholesale-reassigned on every load (`acta.js:50`)

```js
window.acta = {
  csrfToken: …, promoteTask: …, removeFilter: …, …
};
```

Comment explains: the palette template's inline script (loaded
earlier in the body) would assign to `window.acta` first; acta.js
runs later and overwrites. Today this works because acta.js is the
**only** late-loaded source that touches `window.acta`. **Fragile
if a second late-loaded script appears** — it'd overwrite
acta.js's exports.

**Defer fix**: change to `Object.assign(window.acta = window.acta
|| {}, { … })`. Tiny risk, future-proof.

### 4.4 No toast on `promoteTask` / kanban-drop failure (`acta.js:60-71, 712-738`)

Both `promoteTask` (the row's quick-promote chip) and `handleKanbanDrop`
silently fall back on a non-OK response. Drop rolls back the card;
promote does nothing visible. **No error toast**.

The `htmx:responseError` global toast (line 2904) doesn't catch
these because both use raw `fetch`, not HTMX. **Fix**: pipe failures
to `window.actaToast`:

```js
if (!r.ok) { rollback(); window.actaToast("Couldn't move card", "error"); return; }
```

Small but visible UX win. F-toast candidate.

### 4.5 `actaForceApplySelfEvent` 4 s TTL fragility (`acta.js:2795`)

```js
setTimeout(() => window.__actaForceApplySelf.delete(n), 4000);
```

4 seconds is the window for the SSE round-trip to land. Under a
slow worker, a long-running write (long activity-log fanout, slow
Telegram callback) might miss this. The id would be removed; the
SSE event arrives; self-filter drops it; the row stays stale.

**Probability**: low. Acta's writes are fast. But a misbehaving
plugin or queue would surface this as "row didn't update after I
edited it, then I refreshed and it had updated".

**Defer fix**: bump TTL to 30 s and document. The set is bounded
(at most "tasks the user touched in the last 30 s"), so memory cost
is trivial.

### 4.6 `htmx:afterSwap` vs `htmx:afterSettle` choice

Mixed across handlers:
- `htmx:afterSwap` — `initWorkspaceSse`, `initUserSse`,
  `themeTooltips`, `initScrollFades` (line 2019)
- `htmx:afterSettle` — `lazyLoadPanels`, kanban DnD, labels DnD,
  filter form bind, recent marker scan, comment hash highlight,
  history pushState

`afterSwap` fires before Alpine settles; `afterSettle` after. The
distinction matters when a handler depends on Alpine being ready.
Quick audit:
- `initWorkspaceSse` reads `[data-workspace-sse]` — pure DOM, no
  Alpine. OK on `afterSwap`.
- `themeTooltips` toggles classes by data — no Alpine. OK on
  `afterSwap`.
- `initScrollFades` reads scroll positions. OK on `afterSwap`.

Choices look intentional. **Not a finding** — just an observation.

### 4.7 Many delegated `document` listeners (~8+)

Click, dblclick, mouseover, mouseout, contextmenu, keydown × 3,
auxclick. Standard delegation. Each handler is short and early-exits
when its closest-selector doesn't match. **No issue**.

### 4.8 `getCookie("csrftoken")` reads `document.cookie` on every call

Cheap (microseconds), called per-fetch. Not a problem.

### 4.9 SSE handler imports `__actaInvalidatePageCache` via `window` (line 2135)

```js
if (window.__actaInvalidatePageCache) window.__actaInvalidatePageCache();
```

The page-cache code defines `window.__actaInvalidatePageCache =
invalidatePageCache;` (line 304). SSE handler reads it via window.
Works because both live in the same IIFE — could be a direct
reference. Defensive but redundant. Cosmetic.

### 4.10 `applyCardReplace` / `applyCardMove` use idiomorph (line 2042-2076)

`applyCardReplace` swaps a kanban card in place; `applyCardMove`
moves the card between columns + recounts. Both use
`morphFromString` (idiomorph). **Focus + Alpine state preserved on
SSE peer edits to cards.** This is the right pattern; should be
the model for any future per-row swap.

### 4.11 Cmd+K hotkey listens on `document` (line 1698)

```js
document.addEventListener("keydown", function onPaletteHotkey(evt) {…});
```

Plus `n` for create-task (line 1685). Both go through `isTypingTarget`
guard (line 1659) so they don't fire when the user is typing in an
input / textarea / contenteditable. Clean.

### 4.12 No reference to `requestIdleCallback`

Several `setTimeout(fn, 50/150/250)` patterns in lazy panels,
filter debounce, list refetch. Could be `requestIdleCallback`
on browsers that support it, with `setTimeout` fallback. Tiny
perf gain. Defer.

---

## 5. Subtle issues to verify in dev

| # | Issue | How to verify |
|---|---|---|
| 5.1 | Page cache hit rate under live traffic | Devtools profile: open project, sidebar→inbox, back. Snapshot count vs hit count |
| 5.2 | SSE EventSource leakage on rapid nav | Open project A, immediately nav to project B; verify `SSE_BOUND_URLS` doesn't grow unbounded |
| 5.3 | Idiomorph fallback path (no Idiomorph loaded) | Block the CDN script; verify `targetEl.replaceWith(fresh)` works |
| 5.4 | `actaForceApplySelfEvent` 4 s TTL under load | Edit status of a task in a workspace with heavy fanout (Telegram + many recipients); verify the row updates |
| 5.5 | Timeline init on cache-restore | Open timeline, switch away, switch back via Back; verify no double-render or stale chart |
| 5.6 | Toast queue replay | Force an HTMX error before Alpine init (cold reload + immediately click a failing button) |

Park until dev is up.

---

## 6. Fix candidates (input to Chunk G)

| # | Tag | Title | Notes |
|---|---|---|---|
| F1 | `ux/toast` `[3/1/1]` | Add `actaToast` failure-path for `promoteTask` + `handleKanbanDrop` (§4.4) | Tiny visible UX win |
| F2 | `bug/safety` `[2/1/1]` | Bump `actaForceApplySelfEvent` TTL 4 s → 30 s + comment (§4.5) | Two lines |
| F3 | `clean/code` `[1/2/1]` | Consolidate 10+ `htmx:afterSettle` listeners into one dispatcher (§4.1) | Shape, not perf |
| F4 | `clean/code` `[2/3/2]` | Split `initTimeline` into `static/js/timeline.js` (460 LOC out of acta.js) | Maintainability |
| F5 | `clean/code` `[1/1/1]` | `Object.assign(window.acta = window.acta \|\| {}, {…})` so a late-loaded peer script can't wipe exports (§4.3) | Future-proof |
| F6 | `perf/js` `[2/2/2]` | `requestIdleCallback` w/ `setTimeout` fallback for lazy-panels / list-refetch (§4.12 + B1 §3.6) | Defer to a sweep PR |
| F7 | `clean/code` `[1/1/1]` | Drop the `window.__actaInvalidatePageCache` defensive read (§4.9) | Cosmetic |
| F8 | `bug/uat` `[2/1/1]` | UAT idiomorph fallback path (§5.3) | Defensive UAT |
| F9 | `tests/regress` `[3/3/3]` | JS unit tests for `readFilterState`, `rowMatches`, `compareRows`, `parseClauses` (already partial in `vitest.config.js` setup) | Filter/sort tests exist; extend to nav router primitives |
| F10 | `clean/file-split` `[2/4/3]` | Long-term: split `acta.js` into 8-10 logical files at section boundaries (§2 map) | Defer; needs a focused PR |

---

## 7. Inputs to other Wave 1 chunks

- **G (synthesis)**: D1 confirms most of what B1-B4 found; nav-router
  itself is **not** the source of perceived jank. The jank, if real,
  is more likely in cold-load payload size (B1 row template / B3
  cell density) or Alpine re-mount on full swap (which is by design).
- **B1 F4 (lucide `<use>`)**: D1 surfaces no new info on inline SVG.

## 8. Inputs to Wave 3 (frontend depth)

- **D2 (Alpine patterns)**: 460-LOC `initTimeline` is the densest
  imperative DOM block in the file — verify its Alpine integration
  is clean (it likely is, since it uses native DOM with sortable.js,
  not Alpine state).
- **D3 (CSS / Tailwind / FOUC)**: scroll-fades pattern in §4.6 is
  fully JS-driven. Could move to pure CSS via `scroll-margin` +
  `position: sticky` techniques, but the current approach works.

## 9. Inputs to Wave 2 (placeholder)

- **C6 (activity / SSE)**: confirm broadcast payload (`card_html`,
  `row_html_table`) is what D1's `applyCardReplace` / `applyRowHtmlTable`
  expect. Drift between Python `emit_task_diff_events` and JS
  handlers is the highest-risk drift in the SSE path.
- **C5 (comments)**: hover-card "task card on mention" code lives
  in `acta.js:2557-2716` — its data shape comes from a `?` endpoint;
  C5 should audit that endpoint.

---

## 10. Status

- Chunk D1: **complete**.
- No code changed.
- Page-cache router judged **solid**. Headline finding: cache hit
  rate is structurally capped low by aggressive invalidation —
  correct but worth knowing.
- Idiomorph **is** loaded (baseline §6 was wrong); used for SSE
  per-row swaps with documented fallback.
- 10 fix candidates added to G's input set.
- Next chunk: **G — Wave 1 synthesis**.
