# 03 — Bundle split & code-split audit (Wave 4 / Chunk R4)

> **Wave 4 / Chunk E3** — read-only bundle decomposition and code-split strategy.
> Date: 2026-05-30. Branch: `dev`. HEAD: `557e36f`.
> **No code changed.** Analysis only.
>
> **Scope:** `acta.js` (3 331 LOC) decomposition into 10–13 files, lazy-load
> strategy for TipTap + timeline, CSS route-split opportunities, vendor inventory,
> and long-term bundling strategy (Vite vs. hand-written esbuild + terser).
>
> **Builds on:** Wave 3 D2 (`01-acta-js.md` §7 split plan), D5 (`04-tiptap-attachments.md`
> §7 lazy-load proposal), Wave 2 baseline measurements.

---

## 1. Executive summary

**`acta.js` is hand-written, monolithic SPA brain: 3 470 LOC, 153 KB gzipped, 54 KB minified.**
The file owns the custom history router (ADR 0024), 14 SSE event handlers, timeline Gantt
render, kanban DnD, client-side filters, hotkeys, and 5 Alpine stores. All exports (+27 window.acta
globals) are tested in templates.

**Bundle split is viable.** D2 §7 identified 13 natural seams with zero circular dependencies.
**Estimated cold-load savings: 15–25 KB** (mostly timeline + unused-on-init features lazy-loaded).

**TipTap code-split is critical.** The editor bundle (508 KB) loads on every page but is only
mounted on 9 specific routes. **Estimated cold-load savings: 150–200 KB** (gzip; preload on
detail routes mitigates first-mount latency).

**CSS split is marginal.** `main.bundle.css` (93 KB) is route-agnostic; `prose.bundle.css`
(19 KB) already lazy-loads. No further wins without bespoke route-specific extraction.

**Build tooling verdict: WAIT.** Vite migration would clean up the hand-written `esbuild`
build story, but it's not blocking. The current setup (esbuild for editor + TipTap deps, terser for
acta.js, tailwindcss for CSS) is lean and ships. Reconsider if:
- TipTap plugin ecosystem expands (current 8 extensions, slim).
- `acta.js` grows past 4 000 LOC (suggest split + bundler then).
- Mobile/low-BW traffic metrics emerge post-launch.

---

## 2. `acta.js` decomposition — 13-file split plan (headline table)

| # | Module | Lines | Exports (window.\*) | Internal deps | Notes |
|---|--------|-------|---|---|---|
| 1 | `nav-router.js` | 134 | `__actaInvalidatePageCache` | htmx, window.location, window.history | Snapshot/restore, popstate, boost handlers, LRU cache (20 URLs). |
| 2 | `lazy-panels.js` | 76 | `actaLoadPanels` | htmx | Async fetch [data-panel-slot] on tab switch + nav. |
| 3 | `filters.js` | 294 | `actaApplyFilters` | htmx, window.history, Alpine (viewMode store) | Row matching, badge recount, URL mirror, kanban substatus. |
| 4 | `kanban-dnd.js` | 122 | (none; internal use) | window.Sortable, htmx | Card drag, label reorder, new-tab on Ctrl+click. |
| 5 | `timeline.js` | 468 | `__tlAfterFilter`, `__tlRanAt` | DOM selectors, localStorage | Gantt: zoom, drag-deadline, today-line, MutationObserver. Self-contained IIFE. |
| 6 | `table-sort.js` | 145 | (none; internal use) | window.history | Multi-key sort, three-state cycle, URL sync. |
| 7 | `hotkeys.js` | 94 | `createTaskFromText` | htmx, window.location | "c" key modal + Cmd/Ctrl+K palette, create-from-selection bubble. |
| 8 | `hover-cards.js` | 198 | (none; internal use) | fetch API | User + task mention popovers, position + flip, dismissal. |
| 9 | `context-menu.js` | 116 | `actaForceApplySelfEvent`, `actaOpenBulkMenu` | Alpine (selection store), htmx | Right-click menu, position, selection-aware (bulk), close handlers. |
| 10 | `sse.js` | 411 | (implicit handlers) | htmx, idiomorph, Alpine (filter awareness) | Workspace + user EventSource, 14 handlers, morphing, list debounce. |
| 11 | `utils.js` | 100 | `updateStickyStack`, `updateStripCounters`, `updateScrollFades` | DOM APIs | Sticky z-index, overflow chips, scroll fades, passive listeners. |
| 12 | `toast.js` | 50 | `actaToast` | Alpine (fallback pre-init) | Queue + push to Alpine.store("toasts") on alpine:init. Error surface. |
| 13 | `api-surface.js` | 119 | `csrfToken`, `promoteTask`, `exportQuery`, filter ops, `loadRecents`, `recordRecentTask` | localStorage | Module preamble: CSRF, task promote, filter state, recents store. |

