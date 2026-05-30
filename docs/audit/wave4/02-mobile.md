# 02 — Mobile / touch / viewport audit (E2)

> **Wave 4 / Chunk E2** — Read-only.  
> Date: 2026-05-30.  
> **Status: NOT MOBILE-FIRST. Audit for mobile-viewable baseline per `[[project-todo-mobile-viewable]]`.**

---

## 1. Executive summary

Acta is a **desktop-first** application with explicit non-goals around mobile optimization. This audit surveys mobile-viewable gaps—where the layout breaks, touch affordances fail, or responsive patterns are incomplete—without expecting mobile-optimal design.

**Key findings:**

- **Viewport meta tag** is correctly configured (`width=device-width, initial-scale=1`).
- **Breakpoint usage** is sparse: `md:` (≥768px) for sidebar hide, `lg:` (≥1024px) for filter panels, `sm:` (≥640px) for a few text fields. **No explicit mobile-first scaffolding.**
- **Layout breakage at narrow viewports:** sidebar forced hidden below 768px (no hamburger menu on <768px), command palette at max-width 640px on small screens (no fallback).
- **Touch-hostile patterns:** 31+ instances of `group-hover` / `opacity-0 group-hover:opacity-100` (controls invisible until hover), text-xs (12px) clickable elements, drag-and-drop kanban lacks touch gesture handling.
- **Critical mobile gap:** No mobile navigation drawer/FAB on <768px viewports (Wave 3 D6 §8 noted this).
- **Form inputs:** Native `<input type="date">` on iOS Safari, no `inputmode` on numeric/search fields, no `enterkeyhint`.
- **Modals:** centered, 640px max-width—OK on 375px viewport but full-screen fallback would improve usability.
- **Overall impression:** The app is **readable and navigable on phones** but **not touch-optimized**; interactivity is dense (small targets, hover-dependent), and critical navigation is desktop-only.

**Recommendation:** Ship the **mobile drawer/hamburger FAB** (Wave 4) as the anchor for "mobile-viewable." Defer full touch polish (target sizing, group-hover replacement, gesture handling) to Wave 5.

---

## 2. Detailed findings

### F1 — Sidebar invisible below `md:` breakpoint, no mobile fallback [P1]

**Where:** `templates/base_app.html:43`, `static_src/css/main.css` (modal backdop `pt-16`).

**What:**  
The sidebar `<aside>` carries `class="hidden md:flex"`, making it completely invisible on `<768px` viewports. There is no hamburger menu / FAB / bottom-bar navigation entry point below `md`. On a 375px or 414px phone viewport, the app shell is:

```
┌─ header (topbar) ─────────────────────────┐
│  [hamburger (hidden)]  [search] [+] [...] │
├───────────────────────────────────────────┤
│                                           │
│  [main content: my-work / all-tasks]      │
│  (full width, no nav access)              │
│                                           │
└───────────────────────────────────────────┘
```

Users cannot navigate to Inbox, My Activity, Projects, or switch workspaces below 768px. The hamburger button in the topbar is also hidden (`class="hidden md:grid"`).

**Why it matters:**  
Mobile users are locked into whatever page they landed on. Workspace switching, key navigation paths (Dashboard, Inbox, My Work) all require manual URL entry or browser history.

**Fix sketch:**  
Unconditionally render the hamburger button (remove `md:` hide). Add a bottom-bar or slide-in drawer on `<md` viewports. OR: render a sticky FAB (e.g. "hamburger + badge" fixed to bottom-right) that opens a full-screen or half-screen drawer with the sidebar nav. The sidebar should be `hidden` below `md` OR styled as a drawer overlay. See `[[project-todo-mobile-viewable]]` for the intended pattern.

**Effort:** 4–6 hours (drawer template, CSS transitions, Alpine state wire-up, topbar button fixes).

---

### F2 — Command palette modal doesn't fit <375px, keyboard hint hidden below `sm:` [P2]

**Where:** `templates/web/_command_palette.html:29-39`, line 94 (kbd hidden on `<sm`).

**What:**  
The palette container is `class="… w-full max-w-xl …"` (`max-w-xl` = 36rem = 576px). On a 375px iPhone:

