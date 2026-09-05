/**
 * Unit tests for the task-table virtualisation window maths.
 *
 * The DOM side (spacer rows, ``data-vrow-hidden``, the scroll /
 * ResizeObserver wiring) lives in static/js/acta.js; everything that
 * decides *which* rows survive is here so it can be pinned down
 * without a browser.
 */
import { describe, it, expect } from "vitest";
import { VIRTUAL_MIN_ROWS, VIRTUAL_OVERSCAN, VIRTUAL_BLOCK, computeWindow, overscanFor, sameWindow } from "../virtual.js";

const BASE = {
  total: 400,
  rowHeight: 40,
  scrollTop: 0,
  viewportHeight: 800,
};

describe("overscanFor", () => {
  it("keeps a full viewport of rows on each side", () => {
    expect(overscanFor(800, 40)).toBe(20);
    expect(overscanFor(761, 38.5)).toBe(20);
  });

  it("never drops below the floor", () => {
    expect(overscanFor(200, 40)).toBe(VIRTUAL_OVERSCAN);
  });

  it("falls back to the floor on unmeasurable input", () => {
    expect(overscanFor(800, 0)).toBe(VIRTUAL_OVERSCAN);
    expect(overscanFor(NaN, 40)).toBe(VIRTUAL_OVERSCAN);
  });
});

describe("computeWindow — off switches", () => {
  it("stays off below the row threshold", () => {
    const w = computeWindow({ ...BASE, total: VIRTUAL_MIN_ROWS - 1 });
    expect(w.active).toBe(false);
    expect(w.start).toBe(0);
    expect(w.end).toBe(VIRTUAL_MIN_ROWS - 1);
    expect(w.padTop).toBe(0);
    expect(w.padBottom).toBe(0);
  });

  it("turns on exactly at the threshold", () => {
    expect(computeWindow({ ...BASE, total: VIRTUAL_MIN_ROWS }).active).toBe(true);
  });

  it("stays off when the row height is unmeasured", () => {
    expect(computeWindow({ ...BASE, rowHeight: 0 }).active).toBe(false);
  });

  it("stays off when the panel is hidden (zero viewport)", () => {
    // ``x-show`` on an inactive view tab collapses the scroll container
    // to 0px — every row must stay visible so the tab renders complete
    // the moment it is shown.
    const w = computeWindow({ ...BASE, viewportHeight: 0 });
    expect(w.active).toBe(false);
    expect(w.end).toBe(400);
  });

  it("stays off on non-finite input", () => {
    expect(computeWindow({ ...BASE, total: NaN }).active).toBe(false);
    expect(computeWindow({ ...BASE, rowHeight: NaN }).active).toBe(false);
    expect(computeWindow({ ...BASE, viewportHeight: Infinity }).active).toBe(false);
  });
});