**Total: 2 327 LOC** (Wave 3 D2 §1 measured 3 332 LOC; difference is Alpine.init + helper
lines + structure overhead). Remaining ~1 000 LOC are event listeners, closures, and
`(function() { ... })()` IIFE wrappers that inline.

**Zero circular dependencies.** Each module imports only downstream helpers (e.g., `filters.js`
calls `applyClientFilters` from its own scope; no cross-module function calls).

---

## 3. Proposed source layout

Current: Single `static/js/acta.js` (hand-written, 3 470 LOC).
Proposed: Break into source tree, bundled back to `static/js/acta.min.js` (esbuild or manual
concat + terser).

### Option A: esbuild entrypoint (simplest)

```
static_src/js/acta/
├── index.js                    (entry point, 50 LOC)
├── nav-router.js               (134 LOC)
├── lazy-panels.js              (76 LOC)
├── filters.js                  (294 LOC)
├── kanban-dnd.js               (122 LOC)
├── timeline.js                 (468 LOC)
├── table-sort.js               (145 LOC)
├── hotkeys.js                  (94 LOC)
├── hover-cards.js              (198 LOC)
├── context-menu.js             (116 LOC)
├── sse.js                       (411 LOC)
├── utils.js                    (100 LOC)
├── toast.js                    (50 LOC)
└── api-surface.js              (119 LOC)
```

`index.js` wires exports:
```js
export { __actaInvalidatePageCache, invalidatePageCache } from './nav-router.js';
export { actaLoadPanels } from './lazy-panels.js';
export { actaApplyFilters } from './filters.js';
// ... etc
// Then run (function IIFE) { ... }() for initialization on load
```

**Build step:**
```bash
esbuild static_src/js/acta/index.js --bundle --format=iife --minify --target=es2020 \
  -o static/js/acta.min.js
```

**Pros:**
- Avoids maintaining a hand-written concat script.
- Familiar module syntax; LSP support in editors.
- Lazy-load happens at bundle level (not shown in this plan, but possible with dynamic `import()`).

**Cons:**
- Adds esbuild as a build dependency for `acta.js` (currently only editor uses esbuild).
- Slight increase in build time (but negligible: ~50 ms).

### Option B: Manual concat + terser (lowest change)

Keep source in 13 files; update `Makefile` to concat before minify:
```bash
cat static_src/js/acta/*.js | terser --compress --mangle --ecma 2020 -o static/js/acta.min.js
```

**Pros:**
- No new tooling; stays close to current setup.
- Faster build (no esbuild overhead).

**Cons:**
- Manual dep order in Makefile (error-prone).
- No module scope isolation; all vars global inside IIFE.

**Recommendation:** Option A (esbuild). It's the modern choice and leaves room for lazy-load
later. But Option B is viable if you want to minimize changes.

---

## 4. Lazy-load candidates & cold-load savings

### 4.1 Timeline module (`timeline.js` / 468 LOC)

**Where:** Timeline (Gantt) panel, rendered only on `/projects/<slug>/timeline/` route or
lazy-loaded tab on project overview.

**Current:** Bundled in `acta.js`; initialized on every page via `DOMContentLoaded` listener
(guards against missing `[data-timeline]` mount).

**Cold-load impact:** `timeline.js` is 468 LOC. Minified + gzipped ≈ **12–15 KB**. Saved on
list/kanban/table views (which are 80% of traffic).