- Palette width = min(375px - 2×16px padding, 576px) = 343px. OK.
- **Esc kbd hint hidden below `sm:` (≤639px):** `<kbd class="hidden sm:inline-flex">`
- **Footer hints always shown,** but stack vertically on <414px—not critical.

**Why it matters:**  
Mobile users who hit Cmd+K (unlikely, but possible if they find the search button) see no Esc hint. Minor discoverability gap.

**Fix sketch:**  
Change the footer `<kbd>` sections to show on all viewports (remove `sm:` gate), or render a mobile-specific close hint (e.g., tap-outside or an explicit close button instead of a kbd). Or ensure the input is focused so Esc is still discoverable.

**Effort:** 0.5 hours (template edit).

---

### F3 — Filter sidebar collapses awkwardly on narrow viewports, no mobile drawer [P2]

**Where:** `templates/web/_filters_sidebar.html:44-100`.

**What:**  
The filter sidebar structure is:

```html
<aside class="acta-flt-aside-v2 … lg:mt-0 lg:-mr-8 lg:-mb-6" 
       :data-mode="$store.filters.mode">
```

Below `lg:` (1024px), the sidebar is in "rail mode" (collapsed, vertical). On mobile (<768px), it stacks **below** the main content or becomes a horizontal rail. The CSS shows:

```css
.acta-flt-aside-v2[data-mode="rail"][data-open="status"] { /* 288px popover */ }
.acta-flt-aside-v2[data-mode="expanded"] { /* 512px panel */ }
```

On a 375px viewport, a 288px or 512px popover is **larger than the viewport width**. The popover is positioned `position:fixed` or positioned absolutely within a scrolling parent—either way, it likely **overflows the viewport horizontally or gets clipped by a parent's `overflow:hidden`.**

**Why it matters:**  
Mobile users can't access filters on narrow screens without horizontal scrolling or awkward positioning. Filters are essential (search, status/priority/assignee chips).

**Fix sketch:**  
Below `md:`, render filters as a **full-screen drawer** or a **bottom sheet** instead of a side popover. Use `data-mode="mobile-drawer"` with CSS `@media (max-width: 767px)` to trigger alternative layout. The expanded vs. rail toggle still works; just make the drawer dimensions screen-relative (e.g., 90vw max on narrow).

**Effort:** 3–4 hours (CSS media query for drawer, Alpine state for drawer-specific close, touch-outside dismiss).

---

### F4 — 31+ `group-hover` + `opacity-0` controls invisible on touch [P1]

**Where:** Multiple templates. Key examples:
- `templates/web/_task_row.html:48-50` — "promote task" button `opacity-0 group-hover:opacity-100`.
- `templates/web/projects/_due_date_cell.html` — clear due-date ✕ button.
- `templates/web/projects/_comment_actions.html:3` — edit/delete comment buttons.
- `templates/web/_notification_row.html` — delete/archive icons.
- `templates/web/projects/_links_panel.html` — delete link ✕.

**What:**  
Controls are hidden until the **parent group is hovered**. On touch (phones, tablets), there is no "hover"—the controls are **permanently invisible**. Example from `_task_row.html`:

```html
<span class="opacity-0 group-hover:opacity-100 cursor-pointer inline-flex …"
      @click.prevent.stop="window.acta.promoteTask(…)">
  Promote to Ready
</span>
```

On desktop, hover the row → button appears → click. On mobile, button never appears. Users cannot promote tasks, delete comments, or clear due dates.

**Why it matters:**  
**Critical interactivity loss on touch.** These are not optional affordances; they are the primary way to mutate task state inline.

**Fix sketch:**  
Replace `group-hover` visibility with **always-visible** buttons on mobile. Use CSS:

```css
.group { /* keep hover on desktop */ }
@media (hover: none) or @media (pointer: coarse) {
  .group-hover:opacity-100 { opacity: 1 !important; }
}
```

OR restructure: make the button always visible (opacity-100) and adjust its styling (color, size) based on hover state. OR: add a long-press menu on touch to expose actions.

**Effort:** 6–8 hours (audit 31+ instances, implement touch media query, test across components, ensure visual balance on desktop).