describe("computeWindow — window placement", () => {
  it("anchors at the top with both edges clamped into the list", () => {
    const w = computeWindow({ ...BASE, overscan: 12, block: 8 });
    expect(w.start).toBe(0);
    // anchor 0 + block 8 + span 21 + overscan 12.
    expect(w.end).toBe(41);
    expect(w.padTop).toBe(0);
    expect(w.padBottom).toBe((400 - 41) * 40);
  });

  it("follows the scroll offset", () => {
    const w = computeWindow({ ...BASE, scrollTop: 4000, overscan: 12, block: 8 });
    // first = 4000 / 40 = 100, anchor 96; 96 - 12 and 96 + 8 + 21 + 12.
    expect(w.start).toBe(84);
    expect(w.end).toBe(137);
    expect(w.padTop).toBe(84 * 40);
    expect(w.padBottom).toBe((400 - 137) * 40);
  });

  it("clamps both edges when the scroll offset is past the content", () => {
    const w = computeWindow({ ...BASE, scrollTop: 99999 });
    expect(w.start).toBeLessThanOrEqual(400);
    expect(w.end).toBe(400);
    expect(w.end).toBeGreaterThanOrEqual(w.start);
    expect(w.padBottom).toBe(0);
  });

  it("treats a negative scrollTop (rubber-band) as the top", () => {
    const w = computeWindow({ ...BASE, scrollTop: -120 });
    expect(w.start).toBe(0);
    expect(w.padTop).toBe(0);
  });

  it("always keeps total height intact", () => {
    // padTop + rendered rows + padBottom must equal the unvirtualised
    // height, or the scrollbar jumps mid-scroll.
    for (const scrollTop of [0, 500, 4000, 12000, 15960, 99999]) {
      const w = computeWindow({ ...BASE, scrollTop });
      const rendered = (w.end - w.start) * BASE.rowHeight;
      expect(w.padTop + rendered + w.padBottom).toBe(BASE.total * BASE.rowHeight);
    }
  });

  it("keeps the viewport inside the rendered slice", () => {
    for (const scrollTop of [0, 37, 500, 4001, 12000, 15960]) {
      const w = computeWindow({ ...BASE, scrollTop });
      const top = Math.max(0, scrollTop);
      expect(w.start * BASE.rowHeight).toBeLessThanOrEqual(top);
      expect(w.end * BASE.rowHeight).toBeGreaterThanOrEqual(
        Math.min(top + BASE.viewportHeight, BASE.total * BASE.rowHeight),
      );
    }
  });

  it("keeps a full viewport of runway past each edge by default", () => {
    // The Safari fix: momentum scrolling outruns the ``scroll`` event, so
    // the buffer has to survive a screen's worth of travel between passes.
    const w = computeWindow({ ...BASE, scrollTop: 8000 });
    const top = 8000;
    expect(top - w.start * BASE.rowHeight).toBeGreaterThanOrEqual(BASE.viewportHeight);
    expect(w.end * BASE.rowHeight - (top + BASE.viewportHeight)).toBeGreaterThanOrEqual(BASE.viewportHeight);
  });

  it("moves the whole window exactly once per block", () => {
    // Both edges hang off one anchor, so scrolling the full list costs
    // total/block DOM passes — not one per row, and not two per block
    // from edges snapping out of phase.
    let previous = null;
    let moves = 0;
    for (let row = 0; row < BASE.total; row += 1) {
      const w = computeWindow({ ...BASE, scrollTop: row * BASE.rowHeight });
      if (!sameWindow(previous, w)) moves += 1;
      previous = w;
    }
    expect(moves).toBe(Math.ceil(BASE.total / VIRTUAL_BLOCK));
  });

  it("renders everything when the viewport is taller than the list", () => {
    const w = computeWindow({ ...BASE, total: 80, viewportHeight: 6000 });
    expect(w.start).toBe(0);
    expect(w.end).toBe(80);
    expect(w.padTop).toBe(0);
    expect(w.padBottom).toBe(0);
  });
});

describe("sameWindow", () => {
  it("is false against a missing previous window", () => {
    expect(sameWindow(null, computeWindow(BASE))).toBe(false);
  });

  it("is true for two identical windows", () => {
    expect(sameWindow(computeWindow(BASE), computeWindow(BASE))).toBe(true);
  });

  it("absorbs scrolling within a block", () => {
    // Every tick that doesn't cross a block boundary must cost zero DOM
    // work — that's what stops the table relayouting on every frame.
    // Rows 96-103 share an anchor: scrollTop 3840 through 4159.
    const a = computeWindow({ ...BASE, scrollTop: 3840 });
    for (const scrollTop of [3850, 3880, 4000, 4120, 4159]) {
      expect(sameWindow(a, computeWindow({ ...BASE, scrollTop }))).toBe(true);
    }
  });

  it("is false once the window crosses a block boundary", () => {
    const a = computeWindow({ ...BASE, scrollTop: 4000 });
    const b = computeWindow({ ...BASE, scrollTop: 4000 + VIRTUAL_BLOCK * BASE.rowHeight });
    expect(sameWindow(a, b)).toBe(false);
  });

  it("is false when virtualisation flips off", () => {
    const on = computeWindow(BASE);
    const off = computeWindow({ ...BASE, viewportHeight: 0 });
    expect(sameWindow(on, off)).toBe(false);
  });
});