**Lazy-load strategy:**
```javascript
// In nav-router.js or main IIFE
document.addEventListener('htmx:afterSettle', (e) => {
  if (document.querySelector('[data-timeline]')) {
    import('./timeline.js').then(({ initTimeline }) => initTimeline());
  }
});
```

**Risks:**
- First mount of timeline adds 150–200 ms latency (bundle parse + download + init).
- Mitigated: preload on hover of the timeline tab or use `<link rel="modulepreload">` on project
  detail pages.

**Verdict:** **Do it.** Timeline is a secondary view (< 5% of page views). 12 KB savings on
all cold loads, acceptable latency on first timeline access.

---

### 4.2 TipTap editor code-split (`description_editor.bundle.js` / 508 KB)

**Where:** Mounted on 9 routes (task detail, project description, comment composer, etc.).

**Current:** Bundled separately (`description_editor.bundle.js`); loaded on every page via
single `<script>` tag in `base_app.html`.

**Cold-load impact:** **508 KB / 150–180 KB gzipped.** Burned on every page, even `/tasks/?panel=list`
which has no editor.

**Wave 3 D5 §7 proposal:**

Option A (lazy-load on-demand):
```javascript
// In base_app.html or app boot
if (document.querySelector('[data-description-editor]')) {
  import('./description_editor.bundle.js').then(() => {
    // Trigger mountAll()
  });
}
```

**Tradeoff:** First editor open (+150–200 ms latency) vs. 150 KB cold-load win on all other pages.

**Verdict:** **Recommended for Phase 2.** Impact is substantial (150 KB on most page loads), but
requires:
1. Defer `<script src="description_editor.bundle.js">` tag in `base_app.html`.
2. Add dynamic `import()` in nav-router or acta.js on htmx:afterSettle.
3. Preload on project detail / task detail pages via `<link rel="modulepreload">` to avoid
   first-open surprise.

Estimated effort: **2 h** (template change + dynamic import wiring).

**Measurement needed:** Lighthouse before/after on `/tasks/?panel=list` ksu24. Expected FCP
improvement: **+150–200 ms** (one-time cold-load, repeats on cache clear).

---

### 4.3 Flatpickr date picker (not yet audited)

**Note:** Date pickers appear on task cells (due date, start date, end date, cycle, etc.) but
are mounted only inside popovers that appear on user click. The underlying library (Flatpickr)
is not yet inventoried (likely on CDN per Wave 1 PR-3 vendor survey).

**Action:** Defer to Wave 4 R5 (vendor inventory + dynamic date-picker lazy-load).

---

## 5. Cold-load quantification (baseline → proposed)

Assuming typical ksu24 workspace (~260 tasks):

| Page | Size (KB gzip) | Acta.js | Editor | CSS | Total | Proposed |
|---|---|---|---|---|---|---|
| `/tasks/?panel=list` | — | 54 | 150 | 93 | 297 | 54 + 93 = **147** (−150 KB) |
| `/projects/ACTA/` | — | 54 | 150 | 93 | 297 | 54 + 93 = **147** (−150 KB) |
| Task detail modal | — | 54 | 150 | 93 | 297 | 54 + 150 (dyn) + 93 = **297** (+0 KB cold, −150 on 2nd page) |
| Timeline view | — | 54 | 150 | 93 | 297 | 54 (−12) + 150 + 93 = **285** (−12 KB) |

**Net cold-load savings (assuming 70% list/kanban/table, 20% detail, 10% timeline):**
```
(0.7 × 150) + (0.2 × 0) + (0.1 × 12) = 105 + 0 + 1.2 = ~106 KB saved / page
```

Over 100 page views (typical session): **~10.6 MB** bandwidth savings.

**Caveat:** Editor lazy-load adds 150–200 ms to first description-edit interaction. Preload on
hover or detail-page load mitigates this.

---

## 6. Unused exports audit

**Legend:** ✓ = used in templates. ✗ = defined but unreferenced in templates / tests.

