// Cross-cutting helpers for the Acta frontend.
// See docs/decisions/0014-frontend-architecture.md.

(function () {
  // CSRF token retrieval for fetch() calls outside HTMX.
  function getCookie(name) {
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(";").shift();
    return null;
  }

  window.acta = {
    csrfToken: () => getCookie("csrftoken"),
    updateStickyStack: null, // assigned below once defined

    // Untoggle one filter value (e.g. ``status=to-do``) inside the
    // sidebar form and re-submit so HTMX refreshes the result list.
    // Dispatches a real ``change`` event so per-row Alpine handlers
    // (assignee / project sticky-stack rows) update their state.
    removeFilter(name, value) {
      const form = document.getElementById("filter-form");
      if (!form) return;
      const inp = form.querySelector(
        `input[name="${name}"][value="${CSS.escape(value)}"]`,
      );
      if (!inp) return;
      inp.checked = false;
      inp.dispatchEvent(new Event("change", { bubbles: true }));
    },

    // Click-cell-to-filter helper. Each filterable cell in the task
    // table / list rows calls this with the filter ``name`` and value;
    // we find the matching label by stable ``data-filter-name`` /
    // ``data-filter-value`` attrs and dispatch a click. The input's
    // own ``name`` attribute is reactive on the chip's tri-state
    // Alpine, so a direct ``name=`` selector is fragile.
    //
    // Search is document-wide on purpose: status / priority / project
    // / label chips live inside ``#filter-form``, but the assignee
    // strip sits ABOVE the form (its inputs are associated via
    // ``form="filter-form"`` instead of nesting). One helper covers
    // both surfaces.
    //
    // Native ``<label>`` click → checkbox toggle → existing
    // ``@change`` handler runs and submits the form. Toggling an
    // already-active value clicks the label a second time, flipping
    // it off — same UX as clicking the sidebar chip twice.
    toggleFilter(name, value) {
      const label = document.querySelector(
        `label[data-filter-name="${name}"][data-filter-value="${CSS.escape(String(value))}"]`,
      );
      if (!label) return;
      label.click();
    },

    // Clear a non-value filter input (search ``q`` or the lone
    // ``show_done`` toggle) and re-submit.
    clearFilter(name) {
      const form = document.getElementById("filter-form");
      if (!form) return;
      const inp = form.querySelector(`input[name="${name}"]`);
      if (!inp) return;
      if (inp.type === "checkbox" || inp.type === "radio") {
        inp.checked = false;
        inp.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        inp.value = "";
        form.requestSubmit();
      }
    },
  };

  // Lazy-load alternate view bodies. Server renders only the active
  // view (table / kanban / list) on initial response and leaves
  // empty ``[data-panel-slot]`` placeholders for the rest. After the
  // page settles we fire one ``htmx.ajax`` per empty slot with
  // ``?panel=<key>``, which the server short-circuits to a single
  // partial (much cheaper than rebuilding the whole page).
  //
  // The biggest win is on All Tasks where the list panel alone
  // produces 5 axes × N rows of HTML; without this it doubles the
  // first-paint time. Once loaded, switching tabs stays instant
  // because all three panels live in the DOM.
  function lazyLoadPanels(basePath) {
    const slots = document.querySelectorAll("[data-panel-slot]");
    slots.forEach((slot) => {
      if (slot.children.length > 0) return; // already filled
      if (slot.dataset.panelLoading === "true") return; // request in flight
      const key = slot.dataset.panelSlot;
      if (!key || !window.htmx) return;
      // HTMX boost runs ``hx-push-url`` AFTER ``htmx:afterSettle``, so
      // ``window.location.href`` is still the *previous* URL when this
      // fires. Prefer the request path of the boost that just settled
      // (passed in by the listener) and fall back to window.location
      // for the cold-load case where there's no boost event.
      const base = basePath
        ? new URL(basePath, window.location.origin)
        : new URL(window.location.href);
      base.searchParams.set("panel", key);
      slot.dataset.panelLoading = "true";
      window.htmx.ajax("GET", base.pathname + base.search, {
        target: slot,
        swap: "innerHTML",
      });
    });
  }
  // Run after initial paint settles, and after any HTMX swap that
  // might bring back empty slots (filter form refresh swaps the
  // whole panel wrapper).
  document.body.addEventListener("htmx:afterSettle", (evt) => {
    const path = evt.detail && evt.detail.requestConfig && evt.detail.requestConfig.path;
    setTimeout(() => lazyLoadPanels(path), 50);
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => setTimeout(lazyLoadPanels, 50));
  } else {
    setTimeout(lazyLoadPanels, 50);
  }

  // Sidebar active-nav highlight. The sidebar element survives HTMX
  // boost navigation (only ``#app-content`` swaps), so Django's
  // template-time ``{% if current == "..." %}`` branch can't repaint
  // the active link — we toggle ``.acta-rail-active`` here based on
  // ``window.location.pathname`` instead. Runs on first paint and
  // after every HTMX swap (``htmx:afterSettle``); also runs on
  // ``popstate`` for browser back/forward.
  function refreshSidebarActive() {
    const path = window.location.pathname;
    document.querySelectorAll(".acta-rail-link[href]").forEach((a) => {
      let linkPath = "";
      try {
        linkPath = new URL(a.href, window.location.origin).pathname;
      } catch (_) {
        return;
      }
      // Default: exact match (top-level nav links — My Work, All Tasks,
      // Dashboard, Projects). Links marked ``data-nav-prefix`` (project
      // detail entries in the sidebar list) keep the highlight when the
      // user drills into a task inside the same project.
      const isPrefix = a.hasAttribute("data-nav-prefix");
      const active = isPrefix ? path.startsWith(linkPath) : linkPath === path;
      a.classList.toggle("acta-rail-active", active);
    });
  }
  refreshSidebarActive();
  document.body.addEventListener("htmx:afterSettle", refreshSidebarActive);
  window.addEventListener("popstate", refreshSidebarActive);

  // Sidebar icon-rail tooltips. CSS gives them visual chrome only —
  // we set ``top`` / ``left`` here on mouseenter so the tip lines up
  // with the link regardless of scroll position or parent overflow.
  // Delegated on document so links added by HTMX swaps work too.
  document.addEventListener("mouseover", function (evt) {
    const link = evt.target.closest(".acta-rail-link");
    if (!link) return;
    if (window.Alpine && window.Alpine.store && window.Alpine.store("sidebar")?.open) {
      // Sidebar is expanded — labels are inline, no tooltip needed.
      return;
    }
    const tip = link.querySelector(".acta-rail-tip");
    if (!tip) return;
    const rect = link.getBoundingClientRect();
    tip.style.left = rect.right + 10 + "px";
    tip.style.top = rect.top + rect.height / 2 + "px";
    tip.classList.add("is-visible");
  });
  document.addEventListener("mouseout", function (evt) {
    const link = evt.target.closest(".acta-rail-link");
    if (!link) return;
    const tip = link.querySelector(".acta-rail-tip");
    if (tip) tip.classList.remove("is-visible");
  });

  // ---- Client-side filter ------------------------------------------------
  //
  // Filter form (``#filter-form``) writes its state into per-row
  // ``data-*`` attributes via the ``task_filter_attrs`` template tag.
  // On any change we walk every ``[data-task-id]`` element on the page
  // (kanban cards, table rows, list rows all share the marker) and
  // toggle a ``hidden`` attribute. No HTTP round-trip; the server side
  // still handles cold loads + reset + future SSE refreshes.
  //
  // The mirror of ``apply_task_filters`` in apps/web/filters.py — keep
  // the two in sync when adding a new filter dimension.
  function readFilterState(form) {
    if (!form) return null;
    const fd = new FormData(form);
    const multi = (name) => fd.getAll(name).map((v) => String(v));
    // ``show_archived`` carries both a hidden ``0`` and the checkbox
    // ``1`` when checked. Match the server's "trailing 1 wins" parse.
    const archivedRaw = fd.getAll("show_archived");
    const showArchived = archivedRaw.includes("1");
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
      showArchived,
    };
  }

  function rowMatches(row, state) {
    // ``data-archived`` — hide archived rows unless show_archived is on.
    if (!state.showArchived && row.dataset.archived === "1") return false;
    // Status
    const s = row.dataset.status || "";
    if (state.status.size && !state.status.has(s)) return false;
    if (state.xstatus.size && state.xstatus.has(s)) return false;
    // Priority — DOM carries integer string.
    const p = row.dataset.priority || "0";
    if (state.priority.size && !state.priority.has(p)) return false;
    if (state.xpriority.size && state.xpriority.has(p)) return false;
    // Assignee — server-side tokens ``me`` / ``unassigned`` join numeric
    // user ids. We match by intersecting the requested set with the row's
    // possible tokens (numeric id, ``me``, ``unassigned``).
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
      for (const t of state.assignee) if (aTokens.has(t)) { ok = true; break; }
      if (!ok) return false;
    }
    if (state.xassignee.size) {
      for (const t of state.xassignee) if (aTokens.has(t)) return false;
    }
    // Project
    const proj = row.dataset.projectId || "";
    if (state.project.size && !state.project.has(proj)) return false;
    if (state.xproject.size && state.xproject.has(proj)) return false;
    // Workspace
    const ws = row.dataset.workspaceId || "";
    if (state.workspace.size && !state.workspace.has(ws)) return false;
    if (state.xworkspace.size && state.xworkspace.has(ws)) return false;
    // Labels — ``data-label-ids`` is space-separated.
    if (state.label.size || state.xlabel.size) {
      const rowLabels = new Set((row.dataset.labelIds || "").split(/\s+/).filter(Boolean));
      if (state.label.size) {
        let any = false;
        for (const id of state.label) if (rowLabels.has(id)) { any = true; break; }
        if (!any) return false;
      }
      if (state.xlabel.size) {
        for (const id of state.xlabel) if (rowLabels.has(id)) return false;
      }
    }
    // Search — substring against title + first 160 chars of description.
    if (state.q) {
      const hay = row.dataset.searchHaystack || "";
      if (!hay.includes(state.q)) return false;
    }
    return true;
  }

  function activeFilterCount(state) {
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

  function refreshFilterCountBadges(count) {
    // Server sets the count via OOB swap on full HTMX response; for the
    // local-only path we recompute and toggle the visibility classes
    // directly to keep the badge in sync without a round-trip.
    const visibleHide = (el) => {
      if (!el) return;
      if (count > 0) {
        el.classList.remove("hidden");
        el.textContent = String(count);
      } else {
        el.classList.add("hidden");
        el.textContent = "";
      }
    };
    visibleHide(document.getElementById("filter-count-collapsed"));
    visibleHide(document.getElementById("filter-count-expanded"));
  }

  function applyClientFilters() {
    const form = document.getElementById("filter-form");
    if (!form) return;
    // Inbox filters server-side (see ``bindFilterForm``) — never hide
    // rows client-side there.
    if (form.dataset.serverFilter === "true") return;
    const state = readFilterState(form);
    if (!state) return;
    const rows = document.querySelectorAll("[data-task-id]");
    let visible = 0;
    rows.forEach((row) => {
      // Skip elements that don't carry filter attrs (some
      // ``data-task-id`` markers live on activity rows etc.).
      if (!row.hasAttribute("data-status")) return;
      // For the list view the task element is an ``<a data-task-id>``
      // wrapped in a ``<li>`` — hiding only the ``<a>`` leaves the
      // ``<li>`` taking row height. Walk up to the closest ``<li>``
      // (or ``<tr>`` for tables) and toggle ``hidden`` on that.
      const target = row.closest("li") || row.closest("tr") || row;
      const match = rowMatches(row, state);
      if (match) {
        target.removeAttribute("hidden");
        target.style.display = "";
        visible += 1;
      } else {
        // ``[hidden]`` attribute has UA specificity 0,1,0 — equal to
        // Tailwind ``.block`` on kanban cards, so source order decides.
        // Inline ``display:none`` has the highest specificity and wins
        // unconditionally. Belt + suspenders: keep the attribute too so
        // ``:not([hidden])`` selectors (Tailwind ``space-y-*``) still
        // skip the row correctly when computing sibling spacing.
        target.setAttribute("hidden", "");
        target.style.display = "none";
      }
    });
    refreshFilterCountBadges(activeFilterCount(state));
    // Kanban column counts reflect visible cards, not the server-side
    // total — match the same logic the drag-and-drop handler uses
    // after a successful drop. List-view section counts are
    // server-rendered and not yet recomputed here (TODO).
    document.querySelectorAll(".kanban-column").forEach((c) => {
      const visible = c.querySelectorAll("[data-task-id]:not([hidden])").length;
      const counter = c.parentElement?.querySelector("[data-column-count]");
      if (counter) counter.textContent = String(visible);
    });
    recomputeKanbanSubstatus();
    // Update ``acta_show_archived`` cookie so a hard refresh remembers
    // the toggle — server-side fallback path reads this on cold load.
    const oneYear = 60 * 60 * 24 * 365;
    document.cookie = `acta_show_archived=${state.showArchived ? "1" : "0"}; path=/; max-age=${oneYear}; samesite=Lax`;
    // Mirror URL params so refresh / share carry the same filter
    // state — Django filter view re-renders identically on cold load.
    if (window.history && window.history.replaceState) {
      const params = new URLSearchParams(window.location.search);
      // Replace filter-related keys; preserve everything else (sort,
      // view, axis).
      const keys = ["status", "xstatus", "priority", "xpriority", "assignee",
        "xassignee", "project", "xproject", "workspace", "xworkspace",
        "label", "xlabel", "q", "show_archived"];
      keys.forEach((k) => params.delete(k));
      const fd = new FormData(form);
      for (const [k, v] of fd.entries()) {
        if (!keys.includes(k)) continue;
        if (k === "show_archived") {
          // hidden ``0`` + checkbox ``1`` — keep only the trailing
          // value (same logic as resolve_show_archived).
          if (v === "1") params.set("show_archived", "1");
          continue;
        }
        if (v) params.append(k, v.toString());
      }
      const qs = params.toString();
      window.history.replaceState({}, "", window.location.pathname + (qs ? "?" + qs : ""));
    }
    // Timeline view keeps a two-pane layout the generic row loop already
    // hid (both left + gantt rows carry the filter attrs). Let it
    // recompute the today-line height against the now-visible row count.
    if (window.__tlAfterFilter) window.__tlAfterFilter();
    return visible;
  }
  window.actaApplyFilters = applyClientFilters;

  // Walk every kanban column, look at the *visible* cards inside,
  // and refresh the substatus row (overdue count / "++ N this week" /
  // avatar stack) so the header doesn't carry stale numbers after a
  // client-side filter hides cards. Matches the logic the server
  // applies in ``_build_kanban_columns`` (apps/web/views.py) but
  // re-runs in pure DOM after every filter pass.
  function recomputeKanbanSubstatus() {
    document.querySelectorAll(".kanban-column").forEach((body) => {
      const status = body.dataset.status;
      if (!status) return;
      // The substatus row sits in the column header (sibling of the
      // ``.kanban-column`` body). Walk up to the column container
      // and find the row by its data-attr.
      const column = body.closest("[x-data]");
      if (!column) return;
      const row = column.querySelector(`[data-substatus-row="${status}"]`);
      if (!row) return;

      const visibleCards = body.querySelectorAll("[data-task-id]:not([hidden])");
      let overdueCount = 0;
      let doneThisWeekCount = 0;
      const seenAssignees = new Set();
      const avatarSources = [];

      visibleCards.forEach((card) => {
        if (card.dataset.overdue === "1") overdueCount += 1;
        if (card.dataset.doneThisWeek === "1") doneThisWeekCount += 1;

        const assigneeId = card.dataset.assigneeId;
        if (assigneeId && !seenAssignees.has(assigneeId) && avatarSources.length < 4) {
          seenAssignees.add(assigneeId);
          const av = card.querySelector("[data-task-assignee-avatar]");
          if (av) {
            avatarSources.push({
              bg: av.style.backgroundColor,
              initial: av.textContent.trim(),
              name: av.getAttribute("title") || "",
            });
          }
        }
      });

      const overdueEl = row.querySelector("[data-substatus-overdue]");
      const doneEl = row.querySelector("[data-substatus-done-this-week]");
      const emptyEl = row.querySelector("[data-substatus-empty]");
      const avatarsEl = row.querySelector("[data-substatus-avatars]");

      const showOverdue = overdueCount > 0;
      const showDoneThisWeek = status === "done" && doneThisWeekCount > 0 && !showOverdue;

      if (overdueEl) {
        overdueEl.textContent = `!! ${overdueCount} overdue`;
        overdueEl.classList.toggle("hidden", !showOverdue);
      }
      if (doneEl) {
        doneEl.textContent = `++ ${doneThisWeekCount} this week`;
        doneEl.classList.toggle("hidden", !showDoneThisWeek);
      }
      if (emptyEl) {
        emptyEl.classList.toggle("hidden", showOverdue || showDoneThisWeek);
      }

      if (avatarsEl) {
        avatarsEl.innerHTML = "";
        avatarSources.forEach((src, i) => {
          const span = document.createElement("span");
          span.className =
            "w-3.5 h-3.5 rounded-full text-white grid place-items-center text-[8px] font-medium" +
            (i > 0 ? " -ml-1" : "");
          span.style.backgroundColor = src.bg;
          span.style.boxShadow = "0 0 0 1.5px rgb(var(--card))";
          span.setAttribute("title", src.name);
          span.textContent = src.initial;
          avatarsEl.appendChild(span);
        });
      }
    });
  }

  function bindFilterForm() {
    const form = document.getElementById("filter-form");
    if (!form || form.dataset.clientFiltersBound === "true") return;
    // The inbox reuses ``#filter-form`` for its project strip but filters
    // server-side (notifications / updates aren't task rows), so leave
    // HTMX's normal round-trip alone — don't hijack it with the
    // client-side task filter below.
    if (form.dataset.serverFilter === "true") return;
    form.dataset.clientFiltersBound = "true";
    // The filter chips in ``_filters_sidebar.html`` use Alpine
    // ``@change.stop`` — that stops the change event from bubbling to
    // the form, so we can't hang ``change`` listeners on the form
    // itself. They DO call ``form.requestSubmit()`` though, which
    // routes through HTMX → ``htmx:beforeRequest`` fires on the
    // form. We hijack that hook: cancel the request and run our
    // client-side filter instead. ``htmx:beforeRequest`` survives
    // ``.stop`` because it's dispatched by HTMX itself on the form
    // node, not bubbled up from the chip.
    form.addEventListener("htmx:beforeRequest", (evt) => {
      evt.preventDefault();
      applyClientFilters();
    });
    // Search input doesn't go through ``requestSubmit`` (no
    // ``hx-trigger`` on it), so we drive it directly with a debounced
    // input listener.
    const q = form.querySelector('input[name="q"]');
    if (q) {
      let qTimer = null;
      q.addEventListener("input", () => {
        if (qTimer) clearTimeout(qTimer);
        qTimer = setTimeout(applyClientFilters, 150);
      });
    }
  }
  bindFilterForm();
  document.body.addEventListener("htmx:afterSettle", () => {
    // Re-bind in case the form was swapped (panel re-render) and
    // re-apply filters so freshly server-rendered rows pick up the
    // current client state.
    bindFilterForm();
    applyClientFilters();
  });
  // Reset button broadcasts ``acta:filter-reset`` — chips reset
  // themselves via @acta:filter-reset.window listeners. After they've
  // settled, re-apply (empty state → everything visible).
  window.addEventListener("acta:filter-reset", () => {
    setTimeout(applyClientFilters, 0);
  });

  // Client-side table sort. Each ``<tr>`` carries ``data-sort-*``
  // attributes pre-rendered by the server (see _table.html). On a
  // sort-header click we reshuffle rows in-place — no HTTP round-trip,
  // no DOM replacement — so sort is instant up to a few thousand
  // rows. URL still updates via ``history.pushState`` so a refresh
  // or shared link picks the same order back up on the server.
  //
  // Comparators mirror ``apply_task_ordering`` in apps/web/filters.py:
  // status uses workflow order (encoded as 0-4 in data-sort-status),
  // priority sinks "no priority" via the rank 99, assignee / due /
  // size are NULLS LAST regardless of direction.
  const SORT_BLANK_LAST_KEYS = new Set(["size", "due", "assignee"]);
  const SORT_NUMERIC_KEYS = new Set(["status", "priority", "size"]);
  function compareRows(a, b, key, dir) {
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

  function applyClientSort(tbody, clauses) {
    // ``clauses`` is an array of ``{key, dir}`` evaluated in order
    // (lexicographic on the first column, ties broken by the next,
    // and so on) — matches Django's multi-key ``order_by``.
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

  function parseClauses(str) {
    // ``"status,-priority,-updated"`` → list of ``{key, dir}``.
    return (str || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
      .map((token) => {
        const dir = token.startsWith("-") ? "desc" : "asc";
        return { key: token.replace(/^-/, ""), dir };
      });
  }

  function parseOrder(orderParam) {
    const raw = (orderParam || "").trim();
    if (!raw) return { key: "", dir: "asc" };
    const dir = raw.startsWith("-") ? "desc" : "asc";
    const key = raw.replace(/^-/, "");
    return { key, dir };
  }

  function nextSortState(currentKey, currentDir, clickedKey) {
    // Three-state cycle on the same column: none → asc → desc → none.
    // Clicking a different column resets to asc on the new column.
    // ``key === ""`` represents the "none" / default state.
    if (clickedKey !== currentKey) return { key: clickedKey, dir: "asc" };
    if (currentDir === "asc") return { key: clickedKey, dir: "desc" };
    return { key: "", dir: "asc" }; // cleared
  }

  function buildUrl(currentSearch, nextKey, nextDir) {
    const params = new URLSearchParams(currentSearch);
    if (nextKey) {
      params.set("order", nextDir === "desc" ? "-" + nextKey : nextKey);
    } else {
      params.delete("order");
    }
    const qs = params.toString();
    return window.location.pathname + (qs ? "?" + qs : "");
  }

  function refreshSortIndicators(table, activeKey, activeDir) {
    // Update the trailing arrow span in each header link. Server-side
    // ``sort_indicator`` filter rendered it on cold load; from there
    // we own it.
    table.querySelectorAll("a[data-sort-key]").forEach((a) => {
      const span = a.querySelector("span.text-brand-400");
      if (!span) return;
      const linkKey = a.getAttribute("data-sort-key");
      if (linkKey === activeKey) {
        span.textContent = activeDir === "desc" ? "↓" : "↑";
      } else {
        span.textContent = "";
      }
    });
  }

  document.addEventListener("click", function onSortLinkClick(evt) {
    // Modified clicks should still open in a new tab as usual.
    if (evt.metaKey || evt.ctrlKey || evt.shiftKey || evt.altKey) return;
    if (evt.button !== 0) return;
    const a = evt.target.closest("a[data-sort-key]");
    if (!a) return;
    const root = a.closest("[data-task-list-root]");
    if (!root) return;
    const table = root.querySelector("table");
    const tbody = table && table.querySelector("tbody");
    if (!tbody) return;
    const clickedKey = a.getAttribute("data-sort-key");
    if (!clickedKey) return;
    evt.preventDefault();
    // The next state is derived from the **current URL**, not from the
    // server-rendered ``href`` — the href is set once at render time
    // and goes stale after the first click. Reading the live URL each
    // time keeps the three-state cycle (none → asc → desc → none)
    // working across repeated clicks on the same column.
    const current = parseOrder(new URL(window.location.href).searchParams.get("order"));
    const next = nextSortState(current.key, current.dir, clickedKey);
    const nextUrl = buildUrl(window.location.search, next.key, next.dir);
    if (next.key) {
      applyClientSort(tbody, [{ key: next.key, dir: next.dir }]);
      refreshSortIndicators(table, next.key, next.dir);
    } else {
      // Cleared sort — re-apply the page's default ordering entirely
      // client-side. The server exposes it via
      // ``data-default-order`` on ``#task-table-root`` so we don't
      // have to round-trip just to undo a sort.
      const tableRoot = root.querySelector("#task-table-root");
      const defaultClauses = parseClauses(tableRoot && tableRoot.getAttribute("data-default-order"));
      if (defaultClauses.length) {
        applyClientSort(tbody, defaultClauses);
      }
      refreshSortIndicators(table, "", "asc");
    }
    if (window.history && window.history.pushState) {
      window.history.pushState({}, "", nextUrl);
    }
  });

  // Global ``c`` hotkey — opens the Create Task modal. Lives in JS so
  // it survives HTMX swaps without re-binding, and reliably ignores
  // keys typed into inputs / textareas / contenteditable surfaces.
  // Reads the endpoint URL from a ``data-create-task-url`` attribute
  // on the root app shell so the Django {% url %} reverse is rendered
  // exactly once on the page and the JS stays template-agnostic.
  function isTypingTarget(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  }
  document.addEventListener("keydown", function onCreateTaskHotkey(evt) {
    if (evt.key !== "c" && evt.key !== "C") return;
    if (evt.metaKey || evt.ctrlKey || evt.altKey) return;
    if (isTypingTarget(evt.target)) return;
    const root = document.getElementById("modal-root");
    if (!root || root.innerHTML.trim() !== "") return;
    const shell = document.querySelector("[data-create-task-url]");
    if (!shell || !window.htmx) return;
    evt.preventDefault();
    // Object form is the documented htmx 2.x signature for target+swap;
    // bare-string target works in practice but the explicit form is
    // less surprising when you read the code later.
    window.htmx.ajax("GET", shell.dataset.createTaskUrl, {
      target: "#modal-root",
      swap: "innerHTML",
    });
  });

  // Lucide icons used to be ``<i data-lucide="...">`` placeholders
  // hydrated client-side by ``lucide.min.js`` on load + every HTMX
  // swap. Server now renders inline SVG via the ``{% lucide %}``
  // template tag (apps/web/templatetags/lucide.py), so no JS pass is
  // needed — icons land with the first paint and survive DOM swaps
  // without the empty-frame flicker the JS replacement caused.
  // Kept ``renderIcons`` as a no-op shim so external call sites
  // (existing ``applyCardReplace``, etc.) don't have to be edited;
  // remove once those call sites are gone.
  function renderIcons() {}

  // Sticky-row stacking for filter lists (project / assignee). When
  // multiple selected rows pin to the same edge of a scroll container,
  // default z-index makes the *furthest scrolled past* row visible
  // (last on top), which feels wrong — the user expects the one
  // *closest to the viewport* to be on top. We bump z-index per row
  // based on whether its natural position is above / inside / below
  // the visible window:
  //   - naturally above viewport (sticking to top:0) → later DOM wins
  //   - naturally below viewport (sticking to bottom:0) → earlier DOM wins
  //   - currently in view → plain z, no override
  // Pure CSS can't express that; ~20 lines of JS does it cleanly.
  function updateStickyStack(container) {
    const viewTop = container.scrollTop;
    const viewBottom = viewTop + container.clientHeight;
    const rows = container.querySelectorAll("[data-sticky-row]");
    const total = rows.length;
    rows.forEach((el, idx) => {
      // Read the live checkbox state — avoids racing with Alpine's
      // ``:data-selected`` reactivity which can lag a microtask behind.
      const input = el.querySelector("input[type=checkbox]");
      const isSelected = !!(input && input.checked);
      if (!isSelected) {
        el.style.zIndex = "";
        return;
      }
      // Classify by which sticky edge the browser is actually pinning
      // the row at — covers partial overlaps (row half in view, half
      // below). Among rows stuck at the top edge the *last* one to
      // leave wins z (highest idx); among bottom-pinned the *last*
      // one to leave also wins (lowest idx, since the one closest to
      // the visible area is the most recent to scroll past).
      const natTop = el.offsetTop;
      const natBottom = natTop + el.offsetHeight;
      const pinnedTop = natTop < viewTop;
      const pinnedBottom = natBottom > viewBottom;
      let z;
      if (pinnedTop && !pinnedBottom) z = 10 + idx;
      else if (pinnedBottom && !pinnedTop) z = 10 + (total - idx);
      else z = 10;
      el.style.zIndex = String(z);
    });
  }
  window.acta.updateStickyStack = updateStickyStack;

  function initStickyStacks() {
    document.querySelectorAll("[data-sticky-stack]").forEach((container) => {
      if (container.dataset.stickyBound === "true") return;
      container.dataset.stickyBound = "true";
      updateStickyStack(container);
      container.addEventListener("scroll", () => updateStickyStack(container), {
        passive: true,
      });
    });
  }

  // Page-top assignee / project strip: counts off-screen chips on
  // each edge, sets ``data-overflow-left`` / ``data-overflow-right``
  // on the wrapper, and updates the ``+N`` text inside the counter
  // overlays. CSS handles the actual fade-in / fade-out via opacity
  // transitions tied to those attributes — keeps the overlays in the
  // DOM (and out of flex flow, via ``absolute`` positioning) so the
  // strip never shifts when off-screen counts change.
  function updateStripCounters(strip) {
    const scrollLeft = strip.scrollLeft;
    const viewRight = scrollLeft + strip.clientWidth;
    const chips = [...strip.querySelectorAll("[data-strip-chip]")];
    let leftCount = 0;
    let rightCount = 0;
    chips.forEach((chip) => {
      const natLeft = chip.offsetLeft;
      const natRight = natLeft + chip.offsetWidth;
      if (natRight <= scrollLeft) leftCount += 1;
      else if (natLeft >= viewRight) rightCount += 1;
    });
    const wrap = strip.parentElement;
    wrap.toggleAttribute("data-overflow-left", leftCount > 0);
    wrap.toggleAttribute("data-overflow-right", rightCount > 0);
    const leftCountEl = wrap.querySelector("[data-strip-left-counter] [data-count]");
    const rightCountEl = wrap.querySelector("[data-strip-right-counter] [data-count]");
    if (leftCountEl) leftCountEl.textContent = leftCount;
    if (rightCountEl) rightCountEl.textContent = rightCount;
  }
  window.acta.updateStripCounters = updateStripCounters;

  function initStrips() {
    document.querySelectorAll("[data-strip]").forEach((strip) => {
      if (strip.dataset.stripBound === "true") return;
      strip.dataset.stripBound = "true";
      updateStripCounters(strip);
      // Clamp scrollLeft to the actual content extent after every
      // scroll event. Belt-and-braces against browsers that allow
      // a strip to settle past ``scrollWidth - clientWidth`` (touchpad
      // inertia / elastic-overscroll on macOS); without this the strip
      // would park itself with visible empty space to the right of the
      // last chip, which read as a layout bug to the user.
      // Hard scroll limit via wheel intercept. ``scrollWidth`` on this
      // strip overshoots the actual content extent by ~280px (browser
      // quirk on ``flex`` + ``scrollbar-width: none``), so the
      // browser's native scroll allows parking past the last chip
      // into empty space. Earlier debounced snap-back caused either a
      // jerk (during scroll) or a delayed animation (after release)
      // — both irritating. The wheel handler computes the real chip
      // max and clamps deltaX *before* the browser applies it, so the
      // strip simply never scrolls past the last chip in the first
      // place. ``preventDefault`` is needed for the clamp to win over
      // native scroll, hence ``passive: false``.
      const chipMax = () => {
        const chips = strip.querySelectorAll("[data-strip-chip]");
        const lastChip = chips[chips.length - 1];
        if (!lastChip) return Infinity;
        return Math.max(0, lastChip.offsetLeft + lastChip.offsetWidth - strip.clientWidth);
      };
      strip.addEventListener(
        "wheel",
        (e) => {
          // Treat trackpad horizontal swipe (deltaX) and shift+wheel
          // (deltaY with shift) as horizontal intent. Plain vertical
          // wheel falls through so the page can scroll.
          const dx = e.deltaX !== 0 ? e.deltaX : e.shiftKey ? e.deltaY : 0;
          if (dx === 0) return;
          const max = chipMax();
          const next = Math.max(0, Math.min(max, strip.scrollLeft + dx));
          if (next === strip.scrollLeft) {
            // Already at edge in the intended direction — let the
            // browser do nothing too (preventDefault stops page scroll
            // from also reacting to the gesture).
            e.preventDefault();
            return;
          }
          e.preventDefault();
          strip.scrollLeft = next;
        },
        { passive: false },
      );
      strip.addEventListener("scroll", () => updateStripCounters(strip), {
        passive: true,
      });
      window.addEventListener("resize", () => updateStripCounters(strip), {
        passive: true,
      });
    });
  }

  // Generic scroll-fade visibility: an element with ``data-scroll-target``
  // tells JS to watch its scroll position. Whenever there's more content
  // past either edge, the corresponding ``data-overflow-*`` attribute
  // pops onto the **parent** wrapper; CSS rules tie fade overlays to
  // those attributes (so they fade in only when there's actually more
  // to scroll to).
  function updateScrollFades(target) {
    const wrap = target.parentElement;
    if (!wrap) return;
    const maxX = target.scrollWidth - target.clientWidth;
    const maxY = target.scrollHeight - target.clientHeight;
    wrap.toggleAttribute("data-overflow-left", target.scrollLeft > 0);
    wrap.toggleAttribute("data-overflow-right", target.scrollLeft < maxX - 1);
    wrap.toggleAttribute("data-overflow-top", target.scrollTop > 0);
    wrap.toggleAttribute("data-overflow-bottom", target.scrollTop < maxY - 1);
  }
  window.acta.updateScrollFades = updateScrollFades;

  function initScrollFades() {
    document.querySelectorAll("[data-scroll-target]").forEach((target) => {
      if (target.dataset.scrollFadesBound === "true") return;
      target.dataset.scrollFadesBound = "true";
      updateScrollFades(target);
      target.addEventListener("scroll", () => updateScrollFades(target), {
        passive: true,
      });
      window.addEventListener("resize", () => updateScrollFades(target), {
        passive: true,
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      initStickyStacks();
      initStrips();
      initScrollFades();
    });
  } else {
    initStickyStacks();
    initStrips();
    initScrollFades();
  }
  document.body.addEventListener("htmx:afterSwap", () => {
    initStickyStacks();
    initStrips();
    initScrollFades();
  });

  // Workspace SSE — opens a single EventSource per page on the
  // ``[data-workspace-sse]`` wrapper and dispatches typed events to
  // DOM updaters. Server pre-renders the affected ``_task_card.html``
  // and puts it in ``data.card_html``; the client just swaps the
  // existing card (or moves it to a different kanban column for
  // status changes). Actor exclusion is client-side via
  // ``data.actor_id`` vs ``data-current-user-id`` on the wrapper —
  // skip our own change since the originating HTTP response already
  // refreshed the UI.
  // Kanban-card selector. ``<a data-task-id>`` is the card element
  // rendered by ``_task_card.html``. The selector deliberately
  // excludes other elements carrying ``data-task-id`` (the activity
  // panel uses ``data-activity-for-task`` to avoid the clash).
  const KANBAN_CARD = (id) => `a[data-task-id="${id}"]`;

  function applyCardReplace(taskId, cardHtml) {
    if (!cardHtml) return;
    const existing = document.querySelector(KANBAN_CARD(taskId));
    if (!existing) return;
    const tmp = document.createElement("div");
    tmp.innerHTML = cardHtml.trim();
    const fresh = tmp.firstElementChild;
    if (!fresh) return;
    existing.replaceWith(fresh);
    renderIcons();
  }
  function applyCardMove(taskId, newStatus, cardHtml) {
    if (!cardHtml) return;
    document.querySelectorAll(KANBAN_CARD(taskId)).forEach((el) => el.remove());
    const column = document.querySelector(`.kanban-column[data-status="${newStatus}"]`);
    if (!column) return;
    const tmp = document.createElement("div");
    tmp.innerHTML = cardHtml.trim();
    const fresh = tmp.firstElementChild;
    if (!fresh) return;
    // Server sorts each column by ``-priority, -updated_at`` (see
    // ProjectDetailView.get_context_data) — a freshly moved card has the
    // newest ``updated_at`` so on reload it lands at the top of its
    // priority bucket. Insert just after the ``.empty-placeholder`` so
    // peers see the same order without a refresh.
    const placeholder = column.querySelector(".empty-placeholder");
    if (placeholder && placeholder.nextSibling) {
      column.insertBefore(fresh, placeholder.nextSibling);
    } else if (placeholder) {
      column.appendChild(fresh);
    } else {
      column.insertBefore(fresh, column.firstChild);
    }
    renderIcons();
  }
  function applyCardRemove(taskId) {
    document.querySelectorAll(KANBAN_CARD(taskId)).forEach((el) => el.remove());
  }

  // Track which SSE channels we've already subscribed to. Some pages
  // (project / task detail) carry a specific ``data-workspace-sse``
  // *and* the global app shell may emit a marker for the same
  // workspace — only open the connection once per unique URL.
  const SSE_BOUND_URLS = new Set();

  function initWorkspaceSse() {
    // Bind one EventSource per ``[data-workspace-sse]`` element. Most
    // pages have a single workspace context (project / task detail),
    // but cross-workspace surfaces (My Work, All Tasks) emit one
    // marker per workspace the user belongs to so SSE updates from
    // any of them flow through. ``SSE_BOUND_URLS`` plus ``sseBound``
    // guard keep re-init idempotent on HTMX swaps.
    document.querySelectorAll("[data-workspace-sse]").forEach(initOneWorkspaceSse);
  }

  function initOneWorkspaceSse(root) {
    if (!root || root.dataset.sseBound === "true") return;
    const url = root.getAttribute("data-workspace-sse");
    if (!url || SSE_BOUND_URLS.has(url)) {
      root.dataset.sseBound = "true";
      return;
    }
    root.dataset.sseBound = "true";
    SSE_BOUND_URLS.add(url);
    const meId = root.getAttribute("data-current-user-id") || "";
    const source = new EventSource(url);
    // Close the stream cleanly on navigation/reload. Without this the
    // browser keeps the TCP connection half-open until the OS times it
    // out, which makes the next request to the same origin wait — most
    // visibly in dev where uvicorn's graceful reload pauses on every
    // open stream ("Waiting for connections to close.").
    const closeStream = () => {
      try {
        source.close();
      } catch (_) {
        /* already closed */
      }
    };
    window.addEventListener("pagehide", closeStream);
    window.addEventListener("beforeunload", closeStream);

    const handle = (eventName, fn) => {
      source.addEventListener(eventName, (e) => {
        let data;
        try {
          data = JSON.parse(e.data);
        } catch (_) {
          return;
        }
        // Drop self-events to avoid double-rendering (kanban drag,
        // inline edits etc.) — *except* when the event came in via MCP
        // (Claude Desktop, Cursor, curl): those write through a different
        // client process the local tab doesn't know about, so the local
        // tab must apply the SSE swap to stay in sync.
        if (String(data.actor_id) === meId && !data.via_mcp) return;
        fn(data);
      });
    };

    // ----- SSE peer-edit handling --------------------------------
    //
    // Every task-mutation event arrives with pre-rendered HTML for
    // each surface the task can appear on — see
    // ``broadcast_task_events`` in apps/tasks/events.py. The client
    // applies whatever HTML the payload carries; no extra HTTP
    // round-trips, no fragment endpoint dance.
    //
    // ``morph:outerHTML`` (idiomorph) patches the existing DOM node
    // in place instead of remove + reinsert — keeps focus, Alpine
    // state, and avoids the empty-frame flicker that a naive
    // outerHTML swap produces.
    //
    // List view groups by axis with section headers + counts; a
    // peer's edit can move a task between sections, and an in-place
    // row swap would leave it in the old section with stale section
    // count. So list is the one surface that still refetches its
    // whole panel (one HTMX request, debounced).
    let listPanelRefetchTimer = null;
    function refreshListPanel() {
      if (!window.htmx) return;
      if (listPanelRefetchTimer) clearTimeout(listPanelRefetchTimer);
      listPanelRefetchTimer = setTimeout(() => {
        document.querySelectorAll('[data-panel-slot="list"]').forEach((slot) => {
          const url = new URL(window.location.href);
          url.searchParams.set("panel", "list");
          window.htmx.ajax("GET", url.pathname + url.search, {
            target: slot,
            swap: "innerHTML",
          });
        });
        listPanelRefetchTimer = null;
      }, 250);
    }

    function morphFromString(targetEl, html) {
      if (!targetEl || !html) return;
      const tpl = document.createElement("template");
      tpl.innerHTML = html.trim();
      const fresh = tpl.content.firstElementChild;
      if (!fresh) return;
      if (window.Idiomorph) {
        window.Idiomorph.morph(targetEl, fresh, { morphStyle: "outerHTML" });
      } else {
        targetEl.replaceWith(fresh);
      }
    }

    function applyRowHtmlTable(taskId, html) {
      if (!html) return;
      document
        .querySelectorAll(`tr[data-task-id="${taskId}"]`)
        .forEach((tr) => morphFromString(tr, html));
    }

    // Single dispatcher used by every per-task update event.
    function applyTaskUpdate(d) {
      if (d.card_html) {
        applyCardReplace(d.target_id, d.card_html);
      }
      if (d.row_html_table) {
        applyRowHtmlTable(d.target_id, d.row_html_table);
      }
      // List view rebuilds the whole panel — group membership and
      // section counts re-compute together. Debounced.
      refreshListPanel();
    }

    handle("task.status_changed", (d) => {
      // Status change is the one event that *moves* the kanban card
      // between columns — applyCardMove handles that; everything else
      // (table / list) goes through the standard update dispatcher.
      applyCardMove(d.target_id, d.to, d.card_html);
      if (d.row_html_table) applyRowHtmlTable(d.target_id, d.row_html_table);
      refreshListPanel();
    });
    handle("task.assigned", applyTaskUpdate);
    handle("task.priority_changed", applyTaskUpdate);
    handle("task.due_changed", applyTaskUpdate);
    handle("task.labels_changed", applyTaskUpdate);
    handle("task.updated", applyTaskUpdate);
    handle("task.archived", applyTaskUpdate);
    handle("task.unarchived", applyTaskUpdate);

    // Link events bypass the self-filter on purpose: adding a link only
    // swapped the rail panel (#task-links), so the board card / table row
    // for this task — and the linked task — are still stale and need the
    // SSE refresh even when the acting user is the one looking at them.
    ["task.link_added", "task.link_removed"].forEach((name) => {
      source.addEventListener(name, (e) => {
        let d;
        try {
          d = JSON.parse(e.data);
        } catch (_) {
          return;
        }
        applyTaskUpdate(d);
      });
    });
    handle("task.deleted", (d) => {
      applyCardRemove(d.target_id);
      document.querySelectorAll(`tr[data-task-id="${d.target_id}"]`).forEach((el) => el.remove());
      refreshListPanel();
    });

    // New task from another user — server emits ``task.created``
    // without a pre-rendered card (the create path uses
    // ``log_event`` directly, not the diff broadcaster). Mirror the
    // same custom event the local create flow already fires; panel
    // wrappers refetch themselves and the new row shows up. The
    // flash is acceptable here because there's no other way to
    // insert the row into the existing DOM.
    handle("task.created", () => {
      document.body.dispatchEvent(new CustomEvent("acta:task-created", { bubbles: true }));
    });

    // Live refresh on the task detail page. Each section wrapper
    // carries a ``data-*-for-task`` attribute + ``hx-trigger="refresh"``
    // + ``hx-get`` so we just dispatch the ``refresh`` event on the
    // wrapper whose ``data-*-for-task`` matches the SSE event's task.
    //
    // ``querySelectorAll`` (not ``querySelector``) is intentional —
    // when a task is open in modal-mode, the *same* task can also be
    // visible in the underlying page (e.g. as a kanban card or table
    // row). Each occurrence has its own wrapper; refreshing all of
    // them is harmless and keeps every surface in sync.
    const refreshIf = (selector, attr, taskId) => {
      document.querySelectorAll(selector).forEach((el) => {
        if (String(el.dataset[attr]) !== String(taskId)) return;
        el.dispatchEvent(new CustomEvent("refresh"));
      });
    };
    const refreshTimeline = (taskId) => refreshIf("#task-timeline-wrap", "timelineForTask", taskId);
    const refreshMeta = (taskId) => {
      refreshIf("#task-meta", "metaForTask", taskId);
      refreshIf("#task-meta-compact", "metaForTask", taskId);
    };
    const refreshTitle = (taskId) => {
      refreshIf("#title-section", "titleForTask", taskId);
      refreshIf("#topbar-task-title", "titleTopbarForTask", taskId);
    };
    const refreshDescription = (taskId) => refreshIf("#description", "descriptionForTask", taskId);

    const taskEvents = [
      "task.status_changed",
      "task.assigned",
      "task.priority_changed",
      "task.due_changed",
      "task.labels_changed",
      "task.deleted",
    ];
    taskEvents.forEach((t) =>
      handle(t, (d) => {
        refreshTimeline(d.target_id);
        refreshMeta(d.target_id);
      }),
    );
    // ``task.updated`` is the catch-all for title / description / size
    // edits. Inspect ``payload.changes`` so we only refresh the cells
    // that actually changed rather than retemplating the whole page
    // on a stray rename.
    handle("task.updated", (d) => {
      refreshTimeline(d.target_id);
      refreshMeta(d.target_id);
      const changes = d.changes || {};
      if (changes.title) refreshTitle(d.target_id);
      if (changes.description) refreshDescription(d.target_id);
    });
    const commentEvents = ["comment.created", "comment.updated", "comment.deleted"];
    commentEvents.forEach((t) =>
      handle(t, (d) => {
        refreshTimeline(d.task_id);
      }),
    );
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initWorkspaceSse);
  } else {
    initWorkspaceSse();
  }
  document.body.addEventListener("htmx:afterSwap", initWorkspaceSse);

  // ----- Per-user notification stream (live inbox) ----------------
  //
  // A second EventSource on the private ``user-<id>`` channel. No
  // self-filter here: the server never delivers a notification to its
  // own actor, so anything arriving on this channel is genuinely for
  // me. Each ``notification.created`` event carries pre-rendered row +
  // badge HTML (see ``apps.notifications.services._broadcast_notification``).
  const USER_SSE_BOUND = new Set();
  const INBOX_KIND_FILTER = { mentions: "mention", assigned: "assigned", due: "due", comments: "comment" };

  function onNotificationCreated(d) {
    // 1) Sidebar unread badge — replace its node, then pulse once.
    const badge = document.getElementById("inbox-badge");
    if (badge && d.badge_html) {
      const tpl = document.createElement("template");
      tpl.innerHTML = d.badge_html.trim();
      const fresh = tpl.content.firstElementChild;
      if (fresh) {
        // Drop ``x-cloak`` on the live-injected node — it's only needed
        // to prevent a flash before Alpine binds on first page paint;
        // here keeping it would hide the badge for a frame and twitch
        // the sidebar row as the digit appears.
        fresh.removeAttribute("x-cloak");
        badge.replaceWith(fresh);
        fresh.classList.add("inbox-pulse");
        setTimeout(() => fresh.classList.remove("inbox-pulse"), 3400);
      }
    }
    // 2) Inbox list — prepend the new row when the inbox is open and the
    // active filter would include it (new rows are always unread). For
    // non-matching filters the badge still bumps; the row shows on the
    // next list fetch.
    const list = document.getElementById("inbox-list");
    if (list && d.row_html) {
      const f = list.getAttribute("data-inbox-filter") || "all";
      const include = f === "all" || f === "unread" || INBOX_KIND_FILTER[f] === d.kind;
      const rows = list.querySelector("[data-inbox-rows]");
      if (include && rows) {
        const tpl = document.createElement("template");
        tpl.innerHTML = d.row_html.trim();
        const row = tpl.content.firstElementChild;
        if (row) {
          row.classList.add("inbox-newly");
          rows.prepend(row);
          if (window.htmx) window.htmx.process(row);
        }
      }
    }
  }

  function initUserSse() {
    document.querySelectorAll("[data-user-sse]").forEach((root) => {
      if (root.dataset.sseBound === "true") return;
      const url = root.getAttribute("data-user-sse");
      if (!url || USER_SSE_BOUND.has(url)) {
        root.dataset.sseBound = "true";
        return;
      }
      root.dataset.sseBound = "true";
      USER_SSE_BOUND.add(url);
      const source = new EventSource(url);
      const close = () => {
        try {
          source.close();
        } catch (_) {
          /* already closed */
        }
      };
      window.addEventListener("pagehide", close);
      window.addEventListener("beforeunload", close);
      source.addEventListener("notification.created", (e) => {
        let d;
        try {
          d = JSON.parse(e.data);
        } catch (_) {
          return;
        }
        onNotificationCreated(d);
      });
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initUserSse);
  } else {
    initUserSse();
  }
  document.body.addEventListener("htmx:afterSwap", initUserSse);

  // Remember the URL we came from any time a click is about to open a
  // modal-root surface. The task-detail modal shell uses this on close
  // to ``history.replaceState`` back without triggering popstate
  // (which would conflict with hx-boost on the sidebar). We capture
  // on ``click`` in the capture phase — runs *before* HTMX intercepts
  // the click and before ``hx-push-url`` rewrites the address bar, so
  // ``window.location.href`` is still the original page (project list,
  // all tasks, full task page, wherever the user came from).
  //
  // We save on every modal-root opener — create-task / bulk-archive
  // close paths don't read this variable, so the harmless write is OK.
  document.body.addEventListener(
    "click",
    (evt) => {
      const opener = evt.target.closest && evt.target.closest('[hx-target="#modal-root"]');
      if (!opener) return;
      if (evt.ctrlKey || evt.metaKey || evt.shiftKey || evt.button !== 0) return;
      window._actaModalReturnTo = window.location.href;
    },
    true,
  );

  // Themed tooltips — convert every ``title="…"`` to ``data-tooltip``
  // (+ ``aria-label`` if unset) so the CSS rule in ``main.css`` renders
  // a card-coloured pop on hover instead of the OS's default black /
  // yellow chrome. Run on initial load and after every HTMX swap so
  // fragments fetched on demand (modal body, cell partials, comments)
  // pick it up too. Idempotent — skips elements already converted.
  function themeTooltips(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll("[title]:not([data-tooltip])").forEach((el) => {
      const t = el.getAttribute("title");
      if (!t) return;
      el.setAttribute("data-tooltip", t);
      if (!el.getAttribute("aria-label")) el.setAttribute("aria-label", t);
      el.removeAttribute("title");
    });
  }
  themeTooltips(document.body);
  document.body.addEventListener("htmx:afterSwap", () => themeTooltips(document.body));
  // Alpine x-teleport / x-show toggles can insert nodes outside the
  // HTMX swap path. Cheap belt: re-scan whenever Alpine processes a
  // tree (event fires after each Alpine ``x-init`` / DOM change).
  document.addEventListener("alpine:initialized", () => themeTooltips(document.body));

  // ----- @-mention hover cards ------------------------------------
  // A user-mention chip (``.acta-mention[data-user-id]``) shows a small
  // card with avatar + full name on hover. The chip itself only carries
  // the id (the markdown render is context-free), so the card is fetched
  // from the page's ``mention-search`` endpoint (``?id=``) and cached.
  const MENTION_CARD_CACHE = {};
  let mentionCardEl = null;
  function hideMentionCard() {
    if (mentionCardEl) {
      mentionCardEl.remove();
      mentionCardEl = null;
    }
  }
  function placeMentionCard(chip, user) {
    hideMentionCard();
    const rect = chip.getBoundingClientRect();
    const initial = (user.name || user.username || "?").slice(0, 1).toUpperCase();
    mentionCardEl = document.createElement("div");
    mentionCardEl.className = "acta-mention-card";
    mentionCardEl.innerHTML =
      `<span class="av" style="background:${user.avatar_color || "#3f3f46"}">${initial}</span>` +
      `<span><span class="nm">${user.name || user.username}</span><br>` +
      `<span class="un">@${user.username}</span></span>`;
    document.body.appendChild(mentionCardEl);
    mentionCardEl.style.top = rect.bottom + 6 + "px";
    mentionCardEl.style.left = rect.left + "px";
  }
  function showMentionCard(chip) {
    const id = chip.getAttribute("data-user-id");
    if (!id) return;
    if (MENTION_CARD_CACHE[id]) {
      placeMentionCard(chip, MENTION_CARD_CACHE[id]);
      return;
    }
    const ep = document.querySelector("[data-mention-url]");
    if (!ep) return;
    fetch(ep.getAttribute("data-mention-url") + "?id=" + encodeURIComponent(id), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && d.user) {
          MENTION_CARD_CACHE[id] = d.user;
          placeMentionCard(chip, d.user);
        }
      })
      .catch(() => {});
  }
  document.addEventListener("mouseover", (e) => {
    const chip = e.target.closest && e.target.closest(".acta-mention[data-user-id]");
    if (chip) showMentionCard(chip);
  });
  document.addEventListener("mouseout", (e) => {
    const chip = e.target.closest && e.target.closest(".acta-mention[data-user-id]");
    if (chip) hideMentionCard();
  });

  // ----- Task-mention hover cards ---------------------------------
  // A task chip shows a richer popover (status / priority / assignee /
  // due / labels) fetched from ``mention-search?task_id=`` and cached.
  const TASK_STATUS_COLOR = {
    planned: "#71717a",
    "to-do": "#3b82f6",
    "in-progress": "#8b5cf6",
    "in-review": "#f59e0b",
    done: "#10b981",
  };
  const TASK_PRIORITY_COLOR = { 1: "#f43f5e", 2: "#fb923c", 3: "#fbbf24", 4: "#38bdf8", 5: "#71717a" };
  const TASK_CARD_CACHE = {};
  let taskCardEl = null;
  function hideTaskCard() {
    if (taskCardEl) {
      taskCardEl.remove();
      taskCardEl = null;
    }
  }
  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function placeTaskCard(chip, t) {
    hideTaskCard();
    const rect = chip.getBoundingClientRect();
    const sColor = TASK_STATUS_COLOR[t.status] || "#71717a";
    const pColor = TASK_PRIORITY_COLOR[t.priority] || "#71717a";
    let html =
      `<div class="t-meta">` +
      `<span class="t-meta-left">` +
      `<span class="t-chip"><span class="t-dot" style="background:${sColor}"></span>${esc(t.status_label)}</span>` +
      `<span class="t-chip" style="color:${pColor}">${esc(t.priority_label)}</span>` +
      `</span>` +
      `<span class="t-due">${t.due_date ? esc(t.due_date) : "—"}</span>` +
      `</div>`;
    if (t.assignee) {
      html +=
        `<div class="t-line"><span class="t-av" style="background:${t.assignee.avatar_color}">` +
        `${esc(t.assignee.initial)}</span>${esc(t.assignee.name)}</div>`;
    } else {
      html += '<div class="t-line t-muted">Unassigned</div>';
    }
    if (t.labels && t.labels.length) {
      html +=
        '<div class="t-labels">' +
        t.labels
          .map(
            (l) =>
              `<span class="acta-label-pill" style="--label-color:${esc(l.color)}">` +
              `<span class="acta-label-pill-dot" style="background-color:${esc(l.color)}"></span>${esc(l.name)}</span>`,
          )
          .join("") +
        "</div>";
    }
    taskCardEl = document.createElement("div");
    taskCardEl.className = "acta-task-card";
    taskCardEl.innerHTML = html;
    document.body.appendChild(taskCardEl);
    const ch = taskCardEl.offsetHeight;
    const below = window.innerHeight - rect.bottom - 8;
    const top = below < ch && rect.top > below ? rect.top - ch - 6 : rect.bottom + 6;
    let left = rect.left;
    const cw = taskCardEl.offsetWidth;
    if (left + cw > window.innerWidth - 8) left = window.innerWidth - cw - 8;
    taskCardEl.style.top = top + "px";
    taskCardEl.style.left = Math.max(8, left) + "px";
  }
  function showTaskCard(chip) {
    const id = chip.getAttribute("data-task-id");
    if (!id) return;
    if (TASK_CARD_CACHE[id]) {
      placeTaskCard(chip, TASK_CARD_CACHE[id]);
      return;
    }
    const ep = document.querySelector("[data-mention-url]");
    if (!ep) return;
    fetch(ep.getAttribute("data-mention-url") + "?task_id=" + encodeURIComponent(id), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && d.task) {
          TASK_CARD_CACHE[id] = d.task;
          placeTaskCard(chip, d.task);
        }
      })
      .catch(() => {});
  }
  document.addEventListener("mouseover", (e) => {
    const chip = e.target.closest && e.target.closest(".acta-task-mention[data-task-id]");
    if (chip) showTaskCard(chip);
  });
  document.addEventListener("mouseout", (e) => {
    const chip = e.target.closest && e.target.closest(".acta-task-mention[data-task-id]");
    if (chip) hideTaskCard();
  });

  // Dismiss any open hover card on navigation / scroll / Escape. A
  // ``mouseout`` doesn't always fire when the hovered chip is covered or
  // removed (e.g. opening the task modal), which otherwise leaves the
  // card stranded on top of the new view.
  function hideAllHoverCards() {
    hideMentionCard();
    hideTaskCard();
  }
  document.body.addEventListener("htmx:beforeSwap", hideAllHoverCards);
  document.addEventListener("scroll", hideAllHoverCards, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideAllHoverCards();
  });

  // ----- Comment deep-link highlight ------------------------------
  // Scroll to a comment and pulse a brand ring so the user spots it.
  // Used both by a ``#comment-<id>`` hash (full-page deep link) and by
  // the My Activity "added a comment" row which opens the task modal.
  function highlightCommentById(id) {
    const el = document.getElementById("comment-" + id);
    if (!el) return false;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("acta-comment-highlight");
    setTimeout(() => el.classList.remove("acta-comment-highlight"), 2500);
    return true;
  }
  function highlightHashComment() {
    const h = window.location.hash;
    if (!h || !/^#comment-\d+$/.test(h)) return;
    highlightCommentById(h.slice(9));
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", highlightHashComment);
  } else {
    highlightHashComment();
  }
  document.body.addEventListener("htmx:afterSettle", highlightHashComment);
  window.addEventListener("hashchange", highlightHashComment);

  // "added a comment" (My Activity) → open the task in the modal, then
  // scroll to + highlight that comment inside it. ``htmx.ajax`` resolves
  // after the swap settles, so we highlight in the promise callback.
  // Modifier / middle clicks fall through to the native deep link.
  document.addEventListener("click", (e) => {
    const link = e.target.closest && e.target.closest("a.acta-comment-link");
    if (!link) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    const taskUrl = link.getAttribute("data-task-url");
    const commentId = link.getAttribute("data-comment-id");
    if (!taskUrl || !window.htmx) return;
    e.preventDefault();
    window._actaModalReturnTo = window.location.href;
    window.htmx
      .ajax("GET", taskUrl + "?modal=1", { target: "#modal-root", swap: "innerHTML" })
      .then(() => {
        if (window.history && window.history.pushState) window.history.pushState({}, "", taskUrl);
        setTimeout(() => highlightCommentById(commentId), 60);
      });
  });

  // Task-mention chip → open the task in the modal instead of a full
  // page nav. The chip is rendered server-side through bleach (which
  // strips ``hx-*``), so we intercept the click here and drive the same
  // ``?modal=1`` → ``#modal-root`` flow the kanban cards use. Modifier /
  // middle clicks fall through to the native link (open in new tab).
  document.addEventListener("click", (e) => {
    const chip = e.target.closest && e.target.closest("a.acta-task-mention");
    if (!chip) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    const href = chip.getAttribute("href");
    if (!href || href === "#" || !window.htmx) return;
    e.preventDefault();
    window._actaModalReturnTo = window.location.href;
    window.htmx.ajax("GET", href + "?modal=1", { target: "#modal-root", swap: "innerHTML" });
    if (window.history && window.history.pushState) {
      window.history.pushState({}, "", href);
    }
  });

  // Shared Alpine store for the filter sidebar's open / collapsed state.
  // Drives both the sidebar itself (collapsed button vs full form) and
  // the page-content wrapper that needs to reserve right padding for the
  // floating sidebar at ``lg+`` widths. Persisted in localStorage so it
  // survives page navigations.
  // Toast helper exposed before Alpine boots — used by the global
   // HTMX error listeners (which fire on the very first request, may
   // be before ``alpine:init``). The store registration below adds
   // the proper Alpine-reactive queue; this fallback keeps the call
   // sites simple (``window.actaToast(...)`` always works).
  const pendingToasts = [];
  window.actaToast = function actaToast(message, level = "error", timeoutMs = 5000) {
    if (window.Alpine && window.Alpine.store("toasts")) {
      window.Alpine.store("toasts").push(message, level, timeoutMs);
    } else {
      pendingToasts.push({ message, level, timeoutMs });
    }
  };

  // Global HTMX error surfacing. ``htmx:responseError`` fires when the
  // server returns 4xx/5xx; ``htmx:sendError`` fires on network /
  // connection failures (offline, DNS, TLS errors). Both used to drop
  // silently and leave the user staring at stale DOM — toasts make
  // the failure visible. Body of the response is preferred for the
  // message because Django HttpResponseBadRequest carries a short
  // human-readable string in ``responseText``.
  document.body.addEventListener("htmx:responseError", (evt) => {
    const xhr = evt.detail && evt.detail.xhr;
    if (!xhr) return;
    let msg = `Request failed (${xhr.status})`;
    const body = (xhr.responseText || "").trim();
    if (body) {
      try {
        const data = JSON.parse(body);
        if (data && (data.detail || data.message)) {
          msg = data.detail || data.message;
        } else if (body.length < 200) {
          msg = body;
        }
      } catch (_) {
        if (body.length < 200) msg = body;
      }
    }
    window.actaToast(msg, "error");
  });
  document.body.addEventListener("htmx:sendError", () => {
    window.actaToast("Network error — check your connection.", "error");
  });

  // Server-side success toasts ride the ``HX-Trigger`` header on HTMX
  // responses. The view returns ``HX-Trigger: {"acta:toast": {"message":
  // "...", "level": "success"}}``; HTMX dispatches an ``acta:toast``
  // event on ``<body>`` and we pipe it through ``window.actaToast``.
  document.body.addEventListener("acta:toast", (evt) => {
    const detail = evt.detail || {};
    const message = detail.message || "";
    const level = detail.level || "success";
    if (message) window.actaToast(message, level);
  });

  document.addEventListener("alpine:init", () => {
    window.Alpine.store("toasts", {
      items: [],
      push(message, level = "error", timeoutMs = 5000) {
        const id = Date.now() + Math.random();
        this.items = [...this.items, { id, message: String(message || ""), level }];
        if (timeoutMs > 0) {
          setTimeout(() => this.dismiss(id), timeoutMs);
        }
      },
      dismiss(id) {
        this.items = this.items.filter((t) => t.id !== id);
      },
      clear() {
        this.items = [];
      },
    });
    // Drain anything queued before Alpine booted.
    while (pendingToasts.length) {
      const t = pendingToasts.shift();
      window.Alpine.store("toasts").push(t.message, t.level, t.timeoutMs);
    }

    window.Alpine.store("filters", {
      open: localStorage.getItem("filtersOpen") !== "false",
      // Mirror open / closed onto <html> so the pre-paint script in
      // base.html and the runtime class stay in sync. CSS in main.css
      // (``html.acta-filters-open`` / ``html.acta-filters-closed``)
      // drives visibility of the collapsed trigger vs the full form
      // — keeping the class on <html> survives HTMX swaps and avoids
      // the Alpine-reactivity race we hit on My Work / All Tasks nav.
      _syncHtmlClass() {
        const html = document.documentElement;
        html.classList.toggle("acta-filters-open", this.open);
        html.classList.toggle("acta-filters-closed", !this.open);
      },
      toggle() {
        this.open = !this.open;
        localStorage.setItem("filtersOpen", this.open);
        this._syncHtmlClass();
      },
      set(value) {
        this.open = !!value;
        localStorage.setItem("filtersOpen", this.open);
        this._syncHtmlClass();
      },
    });

    // Theme — three-state cycle: light → dark → midnight → light.
    // Midnight reuses the ``dark`` Tailwind variant (so ``dark:*``
    // utilities keep firing) and layers a ``midnight`` class on top
    // that overrides surface CSS vars in main.css.
    const THEMES = ["light", "dark", "midnight"];
    function currentThemeFromDom() {
      const cls = document.documentElement.classList;
      if (cls.contains("midnight")) return "midnight";
      if (cls.contains("light")) return "light";
      return "dark";
    }
    function applyTheme(theme) {
      const cls = document.documentElement.classList;
      cls.remove("light", "dark", "midnight");
      if (theme === "light") {
        cls.add("light");
      } else if (theme === "midnight") {
        cls.add("dark");
        cls.add("midnight");
      } else {
        cls.add("dark");
      }
    }
    window.Alpine.store("theme", {
      current: currentThemeFromDom(),
      toggle() {
        const idx = THEMES.indexOf(this.current);
        this.current = THEMES[(idx + 1) % THEMES.length];
        applyTheme(this.current);
        localStorage.setItem("acta:theme", this.current);
      },
    });

    // Main app sidebar (left nav). Stored as a single boolean —
    // ``acta:sidebar_open=false`` collapses the sidebar into a thin
    // re-open button in the topbar; default true.
    window.Alpine.store("sidebar", {
      open: localStorage.getItem("acta:sidebar_open") !== "false",
      toggle() {
        this.open = !this.open;
        localStorage.setItem("acta:sidebar_open", this.open);
      },
    });

    // Kanban-column collapsed/expanded state. Each entry is a status
    // key (``planned`` / ``to-do`` / …) the user has chosen to fold
    // into a narrow vertical strip. Persisted as a JSON array so the
    // preference survives navigations and project switches. First-
    // time visitors get ``planned`` folded by default — it's typically
    // a long tail of speculative tasks that distracts from active work.
    const rawCollapsed = localStorage.getItem("acta:kanban_collapsed");
    let collapsed;
    if (rawCollapsed === null) {
      collapsed = ["planned"];
    } else {
      try {
        const parsed = JSON.parse(rawCollapsed);
        collapsed = Array.isArray(parsed) ? parsed : [];
      } catch (_) {
        collapsed = [];
      }
    }
    // Current view mode (kanban / table). Read from the
    // ``acta_view_mode`` cookie which the server resets on every
    // page-level render of AllTasksView / ProjectDetailView. The
    // view toggle in ``_view_panel.html`` calls ``set(...)`` so the
    // sidebar (Status section in particular) re-evaluates without
    // waiting for a full page reload.
    const VIEW_MODES = new Set(["overview", "kanban", "table", "list", "timeline"]);
    function readViewModeCookie() {
      const m = document.cookie.match(/(?:^|;\s*)acta_view_mode=([^;]+)/);
      const value = m ? m[1] : "";
      return VIEW_MODES.has(value) ? value : "kanban";
    }
    window.Alpine.store("viewMode", {
      current: readViewModeCookie(),
      set(value) {
        if (!VIEW_MODES.has(value)) return;
        this.current = value;
        // Client-side tab toggles ``history.pushState`` instead of
        // hitting the server, so we mirror the cookie write the server
        // would have made on a full render. Without it, navigating to
        // another project after toggling here would fall back to the
        // previously-server-rendered view.
        const oneYear = 60 * 60 * 24 * 365;
        document.cookie = `acta_view_mode=${value}; path=/; max-age=${oneYear}; samesite=Lax`;
      },
      // Re-read the server-set cookie after every HTMX boost. The
      // sidebar persists across navigations, so this store survives —
      // but the cookie was reset by AllTasksView / MyWorkView (which
      // disallow ``overview``). Without this sync the store keeps the
      // pre-navigation value (e.g. ``overview`` from a project page),
      // every tab's ``x-show`` resolves false, and the user lands on
      // an empty page.
      syncFromCookie() {
        this.current = readViewModeCookie();
      },
    });
    document.body.addEventListener("htmx:afterSettle", () => {
      window.Alpine.store("viewMode").syncFromCookie();
    });

    // Cross-page task selection for bulk operations. Holds task ids
    // currently selected via the row checkboxes in the table view.
    // Reset on every full-page navigation (Alpine boots fresh) — this
    // is deliberate: the selection is a transient editing intent, not
    // a persisted preference. The action bar renders when ``size > 0``.
    window.Alpine.store("selection", {
      ids: new Set(),
      has(id) {
        return this.ids.has(id);
      },
      toggle(id) {
        if (this.ids.has(id)) this.ids.delete(id);
        else this.ids.add(id);
        this._tick();
      },
      add(id) {
        this.ids.add(id);
        this._tick();
      },
      clear() {
        this.ids.clear();
        this._tick();
      },
      get size() {
        return this.ids.size;
      },
      // Toggle every id under ``container``. If all are already selected,
      // clear them; otherwise add the missing ones. ``data-task-id``
      // attributes on rows drive the lookup.
      toggleAll(container) {
        const rows = container ? container.querySelectorAll("[data-task-id]") : [];
        if (!rows.length) return;
        const ids = [...rows].map((r) => Number(r.dataset.taskId)).filter(Number.isFinite);
        const allOn = ids.every((id) => this.ids.has(id));
        if (allOn) ids.forEach((id) => this.ids.delete(id));
        else ids.forEach((id) => this.ids.add(id));
        this._tick();
      },
      _tick() {
        // Force Alpine to re-evaluate ``size`` getters bound in views —
        // Sets are not reactive in Alpine 3, so we swap the reference.
        this.ids = new Set(this.ids);
      },
    });

    // Bulk-archive driver used by the action bar in ``base_app.html``.
    // Fires a single PATCH ``/api/v1/tasks/bulk/`` with ``archived=true``
    // for every selected id, then reloads the page so all caches (row
    // list, sidebar counters, kanban) reflect the change. The bulk
    // endpoint contract is in ``docs/decisions/0012-bulk-operations.md``.
    window.actaBulkArchive = async function actaBulkArchive() {
      const store = window.Alpine.store("selection");
      if (!store || store.size === 0) return;
      const ids = [...store.ids];
      const csrfMatch = document.cookie.match(/csrftoken=([^;]+)/);
      const csrfToken = csrfMatch ? decodeURIComponent(csrfMatch[1]) : "";
      const resp = await fetch("/api/v1/tasks/bulk/", {
        method: "PATCH",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ ids, updates: { archived: true } }),
      });
      if (resp.ok) {
        store.clear();
        // Fire a custom event the page panels listen to via
        // ``hx-trigger="acta:bulk-archived from:body"``. The listener
        // re-fetches its own fragment, so the rows disappear without
        // a full page reload (no scroll jump, no SSE reconnect).
        document.body.dispatchEvent(new CustomEvent("acta:bulk-archived", { bubbles: true }));
      } else {
        let detail = "";
        try {
          const data = await resp.json();
          detail = data.detail || JSON.stringify(data);
        } catch (_) {
          detail = resp.statusText;
        }
        window.actaToast("Bulk archive failed: " + detail, "error");
      }
    };

    window.Alpine.store("kanban", {
      collapsed: new Set(collapsed),
      isCollapsed(key) {
        return this.collapsed.has(key);
      },
      toggle(key) {
        if (this.collapsed.has(key)) {
          this.collapsed.delete(key);
        } else {
          this.collapsed.add(key);
        }
        try {
          localStorage.setItem("acta:kanban_collapsed", JSON.stringify([...this.collapsed]));
        } catch (_) {
          /* localStorage full / disabled — preference is session-only */
        }
      },
    });
  });
})();