---

### F5 — Kanban drag-and-drop doesn't work on touch [P1]

**Where:** `templates/web/projects/_kanban.html:1-150`, `static_src/js/acta.js::initKanbanDnD`.

**What:**  
The kanban board uses `sortable.js` (v1.15.2, self-hosted). Sortable does **not** have native touch support enabled by default. The init code in `acta.js` likely doesn't include:

```javascript
new Sortable(el, {
  touchStartThreshold: 0,
  delayTouchStart: 100,  // or 0 for aggressive touch
  // ... other options
});
```

**Result:** Kanban board is **unsorting-able on touch devices.** Drag-initiation is a desktop mouse gesture. Long-press may work with polyfill, but it's not configured.

**Why it matters:**  
The kanban is a primary workflow view (project detail page defaults to kanban). Mobile users can view tasks but cannot move them between columns.

**Fix sketch:**  
Enable Sortable's touch support:

```javascript
new Sortable(el, {
  touchStartThreshold: 5,  // pixels of movement to initiate drag
  delayTouchStart: 200,    // ms before drag starts on touch
  ...
});
```

Add visual feedback (task card briefly highlights or scales up) when a touch-drag begins. Test on iPad + iPhone.

**Effort:** 2–3 hours (tweak sortable config, test drag interaction, add visual feedback).

---

### F6 — Inline cell edit targets are too small: 32px / 28px on 12px text [P2]

**Where:** `templates/web/projects/_status_cell.html`, `_priority_cell.html`, `_assignee_cell.html`, etc.

**What:**  
Inline edit cells (status badge, priority icon, assignee avatar) have these dimensions:

| Element | Class | Effective size |
|---------|-------|---|
| Status badge | `text-[10px] uppercase px-1.5 py-0.5 rounded` | ~32×20px |
| Priority icon | `w-3.5 h-3.5` | 14×14px |
| Assignee avatar | `w-5 h-5` | 20×20px |
| Kanban card "+" button | `p-2` | 32×32px |

Apple's touch target minimum is **44×44px**; Material Design recommends **48dp**. Most inline cells are **14–32px**, falling short of accessibility baseline.

**Why it matters:**  
Mobile users' fingers are ~8–10mm wide. A 14px target (10.5mm at 96 DPI) is hittable but error-prone, especially in a list of tightly-packed rows.

**Fix sketch:**  
Increase touch-target minimum using `min-h-[44px] min-w-[44px]` on clickable elements below `md:`. Use invisible padding or expand the hit-test area without changing visual layout (`:hover` can shrink the visual element). OR: switch to a context menu / action sheet on touch (less clutter).

**Effort:** 3–4 hours (identify all inline cells, apply min-size utilities, visual QA).

---

### F7 — Table view (`_table_row.html`) is unreadable below 768px due to horizontal scroll [P2]

**Where:** `templates/web/projects/_table.html` (not shown in audit), `_table_row.html:1-166`.

**What:**  
The table has **~12 columns**: checkbox, slug, title, status, size, labels, assignee, project, due date, updated. On a 375px phone:

- Each cell: `px-3 py-2` = min 36px padding.
- Status badge: 32px.
- Avatar: 20px.
- Total minimum: 12 × 36px = 432px > 375px viewport.

**Result:** Full horizontal scroll required. On phones, users scroll sideways repeatedly to see all task details. Compare with the list view, which reflows naturally.

**Why it matters:**  
Table view is unusable on mobile. Users are forced to the list view, losing the flexibility of column sorting / filtering.

**Fix sketch:**  
Below `md:`, hide columns 3–7 (size, labels, project, updated); show only slug, title, status, assignee. Add a "+" expander or tap-to-expand row for hidden columns. OR: switch to a card-based detail below `md:`.

**Effort:** 2–3 hours (column visibility toggle, CSS, optional expander UX).

---

### F8 — Timeline (Gantt) is 100% unviewable below 768px [P1]

**Where:** `templates/web/projects/_timeline.html:1-455`, inline `<style>` (Gantt CSS Grid).

