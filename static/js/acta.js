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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      initStickyStacks();
      initStrips();
    });
  } else {
    initStickyStacks();
    initStrips();
  }
  document.body.addEventListener("htmx:afterSwap", () => {
    initStickyStacks();
    initStrips();
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
    const refreshTitle = (taskId) => refreshIf("#title-section", "titleForTask", taskId);
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
  });
})();
