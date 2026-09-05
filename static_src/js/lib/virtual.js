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
 * Rows rendered above and below the viewport. Covers a fast scroll
 * flick between two rAF ticks without exposing blank space.
 */
export const VIRTUAL_OVERSCAN = 12;

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
 * @param {number} [opts.overscan] Rows to keep past each edge.
 * @param {number} [opts.minRows] Threshold below which we stay off.
 * @returns {{active: boolean, start: number, end: number, padTop: number, padBottom: number}}
 */
export function computeWindow({
  total,
  rowHeight,
  scrollTop,
  viewportHeight,
  overscan = VIRTUAL_OVERSCAN,
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

  const top = Number.isFinite(scrollTop) && scrollTop > 0 ? scrollTop : 0;
  const first = Math.floor(top / rowHeight);
  // ``+ 1`` covers the partially-scrolled row at the bottom edge.
  const span = Math.ceil(viewportHeight / rowHeight) + 1;
  const start = Math.max(0, first - overscan);
  const end = Math.min(total, first + span + overscan);
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