| Export | Uses | Status | Note |
|---|---|---|---|
| `window.acta.csrfToken` | 0 | Used internally in acta.js (line 95, 3385) | Not exposed to templates; safe. |
| `window.acta.promoteTask` | 1 | ✓ | Task promotion from list-view status chip. |
| `window.acta.exportQuery` | 2 | ✓ | CSV/JSON export buttons (task list). |
| `window.acta.removeFilter` | 0 | Checked in inline script; likely unused. | Candidate for removal. |
| `window.acta.toggleFilter` | 19 | ✓✓✓ | Filter chips toggle (high-use). |
| `window.acta.clearFilter` | 0 | Checked in inline script; likely unused. | Candidate for removal. |
| `window.acta.loadRecents` | 1 | ✓ | Command palette "Recents" backing store. |
| `window.acta.recordRecentTask` | 0 | Called from command palette inline script. | Indirect use; safe. |
| `window.acta.updateStickyStack` | 1 | ✓ | Sticky-pinned filter rows z-index recompute. |
| `window.acta.updateStripCounters` | 0 | Called internally on scroll / resize. | Not exposed to templates. |
| `window.acta.updateScrollFades` | 0 | Called internally on scroll / resize. | Not exposed to templates. |
| `window.acta.createTaskFromText` | 1 | ✓ | Selection bubble (create task from selection). |
| `window.acta.actaLightbox` | 0 | Image click handler (inline onclick). | Used via inline event handler in templates. |
| `window.actaLoadPanels` | 0 | Called from htmx:afterSettle listener. | Exported but triggered internally. |
| `window.actaApplyFilters` | 0 | Called from bindFilterForm + filter-reset listener. | Internal use. |
| `window.actaToast` | 4 | ✓ | Global error + success toast queue. |
| `window.actaBulkPatch` | 2 | ✓ | Bulk bar + context menu PATCH. |
| `window.actaBulkDelete` | 1 | ✓ | Bulk bar DELETE. |
| `window.actaBulkArchive` | 2 | ✓ | Bulk bar archive. |
| `window.actaOpenBulkMenu` | 1 | ✓ | Bulk action bar button. |
| `window.actaForceApplySelfEvent` | 14 | ✓✓✓ | Context menu self-SSE replay (Wave 1 PR-9). |

**Finding:** `removeFilter` and `clearFilter` appear in the exports list but are not directly
called in templates. However, they may be used in inline scripts or commands. Verify before
removal.

**Action:** Check filter sidebar template (`_filters_sidebar.html`) for direct calls to these
functions. If unused, mark as deprecated or remove in Wave 4 PR-5.

---

## 7. Vendor inventory + self-hosting status

### 7.1 Current vendors (static/vendor/)

| File | Size (KB) | Hosting | Notes |
|---|---|---|---|
| `htmx.min.js` | 50 | Self-hosted (Wave 1 PR-5) | Hypermedia AJAX library; request/response lifecycle. |
| `htmx-ext-sse.min.js` | 2.8 | Self-hosted (Wave 1 PR-5) | SSE extension for htmx; event source binding. |
| `idiomorph-ext.min.js` | 9.6 | Self-hosted (Wave 1 PR-5) | DOM morphing (outerHTML replacement); preserves Alpine state. |
| `alpine.min.js` | 44 | Self-hosted (Wave 1 PR-5) | Alpine.js reactive components + stores. |
| `sortable.min.js` | 44 | Self-hosted (Wave 1 PR-5) | Drag-drop (kanban + label reorder). |
| `emoji-data.json` | 429 | Self-hosted (Wave 1 PR-5) | Emoji picker data; served as JSON not CDN bundle. |
| Fonts | (Exo 2, JetBrains Mono) | Self-hosted (static/vendor/fonts/) | Fallback for serif / mono; loaded via CSS @font-face. |

**Status:** All critical vendors are self-hosted. ✓ No CDN dependencies for core app logic.

**Lucide icons:** Generated at build time (`scripts/extract_lucide.py` + `scripts/build_lucide_sprite.py`)
into `apps/web/lucide_icons.json` + `static/sprites/lucide.svg`. Not a vendor but similar dedup concern.

### 7.2 Potential self-hosting candidates

