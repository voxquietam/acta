/**
 * Pure helpers for client-side task table sorting.
 *
 * Mirrors ``apply_task_ordering`` in apps/web/filters.py. The whole
 * point of keeping these as ES modules separate from the in-browser
 * IIFE in static/js/acta.js is that they're now testable — see
 * static_src/js/lib/__tests__/sort.test.js.
 *
 * When you change a comparator here, mirror the change in acta.js
 * (search for ``SORT_BLANK_LAST_KEYS`` / ``compareRows``). The two
 * copies are kept in sync by hand until acta.js itself becomes an
 * ESM bundle.
 */

/** Columns whose empty / null values sink to the bottom in BOTH
 *  directions (NULLS LAST). Matches the ``directed_nulls_last``
 *  branch in the server-side ordering. */
export const SORT_BLANK_LAST_KEYS = new Set(["size", "due", "assignee"]);

/** Columns whose ``data-sort-<key>`` attribute is a numeric string —
 *  parsed via ``parseFloat`` before comparison. */
export const SORT_NUMERIC_KEYS = new Set(["status", "priority", "size"]);

/**
 * Compare two row elements on a single sort key.
 *
 * @param {Element} a Row A.
 * @param {Element} b Row B.
 * @param {string} key Column key (id / title / status / priority / size /
 *   assignee / project / due / updated).
 * @param {"asc"|"desc"} dir Direction.
 * @returns {number} Standard ``Array.prototype.sort`` comparator value.
 */
export function compareRows(a, b, key, dir) {
  const prop = "sort" + key.charAt(0).toUpperCase() + key.slice(1);
  const av = a.dataset[prop] || "";
  const bv = b.dataset[prop] || "";
  if (SORT_BLANK_LAST_KEYS.has(key)) {
    if (av === "" && bv === "") return 0;
    if (av === "") return 1;
    if (bv === "") return -1;
  }
  let cmp;
  if (SORT_NUMERIC_KEYS.has(key)) {
    cmp = parseFloat(av) - parseFloat(bv);
  } else {
    cmp = av < bv ? -1 : av > bv ? 1 : 0;
  }
  return dir === "desc" ? -cmp : cmp;
}

/**
 * Sort all task rows inside ``tbody`` in-place by an ordered list of
 * clauses. Multi-key sort — ties on the first clause break by the
 * next, etc. Matches Django ``order_by`` semantics.
 *
 * @param {Element} tbody The ``<tbody>`` whose rows to reshuffle.
 * @param {Array<{key:string, dir:string}>} clauses Sort clauses.
 */
export function applyClientSort(tbody, clauses) {
  const rows = Array.from(tbody.querySelectorAll("tr[data-task-id]"));
  rows.sort((a, b) => {
    for (const { key, dir } of clauses) {
      const c = compareRows(a, b, key, dir);
      if (c !== 0) return c;
    }
    return 0;
  });
  const frag = document.createDocumentFragment();
  rows.forEach((r) => frag.appendChild(r));
  tbody.appendChild(frag);
}

/**
 * Parse a comma-separated sort string (the ``?order=...`` shape) into
 * a list of ``{key, dir}`` clauses. ``"-foo"`` means descending.
 *
 * @param {string} str e.g. ``"status,-priority,-updated"``.
 */
export function parseClauses(str) {
  return (str || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((token) => {
      const dir = token.startsWith("-") ? "desc" : "asc";
      return { key: token.replace(/^-/, ""), dir };
    });
}

/**
 * Three-state click cycle on a column header.
 * - clicking a different column resets to ``asc``;
 * - clicking the current ``asc`` column flips to ``desc``;
 * - clicking the current ``desc`` column clears (``key === ""``).
 *
 * Empty ``key`` represents "no explicit sort" — the page falls back
 * to its server-default order.
 */
export function nextSortState(currentKey, currentDir, clickedKey) {
  if (clickedKey !== currentKey) return { key: clickedKey, dir: "asc" };
  if (currentDir === "asc") return { key: clickedKey, dir: "desc" };
  return { key: "", dir: "asc" };
}
