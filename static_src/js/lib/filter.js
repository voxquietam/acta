/**
 * Pure helpers for client-side task filtering.
 *
 * Mirrors ``apply_task_filters`` in apps/web/filters.py. Same sync
 * caveat as static_src/js/lib/sort.js — change one, change the
 * mirrored copy in static/js/acta.js by hand until acta becomes a
 * proper bundle.
 *
 * Test coverage: static_src/js/lib/__tests__/filter.test.js.
 */

/**
 * Parse a filter form into a normalised state object used by
 * ``rowMatches``. Pulls multi-select values, the search query, and
 * the ``show_archived`` toggle (which gets the "trailing 1 wins"
 * treatment matching ``resolve_show_archived`` on the server).
 */
export function readFilterState(form) {
  if (!form) return null;
  const fd = new FormData(form);
  const multi = (name) => fd.getAll(name).map((v) => String(v));
  const archivedRaw = fd.getAll("show_archived");
  return {
    status: new Set(multi("status")),
    xstatus: new Set(multi("xstatus")),
    priority: new Set(multi("priority")),
    xpriority: new Set(multi("xpriority")),
    assignee: new Set(multi("assignee")),
    xassignee: new Set(multi("xassignee")),
    project: new Set(multi("project")),
    xproject: new Set(multi("xproject")),
    workspace: new Set(multi("workspace")),
    xworkspace: new Set(multi("xworkspace")),
    label: new Set(multi("label")),
    xlabel: new Set(multi("xlabel")),
    q: (fd.get("q") || "").toString().trim().toLowerCase(),
    showArchived: archivedRaw.includes("1"),
  };
}

/**
 * Decide whether a single task ``row`` element (with the data-attrs
 * emitted by ``task_filter_attrs``) satisfies the filter ``state``.
 * Mirror image of the server-side ``apply_task_filters`` predicates.
 */
export function rowMatches(row, state) {
  if (!state.showArchived && row.dataset.archived === "1") return false;
  const s = row.dataset.status || "";
  if (state.status.size && !state.status.has(s)) return false;
  if (state.xstatus.size && state.xstatus.has(s)) return false;
  const p = row.dataset.priority || "0";
  if (state.priority.size && !state.priority.has(p)) return false;
  if (state.xpriority.size && state.xpriority.has(p)) return false;
  const aid = row.dataset.assigneeId || "";
  const isMe = row.dataset.assigneeMe === "1";
  const aTokens = new Set();
  if (aid) {
    aTokens.add(aid);
    if (isMe) aTokens.add("me");
  } else {
    aTokens.add("unassigned");
  }
  if (state.assignee.size) {
    let ok = false;
    for (const t of state.assignee) {
      if (aTokens.has(t)) { ok = true; break; }
    }
    if (!ok) return false;
  }
  if (state.xassignee.size) {
    for (const t of state.xassignee) {
      if (aTokens.has(t)) return false;
    }
  }
  const proj = row.dataset.projectId || "";
  if (state.project.size && !state.project.has(proj)) return false;
  if (state.xproject.size && state.xproject.has(proj)) return false;
  const ws = row.dataset.workspaceId || "";
  if (state.workspace.size && !state.workspace.has(ws)) return false;
  if (state.xworkspace.size && state.xworkspace.has(ws)) return false;
  if (state.label.size || state.xlabel.size) {
    const rowLabels = new Set((row.dataset.labelIds || "").split(/\s+/).filter(Boolean));
    if (state.label.size) {
      let any = false;
      for (const id of state.label) {
        if (rowLabels.has(id)) { any = true; break; }
      }
      if (!any) return false;
    }
    if (state.xlabel.size) {
      for (const id of state.xlabel) {
        if (rowLabels.has(id)) return false;
      }
    }
  }
  if (state.q) {
    const hay = row.dataset.searchHaystack || "";
    if (!hay.includes(state.q)) return false;
  }
  return true;
}

/** Compute the count of *active* filter dimensions for the sidebar
 *  badge. ``q`` and ``show_archived`` each count as one. */
export function activeFilterCount(state) {
  return (
    state.status.size +
    state.xstatus.size +
    state.priority.size +
    state.xpriority.size +
    state.assignee.size +
    state.xassignee.size +
    state.project.size +
    state.xproject.size +
    state.workspace.size +
    state.xworkspace.size +
    state.label.size +
    state.xlabel.size +
    (state.q ? 1 : 0) +
    (state.showArchived ? 1 : 0)
  );
}
