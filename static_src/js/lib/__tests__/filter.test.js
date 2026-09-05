/**
 * Unit tests for the client-side filter matcher.
 *
 * Mirrors apps/web/filters.py::apply_task_filters. Every assertion
 * below is a parity check between the Python predicate and the JS
 * comparator in static_src/js/lib/filter.js.
 */
import { describe, it, expect } from "vitest";
import { readFilterState, rowMatches, activeFilterCount } from "../filter.js";

/** Build a minimal ``<tr>`` element with the data-attrs the matcher
 *  reads. Missing keys fall back to empty strings (= no filter applied
 *  for that dimension). */
function row(attrs = {}) {
  const tr = document.createElement("tr");
  tr.setAttribute("data-task-id", attrs.taskId || "1");
  const defaults = {
    status: "to-do",
    priority: "0",
    assigneeId: "",
    assigneeMe: "0",
    projectId: "1",
    workspaceId: "1",
    labelIds: "",
    archived: "0",
    searchHaystack: "",
  };
  Object.entries({ ...defaults, ...attrs }).forEach(([k, v]) => {
    if (k === "taskId") return;
    // ``data-foo-bar`` maps to ``dataset.fooBar`` — we pre-camelCase
    // the test input so the attribute lookup matches what the matcher
    // expects.
    const attr = k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
    tr.setAttribute(`data-${attr}`, v);
  });
  return tr;
}

/** Build a form-like state object directly (skipping ``readFilterState``
 *  / FormData) so we can test the matcher in isolation. */
function state(patch = {}) {
  return {
    status: new Set(),
    xstatus: new Set(),
    priority: new Set(),
    xpriority: new Set(),
    assignee: new Set(),
    xassignee: new Set(),
    project: new Set(),
    xproject: new Set(),
    workspace: new Set(),
    xworkspace: new Set(),
    label: new Set(),
    xlabel: new Set(),
    q: "",
    showArchived: false,
    ...patch,
  };
}

describe("rowMatches — archived flag", () => {
  it("hides archived rows by default", () => {
    expect(rowMatches(row({ archived: "1" }), state())).toBe(false);
  });
  it("shows archived rows when showArchived is on", () => {
    expect(rowMatches(row({ archived: "1" }), state({ showArchived: true }))).toBe(true);
  });
  it("does not hide active rows", () => {
    expect(rowMatches(row({ archived: "0" }), state())).toBe(true);
  });
});

describe("rowMatches — status include / exclude", () => {
  it("include set hides non-matching", () => {
    const s = state({ status: new Set(["done"]) });
    expect(rowMatches(row({ status: "to-do" }), s)).toBe(false);
    expect(rowMatches(row({ status: "done" }), s)).toBe(true);
  });
  it("exclude set hides matching", () => {
    const s = state({ xstatus: new Set(["done"]) });
    expect(rowMatches(row({ status: "done" }), s)).toBe(false);
    expect(rowMatches(row({ status: "to-do" }), s)).toBe(true);
  });
});

describe("rowMatches — priority include / exclude", () => {
  it("filters by priority value", () => {
    const s = state({ priority: new Set(["1"]) });
    expect(rowMatches(row({ priority: "1" }), s)).toBe(true);
    expect(rowMatches(row({ priority: "2" }), s)).toBe(false);
  });
});

describe("rowMatches — assignee tokens (me / unassigned / numeric)", () => {
  it("``me`` matches when row is assigned to current user", () => {
    const s = state({ assignee: new Set(["me"]) });
    expect(rowMatches(row({ assigneeId: "7", assigneeMe: "1" }), s)).toBe(true);
    expect(rowMatches(row({ assigneeId: "7", assigneeMe: "0" }), s)).toBe(false);
  });
  it("``unassigned`` matches rows with no assignee", () => {
    const s = state({ assignee: new Set(["unassigned"]) });
    expect(rowMatches(row({ assigneeId: "" }), s)).toBe(true);
    expect(rowMatches(row({ assigneeId: "7" }), s)).toBe(false);
  });
  it("numeric id matches that specific user", () => {
    const s = state({ assignee: new Set(["42"]) });
    expect(rowMatches(row({ assigneeId: "42" }), s)).toBe(true);
    expect(rowMatches(row({ assigneeId: "7" }), s)).toBe(false);
  });
  it("xassignee with ``me`` excludes my-assigned rows", () => {
    const s = state({ xassignee: new Set(["me"]) });
    expect(rowMatches(row({ assigneeId: "7", assigneeMe: "1" }), s)).toBe(false);
    expect(rowMatches(row({ assigneeId: "8", assigneeMe: "0" }), s)).toBe(true);
  });
});

