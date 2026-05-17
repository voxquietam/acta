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

  // Lucide icons: replace every ``<i data-lucide="...">`` placeholder
  // with the inline SVG. Idempotent — already-replaced placeholders
  // are skipped, so we can safely re-scan after HTMX swaps without
  // double-rendering.
  function renderIcons() {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderIcons);
  } else {
    renderIcons();
  }
  document.body.addEventListener("htmx:afterSwap", renderIcons);

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

  function initWorkspaceSse() {
    const root = document.querySelector("[data-workspace-sse]");
    if (!root || root.dataset.sseBound === "true") return;
    root.dataset.sseBound = "true";
    const url = root.getAttribute("data-workspace-sse");
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
        if (String(data.actor_id) === meId) return; // ignore self
        fn(data);
      });
    };

    handle("task.status_changed", (d) => applyCardMove(d.target_id, d.to, d.card_html));
    handle("task.assigned", (d) => applyCardReplace(d.target_id, d.card_html));
    handle("task.priority_changed", (d) => applyCardReplace(d.target_id, d.card_html));
    handle("task.due_changed", (d) => applyCardReplace(d.target_id, d.card_html));
    handle("task.labels_changed", (d) => applyCardReplace(d.target_id, d.card_html));
    handle("task.updated", (d) => applyCardReplace(d.target_id, d.card_html));
    handle("task.deleted", (d) => applyCardRemove(d.target_id));

    // Activity-feed live refresh on the task detail page. The
    // ``#activity-list`` element has ``hx-trigger="refresh"`` + a
    // matching ``hx-get`` to its fragment endpoint, so we just need
    // to dispatch the ``refresh`` event whenever an event mentions
    // the current task. Works for direct task targets (target_id)
    // and ``comment.*`` events (payload.task_id).
    // Refresh helpers — dispatch a ``refresh`` custom event on the
    // matching wrapper, which then runs its ``hx-get`` to fetch the
    // updated fragment. Each wrapper carries a distinct ``data-...-
    // for-task`` attribute so we only fire on events targeting the
    // task currently open on this page.
    const refreshIf = (selector, attr, taskId) => {
      const el = document.querySelector(selector);
      if (!el || String(el.dataset[attr]) !== String(taskId)) return;
      el.dispatchEvent(new CustomEvent("refresh"));
    };
    const refreshActivity = (taskId) => refreshIf("#activity-list", "activityForTask", taskId);
    const refreshMeta = (taskId) => refreshIf("#task-meta", "metaForTask", taskId);
    const refreshTitle = (taskId) => {
      refreshIf("#title-section", "titleForTask", taskId);
      refreshIf("#topbar-task-title", "titleTopbarForTask", taskId);
    };
    const refreshDescription = (taskId) => refreshIf("#description", "descriptionForTask", taskId);
    const refreshComments = (taskId) => refreshIf("#comment-list", "commentsForTask", taskId);

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
        refreshActivity(d.target_id);
        refreshMeta(d.target_id);
      }),
    );
    // ``task.updated`` is the catch-all for title / description / size
    // edits. Inspect ``payload.changes`` so we only refresh the cells
    // that actually changed rather than retemplating the whole page
    // on a stray rename.
    handle("task.updated", (d) => {
      refreshActivity(d.target_id);
      refreshMeta(d.target_id);
      const changes = d.changes || {};
      if (changes.title) refreshTitle(d.target_id);
      if (changes.description) refreshDescription(d.target_id);
    });
    const commentEvents = ["comment.created", "comment.updated", "comment.deleted"];
    commentEvents.forEach((t) =>
      handle(t, (d) => {
        refreshActivity(d.task_id);
        refreshComments(d.task_id);
      }),
    );
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initWorkspaceSse);
  } else {
    initWorkspaceSse();
  }
  document.body.addEventListener("htmx:afterSwap", initWorkspaceSse);

  // Shared Alpine store for the filter sidebar's open / collapsed state.
  // Drives both the sidebar itself (collapsed button vs full form) and
  // the page-content wrapper that needs to reserve right padding for the
  // floating sidebar at ``lg+`` widths. Persisted in localStorage so it
  // survives page navigations.
  document.addEventListener("alpine:init", () => {
    window.Alpine.store("filters", {
      open: localStorage.getItem("filtersOpen") !== "false",
      toggle() {
        this.open = !this.open;
        localStorage.setItem("filtersOpen", this.open);
      },
      set(value) {
        this.open = !!value;
        localStorage.setItem("filtersOpen", this.open);
      },
    });

    // Theme — dark / light. Initial value comes from the pre-paint
    // script in ``base.html`` which already set ``<html class>``;
    // we mirror it here so Alpine bindings (icon swap on the toggle
    // button) stay in sync with reality. Persisted in ``localStorage``
    // under ``acta:theme`` — read in the same pre-paint script on
    // every page load so there's no light/dark flash.
    window.Alpine.store("theme", {
      current: document.documentElement.classList.contains("light") ? "light" : "dark",
      toggle() {
        this.current = this.current === "dark" ? "light" : "dark";
        document.documentElement.classList.toggle("dark", this.current === "dark");
        document.documentElement.classList.toggle("light", this.current === "light");
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
    const VIEW_MODES = new Set(["overview", "kanban", "table", "list"]);
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
        // Surface the failure so the user knows nothing happened — once
        // [[project-todo-global-htmx-error-toast]] lands this can drop
        // the ``alert``.
        let detail = "";
        try {
          const data = await resp.json();
          detail = data.detail || JSON.stringify(data);
        } catch (_) {
          detail = resp.statusText;
        }
        window.alert("Bulk archive failed: " + detail);
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