**What:**  
The timeline has a **left sticky column (260px)** (`style="width:260px"`) for task names and a **horizontal scrolling grid** (CSS Grid, date columns). On a 375px viewport:

- Left column: 260px.
- First date column: min 60px.
- Total: 260px + 60px = 320px < 375px? Actually fits on 1-col display.
- BUT: The viewport is `width: 100vw`, and the left sidebar takes 56–64px even when collapsed.
- **Actual available width: 375px - 64px (collapsed sidebar) = 311px < 260px.**

The left column would be **clipped or cause horizontal scroll**. Users can't see task names.

**Why it matters:**  
Timeline view is a key project-planning interface. It's entirely inaccessible on phones.

**Fix sketch:**  
Below `md:`, **hide the timeline** or render a **mobile-adapted timeline**:
- Stack rows vertically (task name, inline start/end dates, no grid bars).
- OR: collapse to a **compressed list** with dates in the row.
- OR: show a **start/end date mini-gantt** (just the bar, no date grid) that scrolls left/right independently.

**Effort:** 4–5 hours (alternative mobile layout, responsive container queries or media rules).

---

### F9 — Dashboard (`_dashboard_inner.html`) layout breaks below 768px [P2]

**Where:** `templates/web/_dashboard_inner.html:1-300+`, custom CSS (`.dash`, `.kpis`, `.sec`, etc.).

**What:**  
The dashboard has custom CSS classes (not Tailwind). Layout elements:

| Component | CSS class | Behavior |
|-----------|-----------|----------|
| KPI cards | `.kpis` (grid) | Multiple cards in a row; likely `grid-cols-2` or `grid-cols-4`. Unspecified below `md:`. |
| Section title | `.sec-h` | 2-column grid (title + toggle buttons). Unspecified below `md:`. |
| Charts (Pipeline + CFD) | `.g-pipeline` (grid) | 2-column grid. No responsive rule visible. |

On 375px, a 4-column KPI grid would be **12 cells wide**, requiring huge horizontal scroll or extreme squishing.

**Why it matters:**  
Dashboard is one of the primary entry points (sidebar Dashboard link). Mobile users see a broken layout with illegible charts and cards.

**Fix sketch:**  
Check the compiled `static/css/main.bundle.css` for `.kpis { grid-cols: … }`. Add responsive:

```css
.kpis { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
@media (max-width: 767px) {
  .kpis { grid-template-columns: 1fr; }
  .sec-h { flex-direction: column; }
  .g-pipeline { grid-template-columns: 1fr; }
}
```

**Effort:** 1.5–2 hours (inspect compiled CSS, add media queries, visual QA).

---

### F10 — Form inputs lack mobile affordances: no `inputmode`, no `enterkeyhint` [P3]

**Where:** Multiple form templates. Examples:
- `templates/web/_command_palette.html:75` — search input (no `inputmode="search"`).
- `templates/web/projects/_due_date_cell.html` — date input (native `.type="date"`, which is OK, but no explicit `inputmode`).
- `templates/web/projects/_bulk_context_menu.html` — date input (no `inputmode="date"`).
- Any form with numeric fields (e.g., cycle number) — no `inputmode="numeric"`.

**What:**  
Mobile browsers expose soft keyboards (on-screen keyboards). Without hints:

- Search input → generic keyboard (full qwerty, no search affordance).
- Date input → generic keyboard or native picker (depends on browser).
- Numeric input → generic keyboard (no number pad shortcut).

iOS and Android have reserved `inputmode` values:
- `inputmode="search"` → search key instead of Enter.
- `inputmode="email"` → email layout (@ key visible).
- `inputmode="tel"` → numeric keypad.
- `inputmode="numeric"` → number pad (decimal OK).
- `enterkeyhint="search"` / `"next"` / `"send"` → custom soft key label.

**Why it matters:**  
Typing is slower on mobile without the right keyboard. Users typing search queries or dates see a full keyboard when a numeric/email/search keyboard would be faster.

**Fix sketch:**  
Audit forms and add `inputmode` + `enterkeyhint` to all `<input>` fields:

```html
<input type="search" inputmode="search" enterkeyhint="search" … />
<input type="text" inputmode="text" enterkeyhint="next" … />
<input type="number" inputmode="numeric" enterkeyhint="done" … />
<input type="date" inputmode="numeric" enterkeyhint="done" … />
```

**Effort:** 1–2 hours (search codebase, apply to 30–50 inputs).

---

### F11 — Modal panel doesn't fill viewport on <375px; potential content cutoff [P2]

**Where:** `templates/web/_modal_shell.html:30`, `static_src/css/main.css` (`.acta-modal-backdrop`, `.acta-modal-panel`).

**What:**  
Modal panel is defined as:

```html
<div class="acta-modal-panel max-w-md …"  <!-- max-w-md = 28rem = 448px -->
```

Backdrop is `pt-16 px-4` (top padding + 4px sides). On a 375px viewport:

- Available width: 375px - 2×16px = 343px.
- Panel width: min(343px, 448px) = 343px. ✓
- **Backdrop padding-top: 64px** (4rem).

On a 375×667 iPhone SE (short viewport), with the topbar (56px) + modal top padding (64px) = 120px burned before modal content appears. Modal content area is ~547px tall. **Acceptable but tight.**

However, **the modal height is not constrained**. A long form (create task) with many fields would overflow the viewport vertically, requiring **vertical scroll inside the modal**. This works, but the experience is worse than a full-screen modal on mobile.

**Why it matters:**  
Creating tasks on mobile (create-task modal) is cramped. Users scroll vertically inside the modal, then horizontally to reach button actions (Cancel / Create). It works, but it's not optimized.

**Fix sketch:**  
Below `md:`, make modals **full-screen** (inset-0) or **full-height** (top-0 bottom-0). Reduce padding-top to `pt-8` or `pt-4`. Keep max-width unconstrained below `md:`.

```css
@media (max-width: 767px) {
  .acta-modal-backdrop { @apply pt-4 px-3; }
  .acta-modal-panel { @apply max-w-none; max-height: 100vh; }
}
```

**Effort:** 1 hour (CSS media rule).

---

### F12 — Workspace switcher dropdown and avatar menu unreachable/tiny on narrow viewports [P3]

**Where:** `templates/base_app.html:167-220` (workspace switcher), `320-346` (avatar menu).

**What:**  
Both dropdowns are positioned at the bottom or right of a button that's only visible on `md:` (sidebar hidden). On mobile, the **hamburger menu is hidden** (F1), so users can't access the workspace switcher or account settings.

**Why it matters:**  
Mobile users can't switch workspaces or access account settings. This is a secondary concern (less critical than navigation), but completeness matters.

**Fix sketch:**  
Once the mobile navigation drawer (F1) is implemented, move workspace switcher + avatar menu into the drawer. OR: add them to a mobile top-bar dropdown.

**Effort:** 1–2 hours (once F1 is done).

---

### F13 — Bulk selection bar at bottom center is hard to dismiss on narrow viewports [P3]

**Where:** `templates/base_app.html:516-552` (bulk action bar).

**What:**  
The floating bulk-action bar sits `bottom-5 left-1/2 -translate-x-1/2` (centered-bottom). On a 375px viewport, the bar is ~320px wide (padding + items). The bar displays:

- [Selected count chip]
- [divider]
- [Actions button]
- [divider]
- [Clear selection button + Esc hint]

The Esc key works to dismiss, but there's no visible close button. On mobile, users must hit Esc (unlikely) or select a different row to deselect (opaque).

**Why it matters:**  
Minor UX friction. Users who multi-select tasks may not know how to deselect.

**Fix sketch:**  
Add a visible ✕ button to the bar. Or: make the bar swipeable to dismiss (downward swipe). Or: add a small close × icon to the right end.

**Effort:** 0.5–1 hour (template change).

---

## 3. Breakpoint inventory