describe("rowMatches — project / workspace", () => {
  it("project include narrows", () => {
    const s = state({ project: new Set(["3"]) });
    expect(rowMatches(row({ projectId: "3" }), s)).toBe(true);
    expect(rowMatches(row({ projectId: "5" }), s)).toBe(false);
  });
  it("workspace exclude hides", () => {
    const s = state({ xworkspace: new Set(["2"]) });
    expect(rowMatches(row({ workspaceId: "2" }), s)).toBe(false);
    expect(rowMatches(row({ workspaceId: "1" }), s)).toBe(true);
  });
});

describe("rowMatches — labels (intersection)", () => {
  it("label include keeps rows carrying any of the listed labels", () => {
    const s = state({ label: new Set(["5", "12"]) });
    expect(rowMatches(row({ labelIds: "1 5 7" }), s)).toBe(true);
    expect(rowMatches(row({ labelIds: "1 7" }), s)).toBe(false);
  });
  it("label exclude drops rows carrying ANY of the listed labels", () => {
    const s = state({ xlabel: new Set(["5"]) });
    expect(rowMatches(row({ labelIds: "1 5" }), s)).toBe(false);
    expect(rowMatches(row({ labelIds: "1 7" }), s)).toBe(true);
  });
  it("rows with no labels pass include/exclude when set is empty", () => {
    expect(rowMatches(row({ labelIds: "" }), state())).toBe(true);
  });
});

describe("rowMatches — search query", () => {
  it("substring against haystack", () => {
    const s = state({ q: "needle" });
    expect(rowMatches(row({ searchHaystack: "find the needle inside" }), s)).toBe(true);
    expect(rowMatches(row({ searchHaystack: "nothing here" }), s)).toBe(false);
  });
  it("empty query passes everything", () => {
    expect(rowMatches(row({ searchHaystack: "x" }), state())).toBe(true);
  });
});

describe("rowMatches — composition", () => {
  it("AND across dimensions: only rows passing every active filter survive", () => {
    const s = state({
      status: new Set(["to-do"]),
      assignee: new Set(["me"]),
      q: "alpha",
    });
    expect(
      rowMatches(
        row({ status: "to-do", assigneeMe: "1", assigneeId: "7", searchHaystack: "alpha beta" }),
        s,
      ),
    ).toBe(true);
    expect(
      rowMatches(
        row({ status: "to-do", assigneeMe: "0", assigneeId: "7", searchHaystack: "alpha" }),
        s,
      ),
    ).toBe(false); // not me
  });
});

describe("readFilterState", () => {
  function makeForm(values) {
    const form = document.createElement("form");
    values.forEach(([name, value]) => {
      const input = document.createElement("input");
      input.type = "text";
      input.name = name;
      input.value = value;
      form.appendChild(input);
    });
    return form;
  }
  it("collects multi-select values into Sets", () => {
    const form = makeForm([
      ["status", "to-do"],
      ["status", "in-progress"],
      ["priority", "1"],
    ]);
    const s = readFilterState(form);
    expect(s.status).toEqual(new Set(["to-do", "in-progress"]));
    expect(s.priority).toEqual(new Set(["1"]));
  });
  it("show_archived: trailing 1 wins (hidden + checkbox pair)", () => {
    const offForm = makeForm([
      ["show_archived", "0"],
    ]);
    expect(readFilterState(offForm).showArchived).toBe(false);
    const onForm = makeForm([
      ["show_archived", "0"],
      ["show_archived", "1"],
    ]);
    expect(readFilterState(onForm).showArchived).toBe(true);
  });
  it("lowercases the search query", () => {
    const form = makeForm([["q", "  NEEDLE  "]]);
    expect(readFilterState(form).q).toBe("needle");
  });
});

describe("activeFilterCount", () => {
  it("returns zero on empty state", () => {
    expect(activeFilterCount(state())).toBe(0);
  });
  it("counts each include + exclude + q + showArchived as +1", () => {
    const s = state({
      status: new Set(["to-do"]),
      xstatus: new Set(["done"]),
      label: new Set(["1", "2"]),
      q: "x",
      showArchived: true,
    });
    expect(activeFilterCount(s)).toBe(1 + 1 + 2 + 1 + 1);
  });
});
