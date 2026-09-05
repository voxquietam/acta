/**
 * Pure helpers for windowed virtualisation of the task table.
 *
 * The table keeps every row in the DOM — client-side sort, the filter
 * pass and the selection store all walk the full row set — so we can't
 * detach rows. What costs us is the render tree: hovering a 380-row
 * table produced 15 long tasks of 50-150 ms, a 30-row one produced
 * none. ``display: none`` on out-of-viewport rows takes them out of
 * layout/paint while leaving them queryable, and two spacer rows keep
 * the scrollbar honest.
 *
 * ``content-visibility: auto`` is NOT an option here: containment does
 * not apply to ``<tr>``, so the browser ignores it on table rows.
 *
 * Same sync caveat as static_src/js/lib/filter.js and sort.js — the
 * mirrored copy lives in static/js/acta.js and must be edited by hand
 * until acta becomes a proper bundle.
 *
 * Test coverage: static_src/js/lib/__tests__/virtual.test.js.
 */

/**
 * Row count below which virtualisation stays off. Under ~60 rows the
 * hover cost is unmeasurable and the spacer bookkeeping is pure
 * overhead.
 */
export const VIRTUAL_MIN_ROWS = 60;

/**
 * Floor for the overscan. The real value is a full viewport of rows
 * (see ``overscanFor``) — Safari runs momentum scrolling on the
 * compositor and delivers ``scroll`` to the main thread well behind the
 * pixels, so a fixed 12-row buffer emptied mid-flick and rows visibly
 * popped in. Firefox never showed it.
 */
export const VIRTUAL_OVERSCAN = 12;

/**
 * Window edges snap to multiples of this many rows.
 *
 * Without it the window moves every single row, and each move relayouts
 * the whole table (the spacer rows change height). Snapping means the
 * DOM pass runs once per block instead of once per row, and every
 * scroll tick in between is a ``sameWindow`` early return that touches
 * nothing. Must stay well under the overscan, or the window snaps to a
 * position whose buffer is already consumed.
 */
export const VIRTUAL_BLOCK = 8;

/**
 * Rows to keep rendered past each viewport edge: one full screen, never
 * fewer than ``VIRTUAL_OVERSCAN``.
 *
 * @param {number} viewportHeight Client height of the scroll container.
 * @param {number} rowHeight Measured height of one row, in px.
 * @returns {number} Overscan in rows.
 */
export function overscanFor(viewportHeight, rowHeight) {
  if (!Number.isFinite(viewportHeight) || !Number.isFinite(rowHeight) || rowHeight <= 0) {
    return VIRTUAL_OVERSCAN;
  }
  return Math.max(VIRTUAL_OVERSCAN, Math.ceil(viewportHeight / rowHeight));
}

/**
 * Compute which slice of the row list should stay in the render tree.
 *
 * ``end`` is exclusive. When the result is inactive the caller must
 * show every row and drop both spacers — that's the state for a short
 * table, a table whose panel is hidden (zero viewport), or one whose
 * row height could not be measured yet.
 *
 * @param {object} opts
 * @param {number} opts.total Number of filter-visible rows.
 * @param {number} opts.rowHeight Measured height of one row, in px.
 * @param {number} opts.scrollTop Scroll offset of the scroll container.
 * @param {number} opts.viewportHeight Client height of the scroll container.
 * @param {number} [opts.overscan] Rows to keep past each edge; defaults
 *   to ``overscanFor(viewportHeight, rowHeight)``.
 * @param {number} [opts.block] Row granularity the edges snap to.
 * @param {number} [opts.minRows] Threshold below which we stay off.
 * @returns {{active: boolean, start: number, end: number, padTop: number, padBottom: number}}
 */
export function computeWindow({
  total,
  rowHeight,
  scrollTop,
  viewportHeight,
  overscan,
  block = VIRTUAL_BLOCK,
  minRows = VIRTUAL_MIN_ROWS,
}) {
  const off = {
    active: false,
    start: 0,
    end: total > 0 ? total : 0,
    padTop: 0,
    padBottom: 0,
  };
  if (!Number.isFinite(total) || total < minRows) return off;
  if (!Number.isFinite(rowHeight) || rowHeight <= 0) return off;
  if (!Number.isFinite(viewportHeight) || viewportHeight <= 0) return off;

  const pad = Number.isFinite(overscan) ? overscan : overscanFor(viewportHeight, rowHeight);
  const step = Number.isFinite(block) && block >= 1 ? Math.floor(block) : 1;
  const top = Number.isFinite(scrollTop) && scrollTop > 0 ? scrollTop : 0;
  const first = Math.floor(top / rowHeight);
  // ``+ 1`` covers the partially-scrolled row at the bottom edge.
  const span = Math.ceil(viewportHeight / rowHeight) + 1;
  // Both edges hang off ONE block-aligned anchor, so the whole window
  // moves exactly once per block. Snapping the two edges independently
  // would put their boundaries out of phase and cost two DOM passes per
  // block instead of one.
  //
  // ``anchor <= first`` and ``anchor + step > first``, so widening by
  // ``step`` at the bottom keeps the viewport enclosed however the
  // anchor rounded. Both edges are clamped into the list: a scrollTop
  // past the content (rubber-band, or a stale offset after a filter
  // shrank the list) must not produce a window off the end.
  const anchor = Math.floor(first / step) * step;
  const start = Math.min(total, Math.max(0, anchor - pad));
  const end = Math.min(total, Math.max(start, anchor + step + span + pad));
  return {
    active: true,
    start,
    end,
    padTop: start * rowHeight,
    padBottom: (total - end) * rowHeight,
  };
}

/**
 * Cheap equality between two ``computeWindow`` results.
 *
 * Scroll fires far more often than the window actually moves; skipping
 * the DOM pass on an unchanged window is what keeps scrolling cheap.
 *
 * @param {object|null} a Previous window, or null on the first pass.
 * @param {object} b Freshly computed window.
 * @returns {boolean} True when the DOM already reflects ``b``.
 */
export function sameWindow(a, b) {
  if (!a || !b) return false;
  return (
    a.active === b.active && a.start === b.start && a.end === b.end && a.padTop === b.padTop && a.padBottom === b.padBottom
  );
}