| Breakpoint | Tailwind | Viewport | Usage in codebase |
|---|---|---|---|
| `sm:` | 640px | — | 4 instances: command palette esc hint, inbox announce label, cycles overview grid, settings form grid |
| `md:` | 768px | **Primary mobile cutoff** | 30+ instances: sidebar hide/show, topbar hamburger, main content padding, project strip padding, filter sidebar toggle, dialog padding |
| `lg:` | 1024px | Desktop | 20+ instances: filter sidebar expanded mode, task detail grid, my-work/all-tasks layout switch |
| `xl:` | 1280px | Desktop | 3 instances: task detail sidebar sticky position, settings 2-column grid |
| `2xl:` | 1536px | Desktop | 1 instance: settings columns |

**Gap:** No `xs:` (< 640px) or mobile-first cascade. The app assumes a minimum viewport of ~320px but doesn't explicitly scaffold for it.

---

## 4. Summary table

| Finding | Severity | Category | Effort | Shipped? |
|---------|----------|----------|--------|----------|
| F1: Sidebar invisible <768px, no fallback | P1 | Navigation | 4–6h | No |
| F2: Cmd+K palette kbd hint hidden <640px | P2 | UX Polish | 0.5h | No |
| F3: Filter sidebar overflow on mobile | P2 | Navigation | 3–4h | No |
| F4: 31+ group-hover controls invisible on touch | P1 | Interactivity | 6–8h | No |
| F5: Kanban drag-and-drop broken on touch | P1 | Interactivity | 2–3h | No |
| F6: Inline cell targets too small (<44px) | P2 | Touch affordance | 3–4h | No |
| F7: Table horizontal scroll unreadable <768px | P2 | Layout | 2–3h | No |
| F8: Timeline (Gantt) unviewable <768px | P1 | Layout | 4–5h | No |
| F9: Dashboard layout breaks <768px | P2 | Layout | 1.5–2h | No |
| F10: Form inputs lack inputmode/enterkeyhint | P3 | Accessibility | 1–2h | No |
| F11: Modal padding too aggressive on narrow | P2 | Layout | 1h | No |
| F12: Workspace switcher unreachable on mobile | P3 | Navigation | 1–2h | No (waits on F1) |
| F13: Bulk bar hard to dismiss on mobile | P3 | UX | 0.5–1h | No |

**Total P1 findings:** 3 (F1, F4, F5, F8 overlapping).  
**Total effort (P1 only):** ~22–26 hours.  
**Total effort (all findings):** ~32–39 hours.

---

## 5. Recommendations

### Wave 4 (short term)

**Ship the mobile navigation foundation (F1 + F4)** as the core "mobile-viewable" baseline:

1. **F1**: Implement a hamburger drawer/FAB pattern (4–6h). Drawer contains sidebar nav, workspace switcher, avatar menu. Triggers on <768px.
2. **F4 (partial)**: Replace `opacity-0 group-hover` on **list-view inline actions** (6–8h) with always-visible buttons or a touch-compatible menu (long-press or right-slide).

**Companion fixes (high-ROI):**
- F5 (Kanban touch): Enable Sortable touch support (2–3h).
- F11 (Modal full-screen on mobile): CSS media rule (1h).

**Wave 4 effort estimate:** ~13–18 hours. **Outcome:** Mobile users can navigate the app, interact with task state inline, and use the primary views (my-work, all-tasks, kanban) without desktop fallback.

### Wave 5 (deferred polish)

- F6 (touch target sizing): Systematic min-size pass.
- F7 (table mobile): Column hiding / card layout.
- F8 (timeline mobile): Alternative layout.
- F9 (dashboard responsive): Media queries.
- F10 (form inputmode): Audit all forms.
- F12, F13 (minor UX): Polish.

**Wave 5 effort:** ~14–21 hours.

---

## 6. Conclusion

Acta is **desktop-first by design**, and this audit reflects that. Mobile support is **not broken**—the layout is readable, and basic navigation works. But there are **critical interactivity gaps** (no touch nav, group-hover controls, drag-drop) that lock mobile users out of key workflows.

The **mobile drawer (F1) + touch action buttons (F4) + touch kanban (F5) form the minimum viable mobile experience.** Shipping these three in Wave 4 would mark Acta as "mobile-viewable and usable" rather than "mobile-readable but desktop-only."

Full mobile optimization (target sizing, gesture handling, responsive layouts for every view) is a separate multi-week project better suited to Wave 5–6 or a dedicated mobile sprint.

