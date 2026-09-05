# ADR 0032: Task-table row virtualisation

**Status:** accepted
**Date:** 2026-09-05

## Context

The task table lags on hover once a workspace grows past a few hundred
tasks. Measured against real mouse events on All Tasks: 381 rows produced
15 long tasks of 50–150 ms, 30 rows produced none. Disabling the CSS
transitions and then Alpine changed nothing — the cost is the size of the
render tree that a hover invalidation walks, and it scales with the number
of `<tr>` elements, not with what the rows contain.

Two constraints rule out the obvious fixes:

- **We cannot detach the rows.** Client-side sort (ADR 0019), the client
  filter pass and the selection store all read the *full* row set — that's
  what makes sort and chip toggles instant with no round-trip. Paginating
  or rendering only a slice server-side would trade a hover stutter for a
  network round-trip on every interaction.
- **`content-visibility: auto` does not work here.** CSS containment does
  not apply to `<tr>`, so browsers ignore the property on table rows. It
  was tried, appeared to help on a warm page, and was reverted.

## Decision

**Windowed virtualisation that hides rows instead of removing them.**
Every row stays in the DOM; rows outside the scrolled window get
`data-vrow-hidden`, whose only rule is `display: none !important` — they
leave layout and paint but remain queryable, so sort, filters, search and
"select all" keep seeing all of them.

Two spacer rows (`tr[data-vspacer="top"|"bottom"]`) stand in for the hidden
rows' height, so `scrollHeight` and `scrollTop` mean exactly what they
meant before. On a 381-row All Tasks the virtualised `scrollHeight` matches
the unvirtualised one to the pixel.

The pieces:

- **`static_src/js/lib/virtual.js`** — `computeWindow()` (which slice
  survives, and the two spacer heights) and `sameWindow()` (skip the DOM
  pass when a sub-row scroll didn't move the window). Pure, unit-tested;
  hand-mirrored into `static/js/acta.js` like `filter.js` and `sort.js`.
- **`virtualiseTable()` in `acta.js`** — the DOM side: marker attributes,
  spacer placement, and the row-height measurement.
- **Off below 60 rows**, and off whenever the panel is hidden (an inactive
  view tab collapses the scroll container to zero height, so every row
  stays rendered and the tab is complete the moment `x-show` reveals it).
  Overscan is 12 rows each way.

**Row height is measured as a top-to-top delta over a run of rendered
rows**, not from one row's `offsetHeight`. Rows are 38.5 px at dpr 2 and
`offsetHeight` rounds to whole pixels; the 0.5 px error compounds into a
~120 px scrollbar drift over 270 rows. The table's first row is dropped
from the sample — `divide-y` gives it no border, so it is a pixel shorter
than every other row.

**Recompute triggers.** Scroll (rAF-throttled, and skipped entirely when
the window didn't move); a `ResizeObserver` on the scroll container, which
is what catches a view-tab switch — `x-show` fires no event but does take
the container from 0 to full height — as well as window resize and the
sidebar collapse; and an explicit structural pass at the end of
`applyClientFilters` and after a client-side sort, because both change
which rows the window is indexing.

**Scroll anchoring is disabled** (`overflow-anchor: none`) on a virtualised
container: the browser anchors to an element near the top edge, and ours
leaves layout on every window step. Total height is spacer-preserved, so
the anchoring is not needed.

## Consequences

- Hovering a 381-row table: a full-table style invalidation went from
  112 ms median / 149 ms max to 14.7 ms / 24.2 ms, with 45 rows in the
  render tree instead of 381.
- Sort, chip filters, the search haystack and "select all" are unchanged —
  they walk `tr[data-task-id]` and the hidden rows are still there.
- The browser's own Ctrl/Cmd+F does not find text in a scrolled-past row.
  Client-side filters already had this property; Cmd+K search is
  server-side and unaffected.
- A filter-hidden row never carries `data-vrow-hidden`: the filter pass
  owns the inline `display` on the same element, and a stale `!important`
  marker would outrank it. `virtualiseTable()` clears the marker off
  filter-hidden rows on every pass.
- Only the table is virtualised. Kanban, list and timeline keep their own
  shapes; if one of them starts lagging, the same window maths applies but
  the DOM side has to be written for that surface.

## Alternatives rejected

- **`content-visibility: auto` on rows** — ignored by browsers on `<tr>`;
  containment does not apply to table rows.
- **Server-side pagination** — kills instant client sort and filtering
  (ADR 0019), which is the table's main advantage over Kaneo.
- **Removing rows from the DOM** — the honest virtualisation, but it means
  reimplementing sort, filter, search and selection against a JS model
  instead of the DOM. Much larger change for the same frame budget.
- **`display: table-row` on a `<div>` grid instead of a real table** —
  would let `content-visibility` work, but rewrites every cell partial and
  the sticky header for a problem the marker attribute already solves.