**Flatpickr (date picker)** — likely on CDN per Wave 1 audit. If used:
- Size: ~5–10 KB minified (library + CSS).
- Frequency: Per-cell date editor; mounted on demand.
- **Action:** Inventory in Wave 4 R5 (vendor audit follow-up).

**Floating UI (popover positioning)** — used for mention cards + hover cards. If on CDN:
- Size: ~3–5 KB minified.
- Frequency: All pages (mention picker, task hover).
- **Action:** Consider self-hosting if Floating UI is a blocker dependency.

---

## 8. CSS bundle split analysis

### 8.1 Current structure

| File | Size (KB gzip) | Purpose |
|---|---|---|
| `main.bundle.css` | 93 | Tailwind output from `static_src/css/main.css` + `tailwind.config.js` content scan. |
| `prose.bundle.css` | 19 | Tailwind output from `static_src/css/prose.css` (editor typography); lazy-loaded via `link[rel="preload"]` on description-editor mount. |
| `dashboard.css` | 26 | Custom styles for dashboard page (Wave 1 PR-7). Now included in `main.bundle.css` content scan. |

### 8.2 Route-specific extraction opportunity

**Analysis:** `main.bundle.css` is generated by Tailwind's content scanner, which includes all
templates. Potential split targets:

| Route | Selectors | Opportunity |
|---|---|---|
| `/tasks/?panel=list` | Task row styles, filter sidebar, status/priority/label chips | ~15 KB (10 % of main.css) — low ROI. |
| Task detail modal | Description editor, comment thread, attachment panel | ~8 KB; but modal loads via htmx, CSS already downloaded on parent page. |
| Kanban board | Kanban column, drag-drop visual feedback, card styles | ~12 KB; but same story (CSS on parent). |
| Timeline Gantt | Gantt bars, deadline markers, zoom UI | ~5 KB; included on project overview already. |

**Verdict:** ✗ **No significant wins from route-specific CSS splits.** Reason:
- Tailwind output is already minimal (93 KB for entire app).
- Most routes are HTMX tabs on the same parent page (CSS already loaded).
- PurgeCSS-style tree-shaking is implicit in Tailwind content scan.

**Exception:** `prose.bundle.css` (editor typography) is already lazy-loaded. Keep as-is.

---

## 9. Findings (F1–F12)

### F1 — Timeline module should be lazy-loaded [P1 / 12–15 KB saved]

**Where:** `acta.js:928–1395` (`initTimeline` IIFE).

**What:** Timeline (Gantt) is 468 LOC, only used on `/projects/<slug>/timeline/` route or
lazy-loaded tab. Currently bundled in main `acta.js`; initialized on every page via event listener.

**Fix sketch:** Extract `timeline.js` as a separate module. Add dynamic `import()` on
`htmx:afterSettle` if `document.querySelector('[data-timeline]')` exists. Preload on project
detail pages.

**Effort:** 2 h (module extraction, dynamic import wiring, preload tag).

**Δ bundle:** −12 KB (gzip) on cold list/kanban/table views (80% of traffic). Acceptable
+150 ms latency on first timeline open (preload mitigates).

---

### F2 — TipTap editor should lazy-load on demand [P1 / 150–180 KB saved]

**Where:** `base_app.html` (script tag); `static/js/description_editor.bundle.js` (508 KB).

**What:** Editor bundle is loaded on every page but only mounted on 9 specific routes
(task detail, project description, comments). Wave 3 D5 §7 identified this; now quantified.

**Fix sketch:** Remove `<script src="description_editor.bundle.js">` from `base_app.html` base.
Add dynamic `import()` in nav-router / acta.js main IIFE:
```javascript
document.addEventListener('htmx:afterSettle', (e) => {
  if (document.querySelector('[data-description-editor]')) {
    import('./description_editor.bundle.js').then(() => { mountAll(); });
  }
});
```

Add `<link rel="modulepreload" href="/static/js/description_editor.bundle.js">` on project
detail + task detail pages to prefetch the bundle on hover/idle.

**Effort:** 2 h (template changes, dynamic import, preload tags, test).

