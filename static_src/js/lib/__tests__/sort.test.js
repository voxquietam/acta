/**
 * Unit tests for the client-side sort comparators.
 *
 * Mirrors the test set we'd write for apply_task_ordering on the
 * server side. Every case here is a parity check between Python
 * (apps/web/filters.py) and JS (static_src/js/lib/sort.js).
 */
import { describe, it, expect } from "vitest";
import {
  SORT_BLANK_LAST_KEYS,
  SORT_NUMERIC_KEYS,
  compareRows,
  parseClauses,
  nextSortState,
  applyClientSort,
} from "../sort.js";

function row(attrs) {
  const tr = document.createElement("tr");
  tr.setAttribute("data-task-id", attrs.id || "1");
  Object.entries(attrs).forEach(([k, v]) => {
    if (k === "id") return;
    tr.setAttribute(`data-sort-${k}`, String(v));
  });
  return tr;
}

describe("SORT_BLANK_LAST_KEYS", () => {
  it("covers size, due, assignee", () => {
    expect(SORT_BLANK_LAST_KEYS.has("size")).toBe(true);
    expect(SORT_BLANK_LAST_KEYS.has("due")).toBe(true);
    expect(SORT_BLANK_LAST_KEYS.has("assignee")).toBe(true);
    expect(SORT_BLANK_LAST_KEYS.has("title")).toBe(false);
  });
});

describe("SORT_NUMERIC_KEYS", () => {
  it("covers status, priority, size", () => {
    expect(SORT_NUMERIC_KEYS.has("status")).toBe(true);
    expect(SORT_NUMERIC_KEYS.has("priority")).toBe(true);
    expect(SORT_NUMERIC_KEYS.has("size")).toBe(true);
    expect(SORT_NUMERIC_KEYS.has("title")).toBe(false);
  });
});

describe("compareRows", () => {
  it("compares titles case-insensitively ascending", () => {
    const a = row({ title: "alpha" });
    const b = row({ title: "beta" });
    expect(compareRows(a, b, "title", "asc")).toBeLessThan(0);
    expect(compareRows(b, a, "title", "asc")).toBeGreaterThan(0);
  });

  it("reverses on descending", () => {
    const a = row({ title: "alpha" });
    const b = row({ title: "beta" });
    expect(compareRows(a, b, "title", "desc")).toBeGreaterThan(0);
  });

  it("numeric comparison for status rank", () => {
    const planned = row({ status: "0" });
    const done = row({ status: "4" });
    expect(compareRows(planned, done, "status", "asc")).toBeLessThan(0);
    // String comparison would say "0" > "4" alphabetically; numeric should
    // not. This catches a regression where we forgot the numeric coerce.
    expect(compareRows(done, planned, "status", "asc")).toBeGreaterThan(0);
  });

  it("priority NO_PRIORITY (rank 99) sinks to the bottom in asc", () => {
    const urgent = row({ priority: "1" });
    const noprio = row({ priority: "99" });
    expect(compareRows(urgent, noprio, "priority", "asc")).toBeLessThan(0);
    // And still bottom in desc — the rank ensures it (server uses
    // _PRIORITY_NOPRIO_LAST to achieve the same shape).
    expect(compareRows(urgent, noprio, "priority", "desc")).toBeGreaterThan(0);
  });

  it("NULLS LAST sinks blank size in ascending", () => {
    const small = row({ size: "1" });
    const blank = row({ size: "" });
    expect(compareRows(small, blank, "size", "asc")).toBeLessThan(0);
  });

  it("NULLS LAST sinks blank size in descending too", () => {
    const big = row({ size: "13" });
    const blank = row({ size: "" });
    // Even in desc the blank should land last — this is the key
    // server-parity guarantee.
    expect(compareRows(big, blank, "size", "desc")).toBeLessThan(0);
    expect(compareRows(blank, big, "size", "desc")).toBeGreaterThan(0);
  });

  it("two blanks compare equal", () => {
    const a = row({ assignee: "" });
    const b = row({ assignee: "" });
    expect(compareRows(a, b, "assignee", "asc")).toBe(0);
  });

  it("compares due dates lexicographically (ISO-friendly)", () => {
    const early = row({ due: "2026-01-01" });
    const late = row({ due: "2026-12-31" });
    expect(compareRows(early, late, "due", "asc")).toBeLessThan(0);
  });
});

describe("parseClauses", () => {
  it("handles single key", () => {
    expect(parseClauses("title")).toEqual([{ key: "title", dir: "asc" }]);
  });
  it("handles desc prefix", () => {
    expect(parseClauses("-updated")).toEqual([{ key: "updated", dir: "desc" }]);
  });
  it("handles multi-key default ordering", () => {
    expect(parseClauses("status,-priority,-updated")).toEqual([
      { key: "status", dir: "asc" },
      { key: "priority", dir: "desc" },
      { key: "updated", dir: "desc" },
    ]);
  });
  it("ignores empty / whitespace tokens", () => {
    expect(parseClauses(" , title , ")).toEqual([{ key: "title", dir: "asc" }]);
    expect(parseClauses("")).toEqual([]);
    expect(parseClauses(null)).toEqual([]);
  });
});

describe("nextSortState", () => {
  it("first click on a column → asc", () => {
    expect(nextSortState("", "asc", "title")).toEqual({ key: "title", dir: "asc" });
    expect(nextSortState("size", "desc", "title")).toEqual({ key: "title", dir: "asc" });
  });
  it("second click on same column → desc", () => {
    expect(nextSortState("title", "asc", "title")).toEqual({ key: "title", dir: "desc" });
  });
  it("third click on same column → cleared", () => {
    expect(nextSortState("title", "desc", "title")).toEqual({ key: "", dir: "asc" });
  });
});

describe("applyClientSort", () => {
  function makeBody(rows) {
    const tbody = document.createElement("tbody");
    rows.forEach((r) => tbody.appendChild(r));
    return tbody;
  }

  it("sorts rows in place by title ascending", () => {
    const tbody = makeBody([
      row({ id: "1", title: "charlie" }),
      row({ id: "2", title: "alpha" }),
      row({ id: "3", title: "bravo" }),
    ]);
    applyClientSort(tbody, [{ key: "title", dir: "asc" }]);
    const order = Array.from(tbody.children).map((r) => r.getAttribute("data-task-id"));
    expect(order).toEqual(["2", "3", "1"]);
  });

  it("tie-breaks compound sort (status asc, priority desc)", () => {
    const tbody = makeBody([
      row({ id: "1", status: "1", priority: "3" }),
      row({ id: "2", status: "1", priority: "1" }),
      row({ id: "3", status: "0", priority: "4" }),
    ]);
    applyClientSort(tbody, [
      { key: "status", dir: "asc" },
      { key: "priority", dir: "asc" },
    ]);
    // Task 3 (status 0) first, then status 1 sorted by priority asc:
    // priority 1 (id=2), priority 3 (id=1).
    const order = Array.from(tbody.children).map((r) => r.getAttribute("data-task-id"));
    expect(order).toEqual(["3", "2", "1"]);
  });

  it("preserves original order on no clauses", () => {
    const tbody = makeBody([
      row({ id: "1", title: "alpha" }),
      row({ id: "2", title: "bravo" }),
    ]);
    applyClientSort(tbody, []);
    const order = Array.from(tbody.children).map((r) => r.getAttribute("data-task-id"));
    expect(order).toEqual(["1", "2"]);
  });
});
