/**
 * Unit tests for the task-table virtualisation window maths.
 *
 * The DOM side (spacer rows, ``data-vrow-hidden``, the scroll /
 * ResizeObserver wiring) lives in static/js/acta.js; everything that
 * decides *which* rows survive is here so it can be pinned down
 * without a browser.
 */
import { describe, it, expect } from "vitest";
import { VIRTUAL_MIN_ROWS, VIRTUAL_OVERSCAN, computeWindow, sameWindow } from "../virtual.js";

const BASE = {
  total: 400,
  rowHeight: 40,
  scrollTop: 0,
  viewportHeight: 800,
};

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
  it("anchors at the top with overscan clamped to zero", () => {
    const w = computeWindow({ ...BASE, overscan: 12 });
    expect(w.start).toBe(0);
    // 800 / 40 = 20 visible + 1 partial + 12 overscan.
    expect(w.end).toBe(33);
    expect(w.padTop).toBe(0);
    expect(w.padBottom).toBe((400 - 33) * 40);
  });

  it("follows the scroll offset", () => {
    const w = computeWindow({ ...BASE, scrollTop: 4000, overscan: 12 });
    // first = 4000 / 40 = 100.
    expect(w.start).toBe(88);
    expect(w.end).toBe(133);
    expect(w.padTop).toBe(88 * 40);
    expect(w.padBottom).toBe((400 - 133) * 40);
  });

  it("clamps the end at the last row", () => {
    const w = computeWindow({ ...BASE, scrollTop: 99999, overscan: 12 });
    expect(w.end).toBe(400);
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
    for (const scrollTop of [0, 500, 4000, 12000, 15960]) {
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

  it("renders everything when the viewport is taller than the list", () => {
    const w = computeWindow({ ...BASE, total: 80, viewportHeight: 6000 });
    expect(w.start).toBe(0);
    expect(w.end).toBe(80);
    expect(w.padTop).toBe(0);
    expect(w.padBottom).toBe(0);
  });

  it("defaults the overscan to VIRTUAL_OVERSCAN", () => {
    const withDefault = computeWindow({ ...BASE, scrollTop: 4000 });
    const explicit = computeWindow({ ...BASE, scrollTop: 4000, overscan: VIRTUAL_OVERSCAN });
    expect(withDefault).toEqual(explicit);
  });
});

describe("sameWindow", () => {
  it("is false against a missing previous window", () => {
    expect(sameWindow(null, computeWindow(BASE))).toBe(false);
  });

  it("is true for two identical windows", () => {
    expect(sameWindow(computeWindow(BASE), computeWindow(BASE))).toBe(true);
  });

  it("absorbs sub-row scrolling", () => {
    // A 10px scroll inside the same row must not trigger a DOM pass.
    const a = computeWindow({ ...BASE, scrollTop: 4000 });
    const b = computeWindow({ ...BASE, scrollTop: 4010 });
    expect(sameWindow(a, b)).toBe(true);
  });

  it("is false once the window moves by a row", () => {
    const a = computeWindow({ ...BASE, scrollTop: 4000 });
    const b = computeWindow({ ...BASE, scrollTop: 4040 });
    expect(sameWindow(a, b)).toBe(false);
  });

  it("is false when virtualisation flips off", () => {
    const on = computeWindow(BASE);
    const off = computeWindow({ ...BASE, viewportHeight: 0 });
    expect(sameWindow(on, off)).toBe(false);
  });
});