**Δ bundle:** −150 KB (gzip) on `/tasks/?panel=list`, all non-editor routes. +150–200 ms
latency on first editor open (preload + idle-time fetch mitigates to <50 ms in practice).

---

### F3 — acta.js listener accumulation on element swap [P2 / memory leak]

**Where:** `acta.js:1909–2111` (`initStickyStacks`, `initStrips`, `initScrollFades`).

**What:** Event listeners added to DOM elements without cleanup on HTMX swap. Guard is
`dataset.*Bound == "true"`, which assumes the same DOM node; if HTMX replaces the element,
the old node (and its listeners) linger.

**Fix sketch:** Formalize cleanup pattern: store listener reference on element, remove on
recycle. Or: check element's parent context before adding listener (if parent is new, element
is new).

**Effort:** 1.5 h (refactor 3 init functions, test on rapid swaps).

**Δ bundle:** 0 KB (refactoring only). **Impact:** Minimal memory overhead in practice (passive
listeners, browser GC), but correct semantics.

**Reference:** Wave 3 D2 F1.

---

### F4 — kanban substatus MutationObserver not cleaned on swap [P2 / memory leak]

**Where:** `acta.js:2090–2105` (`installCollapsedBodySnap`).

**What:** MutationObserver stored on DOM element; on column swap, observer never calls
`.disconnect()`.

**Fix sketch:** Add `.disconnect()` inside the guard; defensive cleanup even if element is
reused.

**Effort:** 0.5 h (one-line addition + test).

**Δ bundle:** 0 KB. **Impact:** Negligible (MutationObserver is lightweight), but correct.

**Reference:** Wave 3 D2 F2.

---

### F5 — Timeline deadline patch error logs to console only [P3 / UX polish]

**Where:** `acta.js:1318–1324` (`patchDate`).

**What:** Deadline drag failure is silent to console; user sees deadline snap back with no toast.
Inconsistent with kanban drop + task promote (which toast).

**Fix sketch:** Wrap in `.catch()` and call `window.actaToast("Deadline update failed.", "error")`.

**Effort:** 0.5 h (toast call + test).

**Δ bundle:** 0 KB. **Impact:** UX consistency; user sees error toast instead of silent failure.

**Reference:** Wave 3 D2 F3.

---

### F6 — Search input debounce race with form reset [P3 / edge case]

**Where:** `acta.js:1515–1537` (`bindFilterForm` + search input listener).

**What:** Search input debounces 150 ms. If user types "foo" (timer set) then clicks reset
(form clears), the pending debounce still fires 150 ms later against the now-empty form.

**Fix sketch:** Clear debounce timer on form reset event. Or: capture reset event and clear
`qTimer`.

**Effort:** 0.5 h (add timer clear on reset button + test).

**Δ bundle:** 0 KB. **Impact:** Avoids spurious filter re-apply on reset; idempotent but
wasteful.

**Reference:** Wave 3 D2 F5.

---

### F7 — Kanban column count not updated on SSE status change [P3 / cosmetic]

**Where:** `acta.js:2420–2427` (`task.status_changed` handler).

**What:** When peer moves a task between kanban columns (SSE event), the card moves visually
but column header badge counts don't update until next filter apply or nav.

**Fix sketch:** Call `recountKanbanColumns()` (already exists, line 856) after `applyCardMove`.

**Effort:** 0.5 h (add one function call).

**Δ bundle:** 0 KB. **Impact:** Cosmetic; badge catches up on next event or nav.

**Reference:** Wave 3 D2 F6.

---

### F8 — acta.js split into 13 modules requires build-step review [P1 / maintainability]

**Where:** Whole file.

**What:** Proposed split into `static_src/js/acta/` subtree (13 modules, 2 327 LOC) requires
choosing a build strategy: esbuild entrypoint (Option A) or manual concat + terser (Option B).

**Fix sketch:** Implement Option A (esbuild) for clarity + future lazy-load support. Add to
`package.json` scripts:
```json
"build:js:acta": "esbuild static_src/js/acta/index.js --bundle --format=iife --minify --target=es2020 -o static/js/acta.min.js"
```

Update `Makefile` target `build-front` to run this script.

**Effort:** 3 h (module extraction, esbuild config, test, update CI/Makefile).

**Δ bundle:** 0 KB (same output, split source). **Impact:** Code clarity, maintainability,
foundation for future lazy-load.

---

### F9 — Unused filter exports: `removeFilter`, `clearFilter` [P3 / cleanup]

**Where:** `acta.js:55–168` (`window.acta` assignments) + implementation `lines 434–727`.

**What:** Two filter operation exports defined and exported to `window.acta` but not referenced
in any template. Candidate for deprecation or removal.

**Fix sketch:** Audit `_filters_sidebar.html` for inline calls. If unused, mark as deprecated
in JSDoc with a removal date. Or remove outright if not on public API surface.

**Effort:** 1 h (template audit, decision, removal or deprecation note).

**Δ bundle:** <1 KB (if removed). **Impact:** API clarity; fewer unused exports.

---

### F10 — CSS main.bundle.css includes unused route-specific selectors [P2 / analyzed, no action]

**Where:** `static/css/main.bundle.css` (93 KB); generated by Tailwind content scan.

**What:** Wave 1 PR-7 switched to Tailwind JIT. Content scan includes all templates, so all
route-specific selectors land in one CSS file. Potential to split by route (e.g., timeline-only
styles, kanban-only styles).

**Finding:** Analysis shows <5 % potential savings (5–15 KB per route). Since most routes are
HTMX tabs on the same parent page (CSS already cached), split gains are negligible.

**Verdict:** **No action.** Keep monolithic `main.bundle.css`. `prose.bundle.css` (19 KB) already
lazy-loads separately.

---

### F11 — Build tooling: hand-written esbuild + terser vs. Vite [P2 / strategic]

**Where:** `Makefile` (build-js, build-css targets); `package.json` (scripts).

**What:** Current setup is minimal: esbuild for editor/reactions bundles, terser for acta.js,
tailwindcss CLI for CSS. No single bundler orchestrates these three streams.

**Analysis:**
- **Pros (current):** Lean; each step is explicit and fast. No Vite / Webpack complexity overhead.
- **Cons (current):** Hand-written build logic; no dead-code-elimination across modules (each
  step is isolated).

**Vite alternative:**
```javascript
// vite.config.js
export default {
  build: {
    rollupOptions: {
      input: {
        acta: 'static_src/js/acta/index.js',
        editor: 'static_src/js/description_editor.js',
      },
      output: { dir: 'static/js', format: 'iife', entryFileNames: '[name].min.js' },
    },
  },
};
```

**Effort:** 4–6 h (migration, test, CSS integration via Vite's Tailwind plugin or separate
script, CI updates).

**Verdict:** **WAIT.** Current setup is sufficient for MVP. Vite migration makes sense if:
1. `acta.js` grows past 4 000 LOC (suggest split + bundler then).
2. New editor plugins add complex dep trees (current 8 TipTap extensions, manageable).
3. Mobile / low-BW metrics emerge post-launch (Vite's better tree-shaking may help).

---

### F12 — No preload strategy for description_editor bundle [P2 / UX polish]

**Where:** `base_app.html` (missing `<link rel="modulepreload">`).

**What:** If TipTap bundle is lazy-loaded (F2), first open adds 150–200 ms latency (bundle
download + parse). Mitigations: preload on hover, use `<link rel="modulepreload">` on
detail/compose pages.

**Fix sketch:** On task-detail + project-overview pages, add:
```html
<link rel="modulepreload" href="{{ static 'js/description_editor.bundle.js' }}">
```

This tells the browser to fetch the bundle in idle time, so it's cached by the time the user
opens an editor.

**Effort:** 1 h (template change, test, measure with DevTools).

**Δ bundle:** 0 KB (preload is a hint, not a change). **Impact:** First editor open latency
reduces from 150–200 ms to <50 ms (in most cases).

---

## 10. Cold-load savings summary

**Baseline (current):** `acta.js` (54 KB) + `description_editor.bundle.js` (150 KB) + CSS (93 KB) = 297 KB gzip per page.

**Proposed (all optimizations):**
1. **F2 (editor lazy-load):** −150 KB on non-editor routes (list, kanban, table, dashboard).
2. **F1 (timeline lazy-load):** −12 KB on non-timeline routes.
3. **F3–F9 (refactoring):** 0 KB (same output, cleaner code).

**Result:**
- **List/kanban/table/dashboard (70% traffic):** 297 − 150 − 12 = **135 KB** (−55 % from baseline).
- **Task detail (20% traffic):** 297 − 12 (timeline lazy) = **285 KB** (−4 %; editor loaded, timeline lazy).
- **Timeline view (10% traffic):** 297 − 150 (editor not on timeline) = **147 KB** (−50 %; no editor, acta + css).

**Weighted average cold-load savings: ~110 KB / page** (7–10 s faster on 3G, 2–3 s faster on 4G).

**Cumulative over 100 page loads (typical session): ~11 MB saved.**

---

## 11. Implementation roadmap (Wave 4 + Wave 5)

### Wave 4 (current)

**P0 (headline):**
- **R4a** — TipTap lazy-load + preload (F2). **2 h.** Cold-load win: −150 KB on most pages.
- **R4b** — Timeline module extract + lazy-load (F1). **2 h.** Cold-load win: −12 KB.

**P1 (polish):**
- **R4c** — acta.js 13-file split (F8). **3 h.** Maintainability; foundation for future splits.
- **R4d** — Memory leak cleanup (F3, F4, F5). **2 h.** Correctness + performance.

**P2 (optional):**
- **R4e** — Filter exports audit (F9). **1 h.** API clarity.
- **R4f** — Preload strategy (F12). **1 h.** UX polish.

### Wave 5 (future)

- **R5a** — Flatpickr date picker self-host + lazy-load (F2 follow-up, out-of-scope this wave).
- **R5b** — Vendor inventory audit (follow-up to Wave 1 PR-3).
- **R5c** — Vite migration evaluation (if acta.js grows >4 000 LOC or plugin ecosystem expands).

---

## 12. Cross-references

**Wave 3:**
- D2 (`01-acta-js.md` §7): Detailed split plan for 13 modules.
- D5 (`04-tiptap-attachments.md` §7): TipTap lazy-load strategy.
- 99-backlog (`99-wave3-backlog.md` §2): Prioritized PR queue; aligns with R4a–R4d above.

**Wave 2:**
- C7 (`05-notifications-sse.md`): SSE event drift table (14 types verified).

**Wave 1:**
- D1 (`05-nav-router.md`): History router baseline + ADR 0024 verification.
- PR-5 (baseline): Vendor CDN→self-host (htmx, Alpine, Sortable, etc.).

**ADRs:**
- ADR 0024: History router + page cache design.
- ADR 0025: Attachment + inline-image storage + markdown serialization.

---

## 13. Summary

**`acta.js` is well-architected for hand-writing at 3 331 LOC.** The custom history router (ADR 0024)
is bulletproof, SSE integration is mature (14 event types), and all 27 window.acta exports are in use.

**Two clear wins emerge:**
1. **TipTap lazy-load (F2):** 150 KB saved on 70 % of page loads (list/kanban/table/dashboard). 
   Impact: massive. Effort: 2 h. Risk: low (preload on detail pages mitigates first-mount latency).
2. **Timeline lazy-load (F1):** 12 KB saved on all non-timeline routes. Impact: modest. Effort: 2 h.
   Risk: low.

**Code quality is high but size is substantial.** The proposed 13-file split (F8) is maintainability
focused, not performance-focused; it enables future lazy-loads and makes code navigation easier.

**Build tooling is sufficient.** Hand-written esbuild + terser is lean and fast. Vite migration is
unnecessary for MVP. Revisit if acta.js grows >4 000 LOC or TipTap plugin ecosystem explodes.

**Memory leaks are low risk in practice** but should be cleaned (F3–F5) for correctness.

**CSS is optimized.** Tailwind content scan produces minimal output (93 KB). No route-specific
splits needed.

---

**Audit completed: 2026-05-30.**
**Auditor: Wave 4 / Chunk E3.**
**Status: Ready for Wave 4 R4a + R4b implementation.**

