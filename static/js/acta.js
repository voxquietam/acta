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

  // Command-palette "Recents" storage. Lives here so ``acta.js`` (which
  // owns ``window.acta``) is the single source of truth — the palette
  // template's inline script just reads via ``window.acta.loadRecents``.
  // Loading order matters: acta.js is included at the *end* of base.html
  // and overwrites ``window.acta = {...}`` wholesale, so anything the
  // palette template assigned earlier (it runs in the body block above)
  // would be wiped. Define the recents helpers here and the palette
  // becomes a pure consumer.
  const RECENTS_KEY = "acta:recent-tasks";
  const RECENTS_CAP = 6;

  function loadRecents() {
    try {
      const raw = window.localStorage.getItem(RECENTS_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr.slice(0, RECENTS_CAP) : [];
    } catch (e) {
      return [];
    }
  }

  function recordRecentTask(entry) {
    if (!entry || !entry.slug || !entry.url) return;
    const remaining = loadRecents().filter((r) => r.slug !== entry.slug);
    remaining.unshift({
      slug: entry.slug,
      title: entry.title || "",
      project: entry.project || "",
      url: entry.url,
    });
    try {
      window.localStorage.setItem(RECENTS_KEY, JSON.stringify(remaining.slice(0, RECENTS_CAP)));
    } catch (e) {
      // Quota exceeded / privacy mode — recents silently skip.
    }
  }

  // ``Object.assign`` instead of wholesale assignment: any earlier inline
  // script (currently only the command palette in ``_command_palette.html``)
  // that touched ``window.acta`` before this bundle loaded keeps its
  // properties intact, and a future late-loaded peer can't accidentally
  // clobber our exports.
  window.acta = Object.assign(window.acta || {}, {
    csrfToken: () => getCookie("csrftoken"),
    updateStickyStack: null, // assigned below once defined
    loadRecents,
    recordRecentTask,

    // Quick-promote a backlog task one stage (planned → ready → to-do)
    // from any list row. Posts to the same ``set_task_status`` endpoint
    // the status cell uses, then fires ``acta:task-changed`` so the
    // page's list refetches and the task moves / leaves its section.
    promoteTask(slugPrefix, number, status) {
      // ``fetch`` (not HTMX) — the global ``htmx:responseError`` toast
      // skips this path, so surface failures manually otherwise the
      // chip click is silent and the user can't tell whether the
      // server rejected or just hasn't applied yet.
      //
      // NB: the chip lives in ``_task_row.html`` (list view only). The
      // ``acta:task-changed`` trigger below refetches the inner panel —
      // unavoidably heavy on list view because the panel renders five
      // axes × N rows. See [[project-todo-list-view-promote-chip-speed]]
      // for the SSE-driven in-place row swap that would dodge it.
      fetch(`/projects/${slugPrefix}/${number}/status/`, {
        method: "POST",
        headers: {
          "X-CSRFToken": getCookie("csrftoken"),
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: "status=" + encodeURIComponent(status),
      })
        .then((r) => {
          if (r.ok && window.htmx) {
            window.htmx.trigger(document.body, "acta:task-changed");
          } else if (!r.ok && window.actaToast) {
            window.actaToast(`Couldn't promote task (${r.status}).`, "error");
          }
        })
        .catch(() => {
          if (window.actaToast) window.actaToast("Network error — task not promoted.", "error");
        });
    },

    // Querystring for the "Export filtered tasks" button. Most filters are
    // mirrored into the URL by ``applyClientFilters``, so the export already
    // matches the view — except ``show_backlog`` (deliberately not mirrored,
    // see applyClientFilters). Append its current toggle state so the export
    // reflects whether the backlog is showing.
    exportQuery() {
      const params = new URLSearchParams(window.location.search);
      const cb = document.querySelector('input[name="show_backlog"][value="1"]');
      if (cb) {
        params.delete("show_backlog");
        params.set("show_backlog", cb.checked ? "1" : "0");
      }
      const qs = params.toString();
      return qs ? "?" + qs : "";
    },


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
  });

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
    // Guard against a stale firing: the ``htmx:afterSettle`` listener
    // schedules this on a 50ms delay, so a fast navigation can move us to
    // another page before it runs. If ``basePath`` no longer matches the
    // current URL, the slots on screen belong to a different page — skip,
    // or we'd load (e.g.) ``/tasks/?panel=timeline`` into a project's slot.
    if (basePath) {
      try {
        if (new URL(basePath, window.location.origin).pathname !== window.location.pathname) return;
      } catch (_) {
        return;
      }
    }
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
      // Clear the in-flight flag once the request settles (success OR
      // failure). On success the slot now has children so the
      // ``children.length`` guard above skips it; on failure the slot is
      // still empty and a later trigger (e.g. switching to that tab) can
      // retry instead of being blocked by a stuck flag.
      Promise.resolve(
        window.htmx.ajax("GET", base.pathname + base.search, {
          target: slot,
          swap: "innerHTML",
        }),
      ).finally(() => {
        slot.dataset.panelLoading = "false";
      });
    });
  }
  // Exposed so the view-mode switch can retrigger a lazy panel load when
  // the user lands on a tab whose slot never filled (a slow / missed
  // initial fetch left it empty).
  window.actaLoadPanels = lazyLoadPanels;
  // Run after initial paint settles, and after any HTMX swap that
  // might bring back empty slots (filter form refresh swaps the
  // whole panel wrapper). Fast-exit when the page has no slot at
  // all — most settle events (inline edits, status pills, comment
  // posts) hit pages without lazy panels, and the 50ms timer +
  // URL parse inside is wasted work otherwise.
  document.body.addEventListener("htmx:afterSettle", (evt) => {
    if (!document.querySelector("[data-panel-slot]")) return;
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

  // ---- Own the history navigation (boost + Back/Forward) ----------------
  //
  // htmx's built-in history is disabled (``historyEnabled: false`` in the
  // base.html htmx-config). Its async restore-on-miss couldn't keep up with
  // rapid Back/Forward — it dropped the last restore and left ``#app-content``
  // showing a page that didn't match the URL (Projects under ``/tasks/``). We
  // drive navigation ourselves:
  //
  //   * ``pageCache`` (keyed by URL) makes Back/Forward instant. We snapshot
  //     the page we're *leaving* synchronously, paired with the exact URL it
  //     was shown under (``lastUrl``) — so unlike htmx's debounced save there
  //     is no way to pair content with the wrong URL.
  //   * ``navToken`` keeps the fetch path (cache miss) latest-wins: an
  //     in-flight load is aborted and any late response ignored.
  //
  // hx-boost still swaps ``#app-content`` for forward clicks; we own the URL
  // push, the leave-snapshot, and the Back/Forward restore.
  let navToken = 0;
  let navAbort = null;
  let lastUrl = window.location.pathname + window.location.search;
  const pageCache = new Map();
  const PAGE_CACHE_MAX = 20;

  function currentUrl() {
    return window.location.pathname + window.location.search;
  }

  function snapshotInto(url) {
    const el = document.getElementById("app-content");
    if (!el || !url) return;
    pageCache.delete(url); // re-insert to refresh LRU order
    pageCache.set(url, el.innerHTML);
    while (pageCache.size > PAGE_CACHE_MAX) {
      pageCache.delete(pageCache.keys().next().value);
    }
  }

  // ``htmx.swap`` runs the full afterSwap/afterSettle lifecycle, so lazy
  // panels, SSE binding, icon render and active-nav all re-run on restore.
  function swapAppContent(html) {
    window.htmx.swap("#app-content", html, { swapStyle: "innerHTML" });
  }

  // Freshness guarantee: a cached snapshot is only safe to restore while the
  // underlying data hasn't changed. Any data mutation — an incoming SSE event
  // (someone else's edit, wired in ``initOneWorkspaceSse``) or our own write
  // request below — drops the whole cache, so the next Back/Forward refetches
  // instead of showing a stale snapshot. Whole-cache clear is intentional: a
  // single task can appear on many pages and we can't cheaply tell which.
  function invalidatePageCache() {
    pageCache.clear();
  }
  window.__actaInvalidatePageCache = invalidatePageCache;

  // Our own writes (PATCH/POST/DELETE via htmx) → invalidate. SSE self-events
  // are dropped for DOM updates, so this is what catches edits that touch
  // pages other than the one we're on.
  document.body.addEventListener("htmx:afterRequest", (evt) => {
    const cfg = evt.detail && evt.detail.requestConfig;
    const verb = cfg && cfg.verb;
    const ok = evt.detail && evt.detail.successful;
    if (ok && verb && verb.toLowerCase() !== "get") invalidatePageCache();
  });

  function restorePage(url, token) {
    if (navAbort) navAbort.abort(); // cancel any earlier in-flight load
    // Instant path: we cached this page when we left it.
    if (pageCache.has(url)) {
      swapAppContent(pageCache.get(url));
      lastUrl = url;
      refreshSidebarActive();
      return;
    }
    // Miss: fetch fresh; ignore the response if a newer nav supersedes us.
    navAbort = new AbortController();
    fetch(url, {
      headers: { "HX-Request": "true", "HX-History-Restore-Request": "true" },
      credentials: "same-origin",
      signal: navAbort.signal,
    })
      .then((resp) => resp.text())
      .then((html) => {
        if (token !== navToken) return; // superseded
        const doc = new DOMParser().parseFromString(html, "text/html");
        const fresh = doc.getElementById("app-content");
        if (!fresh || !window.htmx) {
          window.location.assign(url); // can't extract → hard navigate
          return;
        }
        if (doc.title) document.title = doc.title;
        swapAppContent(fresh.innerHTML);
        lastUrl = url;
        snapshotInto(url);
        refreshSidebarActive();
      })
      .catch((err) => {
        if (err && err.name === "AbortError") return; // superseded → ignore
        window.location.assign(url); // network error → hard navigate
      });
  }

  // Browser Back/Forward.
  window.addEventListener("popstate", () => {
    snapshotInto(lastUrl); // save the page we're leaving (correct URL pairing)
    restorePage(currentUrl(), ++navToken);
  });

  // Forward navigation via hx-boost: snapshot the outgoing page just before
  // htmx replaces it. A boosted nav carries a ``requestConfig`` and targets
  // ``#app-content``; our own ``htmx.swap`` (no ``requestConfig``) and
  // lazy-panel swaps (target a slot) never trip this.
  document.body.addEventListener("htmx:beforeSwap", (evt) => {
    const d = evt.detail;
    if (d && d.requestConfig && d.target && d.target.id === "app-content") {
      snapshotInto(lastUrl);
    }
  });

  // After a boosted swap settles, push the new URL ourselves (htmx history is
  // off) and update ``lastUrl``.
  document.body.addEventListener("htmx:afterSettle", (evt) => {
    const cfg = evt.detail && evt.detail.requestConfig;
    const target = evt.detail && evt.detail.target;
    if (!cfg || !target || target.id !== "app-content") return;
    const url = cfg.path || (evt.detail.pathInfo && evt.detail.pathInfo.requestPath);
    if (!url) return;
    if (url !== currentUrl()) {
      navToken++; // forward nav supersedes any pending popstate load
      window.history.pushState({ acta: true }, "", url);
    }
    lastUrl = url;
    refreshSidebarActive();
  });

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
    // ``show_archived`` / ``show_backlog`` each carry a hidden ``0`` + the
    // checkbox ``1`` when on — "trailing 1 wins", same as the server parse.
    const showArchived = fd.getAll("show_archived").includes("1");
    const showBacklog = fd.getAll("show_backlog").includes("1");
    return {
      status: new Set(multi("status")),
      xstatus: new Set(multi("xstatus")),
      priority: new Set(multi("priority")),
      xpriority: new Set(multi("xpriority")),
      size: new Set(multi("size")),
      xsize: new Set(multi("xsize")),
      assignee: new Set(multi("assignee")),
      xassignee: new Set(multi("xassignee")),
      project: new Set(multi("project")),
      xproject: new Set(multi("xproject")),
      label: new Set(multi("label")),
      xlabel: new Set(multi("xlabel")),
      q: (fd.get("q") || "").toString().trim().toLowerCase(),
      dateField: (fd.get("date_field") || "completed").toString().trim(),
      dateAfter: (fd.get("date_after") || "").toString().trim(),
      dateBefore: (fd.get("date_before") || "").toString().trim(),
      showArchived,
      showBacklog,
    };
  }

  function rowMatches(row, state) {
    const s = row.dataset.status || "";
    // Archived — hidden unless show_archived is on.
    if (!state.showArchived && row.dataset.archived === "1") return false;
    // Backlog — planned / ready hidden unless show_backlog is on, except when
    // the status filter explicitly selects them (mirrors server _filter_backlog)
    // OR the row is on the Backlog tab, which always shows planned / ready
    // regardless of the toggle (that's the tab's whole purpose).
    if (
      !state.showBacklog &&
      (s === "planned" || s === "ready") &&
      !state.status.has(s) &&
      !row.closest('[data-panel-slot="backlog"]')
    ) {
      return false;
    }
    // Status
    if (state.status.size && !state.status.has(s)) return false;
    if (state.xstatus.size && state.xstatus.has(s)) return false;
    // Priority — DOM carries integer string.
    const p = row.dataset.priority || "0";
    if (state.priority.size && !state.priority.has(p)) return false;
    if (state.xpriority.size && state.xpriority.has(p)) return false;
    // Size — DOM carries the Fibonacci estimate, empty string for no size.
    const sz = row.dataset.size || "";
    if (state.size.size && !state.size.has(sz)) return false;
    if (state.xsize.size && state.xsize.has(sz)) return false;
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
    // Date range — applies to whichever field ``date_field`` selects.
    // Each ``data-*-at`` is a ``YYYY-MM-DD`` (empty when unset); ISO dates
    // compare correctly as strings. A set bound drops rows missing that date.
    if (state.dateAfter || state.dateBefore) {
      const attr = { created: "createdAt", updated: "updatedAt", completed: "completedAt", due: "dueAt" }[state.dateField] || "completedAt";
      const d = row.dataset[attr] || "";
      if (!d) return false;
      if (state.dateAfter && d < state.dateAfter) return false;
      if (state.dateBefore && d > state.dateBefore) return false;
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
      state.size.size +
      state.xsize.size +
      state.assignee.size +
      state.xassignee.size +
      state.project.size +
      state.xproject.size +
      state.label.size +
      state.xlabel.size +
      (state.q ? 1 : 0) +
      (state.dateAfter || state.dateBefore ? 1 : 0)
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
    // Per-section count badges (the "N" next to STATUS / PRIORITY / …).
    // Server-rendered on cold load; recompute here so they track the
    // client-side chip state. Each badge carries ``data-filter-section-
    // count="<key>"``; the count is include + exclude for that section.
    const sectionCounts = {
      status: state.status.size + state.xstatus.size,
      priority: state.priority.size + state.xpriority.size,
      project: state.project.size + state.xproject.size,
      label: state.label.size + state.xlabel.size,
      date: state.dateAfter || state.dateBefore ? 1 : 0,
    };
    document.querySelectorAll("[data-filter-section-count]").forEach((el) => {
      const n = sectionCounts[el.dataset.filterSectionCount] || 0;
      el.textContent = String(n);
      el.classList.toggle("hidden", n === 0);
    });
    // Kanban column counts reflect visible cards, not the server-side
    // total — match the same logic the drag-and-drop handler uses
    // after a successful drop.
    document.querySelectorAll(".kanban-column").forEach((c) => {
      const visible = c.querySelectorAll("[data-task-id]:not([hidden])").length;
      const counter = c.parentElement?.querySelector("[data-column-count]");
      if (counter) counter.textContent = String(visible);
    });
    // Backlog grooming sections: recompute the per-status count + hide a
    // section whose rows are all filtered out (count is server-rendered).
    document.querySelectorAll("[data-backlog-section]").forEach((sec) => {
      const visible = Array.from(sec.querySelectorAll("[data-task-id]")).filter(
        (row) => !(row.closest("li") || row).hasAttribute("hidden"),
      ).length;
      const counter = sec.querySelector("[data-backlog-count]");
      if (counter) counter.textContent = String(visible);
      sec.classList.toggle("hidden", visible === 0);
    });
    // List-view grouped sections (every axis is pre-rendered): recompute
    // each section's header count from visible rows + hide emptied ones.
    document.querySelectorAll("[data-list-section]").forEach((sec) => {
      const visible = sec.querySelectorAll("li:not([hidden])").length;
      const counter = sec.querySelector("[data-list-count]");
      if (counter) counter.textContent = String(visible);
      sec.classList.toggle("hidden", visible === 0);
    });
    // WIP warnings are computed server-side over the FULL board, so they
    // go stale (false alarms) under a client-side filter — e.g. "4 members
    // over WIP" on a column the filter emptied. Hide them while a filter
    // is active; un-hide (server-accurate again) once filters clear.
    const filtersActive = activeFilterCount(state) > 0;
    document.querySelectorAll("[data-wip-warning]").forEach((el) => {
      el.classList.toggle("hidden", filtersActive);
    });
    recomputeKanbanSubstatus();
    // Backlog off → hide the planned / ready kanban COLUMNS entirely (not just
    // their cards), so an empty column doesn't linger. The column wrapper
    // carries ``data-kanban-column``.
    document.querySelectorAll("[data-kanban-column]").forEach((col) => {
      const k = col.dataset.kanbanColumn;
      if (k === "planned" || k === "ready") col.classList.toggle("hidden", !state.showBacklog);
    });
    // Persist the structural toggles so a reload / cold load restores them
    // (the server reads these cookies to render the checkboxes checked).
    const oneYear = 60 * 60 * 24 * 365;
    document.cookie = `acta_show_archived=${state.showArchived ? "1" : "0"}; path=/; max-age=${oneYear}; samesite=Lax`;
    document.cookie = `acta_show_backlog=${state.showBacklog ? "1" : "0"}; path=/; max-age=${oneYear}; samesite=Lax`;
    // Mirror URL params so refresh / share carry the same filter
    // state — Django filter view re-renders identically on cold load.
    if (window.history && window.history.replaceState) {
      const params = new URLSearchParams(window.location.search);
      // Replace filter-related keys; preserve everything else (sort,
      // view, axis).
      // NB: ``show_backlog`` is deliberately NOT mirrored to the URL. Backlog
      // is purely client-side (always server-rendered, hidden via rowMatches),
      // and the lazy ``?panel=`` fetches build their URL from the current
      // location — a mirrored ``show_backlog=0`` would make the server
      // ``_filter_backlog`` drop planned/ready from a freshly-loaded panel, so
      // toggling backlog on afterwards couldn't reveal them. The cookie
      // (written above) persists the toggle for cold loads instead.
      const keys = ["status", "xstatus", "priority", "xpriority", "assignee",
        "xassignee", "project", "xproject",
        "label", "xlabel", "q", "show_archived"];
      keys.forEach((k) => params.delete(k));
      const fd = new FormData(form);
      for (const [k, v] of fd.entries()) {
        if (!keys.includes(k)) continue;
        if (k === "show_archived") {
          // hidden ``0`` + checkbox ``1`` — keep only the trailing ``1``.
          if (v === "1") params.set(k, "1");
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

  // Kanban drag-and-drop. Lives here (not in the swapped board partial)
  // so it re-binds on every navigation: an inline <script> in content
  // restored via ``htmx.swap`` doesn't reliably re-run, which left cards
  // undraggable (text just selected) after a Back/Forward or boosted nav
  // until a full reload. ``Sortable.get`` keeps the bind idempotent.
  function handleKanbanDrop(evt) {
    const card = evt.item;
    const newStatus = evt.to.dataset.status;
    const taskId = card.dataset.taskId;
    if (!taskId || !newStatus) return;
    const rollback = () => evt.from.insertBefore(card, evt.from.children[evt.oldIndex] || null);
    // Raw ``fetch`` (not HTMX) so the global ``htmx:responseError`` toast
    // never sees this failure. Without a manual toast a silent rollback
    // reads as "the card snapped back, no idea why" — surface the
    // status code on a server reject and a generic message on a
    // network drop, then roll back the DOM in both branches.
    fetch(`/api/v1/tasks/${taskId}/`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": window.acta.csrfToken(),
        Accept: "application/json",
      },
      body: JSON.stringify({ status: newStatus }),
    })
      .then((r) => {
        if (!r.ok) {
          rollback();
          if (window.actaToast) window.actaToast(`Couldn't move card (${r.status}).`, "error");
          return;
        }
        document.querySelectorAll(".kanban-column").forEach((c) => {
          const counter = c.parentElement && c.parentElement.querySelector("[data-column-count]");
          if (counter) counter.textContent = c.querySelectorAll("[data-task-id]").length;
        });
      })
      .catch(() => {
        rollback();
        if (window.actaToast) window.actaToast("Network error — card not moved.", "error");
      });
  }

  function initKanbanDnD() {
    if (!window.Sortable) return;
    document.querySelectorAll(".kanban-column").forEach((col) => {
      if (window.Sortable.get(col)) return; // already bound on this element
      new window.Sortable(col, {
        group: "tasks",
        animation: 150,
        ghostClass: "opacity-30",
        onAdd: handleKanbanDrop,
      });
    });
  }

  // Open-in-new-tab for kanban cards (plain divs, no href): middle-click
  // or Ctrl/Cmd-click. Delegated on document so it survives board swaps.
  function kanbanCardNewTab(e) {
    const card = e.target.closest && e.target.closest("[data-kanban-card]");
    if (!card || !card.dataset.taskUrl) return;
    const middle = e.type === "auxclick" && e.button === 1;
    const modified = e.type === "click" && (e.ctrlKey || e.metaKey);
    if (!middle && !modified) return;
    e.preventDefault();
    window.open(card.dataset.taskUrl, "_blank", "noopener");
  }
  document.addEventListener("click", kanbanCardNewTab);
  document.addEventListener("auxclick", kanbanCardNewTab);
  document.body.addEventListener("htmx:afterSettle", initKanbanDnD);

  // Labels-management drag-drop: any ``<ul data-sortable-labels>`` inside
  // the settings labels card becomes a Sortable, all wired into the same
  // ``"labels"`` group so labels can cross-drop between groups. On drop we
  // collect the slice's current order and POST to ``reorder_labels``; the
  // endpoint returns 204 because the client already moved the DOM. Re-runs
  // on ``htmx:afterSettle`` so the binding survives a card-swap (every
  // CRUD endpoint refreshes the whole ``#workspace-labels`` partial).
  function initLabelsDnD() {
    if (!window.Sortable) return;
    document.querySelectorAll("[data-sortable-labels]").forEach((ul) => {
      if (window.Sortable.get(ul)) return;
      new window.Sortable(ul, {
        group: "labels",
        animation: 150,
        ghostClass: "opacity-30",
        handle: "[data-label]",
        onEnd: persistLabelOrder,
      });
    });
  }

  function persistLabelOrder(evt) {
    const ul = evt.to;
    const groupId = ul.dataset.groupId || "";
    const ids = Array.from(ul.querySelectorAll("[data-label-id]")).map((li) => li.dataset.labelId);
    const section = ul.closest("[data-labels-section]");
    if (!section) return;
    const slug = section.dataset.workspaceSlug;
    const form = new FormData();
    form.append("group", groupId);
    ids.forEach((id) => form.append("label_ids", id));
    const token = document.querySelector("[name=csrfmiddlewaretoken]");
    if (token) form.append("csrfmiddlewaretoken", token.value);
    fetch(`/workspaces/${slug}/labels/reorder/`, {
      method: "POST",
      body: form,
      credentials: "same-origin",
      headers: token ? { "X-CSRFToken": token.value } : {},
    });
  }

  document.body.addEventListener("htmx:afterSettle", initLabelsDnD);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initKanbanDnD);
    document.addEventListener("DOMContentLoaded", initLabelsDnD);
  } else {
    initKanbanDnD();
    initLabelsDnD();
  }

  // Recount the per-column ``[data-column-count]`` after a card is inserted
  // (HX-Retarget from ``_create_task_post`` appends a card but doesn't
  // touch the header counter; the same recount runs on filter-apply and
  // sortable-end already). ``acta:task-created`` fires after-settle so by
  // here the new card is already in the DOM and the count picks it up.
  function recountKanbanColumns() {
    document.querySelectorAll(".kanban-column").forEach((c) => {
      const visible = c.querySelectorAll("[data-task-id]:not([hidden])").length;
      const counter = c.parentElement?.querySelector("[data-column-count]");
      if (counter) counter.textContent = String(visible);
    });
  }
  document.body.addEventListener("acta:task-created", recountKanbanColumns);

  // Cross-view freshness: any newly created task only inserts into the
  // *active* view (kanban gets a card via HX-Retarget, table gets a row
  // the same way, list runs ``acta:list-insert-row``). The sibling
  // ``data-panel-slot`` panels already in the DOM hold stale data — they
  // don't know about the new task. Clear them so the next view-switch
  // re-fetches via ``lazyLoadPanels`` (the active slot we leave alone —
  // it just got its inline insert, no point refetching). For an SSE peer
  // who isn't on kanban, the same rule applies: their active slot stays,
  // others get invalidated.
  document.body.addEventListener("acta:task-created", () => {
    const active = window.Alpine && window.Alpine.store && window.Alpine.store("viewMode")
      ? window.Alpine.store("viewMode").current
      : null;
    document.querySelectorAll("[data-panel-slot]").forEach((slot) => {
      if (slot.dataset.panelSlot === active) return;
      slot.innerHTML = "";
      slot.dataset.panelLoading = "false";
    });
    // Trigger an immediate background refetch so panels stay warm — a
    // user who switches tabs right after a create doesn't sit on a
    // spinner. ``lazyLoadPanels`` is a no-op for slots that already have
    // children, so this is safe to call eagerly.
    if (window.actaLoadPanels) window.actaLoadPanels();
  });

  // Live-insert a freshly-created task into the list view without a panel
  // refetch. Payload from ``_task_card_insert_response`` carries the row
  // HTML once plus a ``{axis: section_key}`` map (server-computed via
  // ``group_tasks``). For each axis pre-rendered in the panel, find the
  // matching ``<section data-section-key>`` and append a fresh ``<li>``
  // to its ``<ul>`` — also bump the per-section ``[data-list-count]``.
  //
  // If a section doesn't exist yet (first task of its kind in an axis
  // that builds sections on-demand, e.g. assignee/project/cycle) we skip
  // it silently — toast already confirmed the create. Next filter change
  // or nav rebuilds the panel from scratch.
  document.body.addEventListener("acta:list-insert-row", (evt) => {
    const detail = evt.detail || {};
    const html = detail.row_html;
    const keys = detail.section_keys || {};
    if (!html) return;
    Object.entries(keys).forEach(([axis, key]) => {
      const wrap = document.querySelector(`[data-list-axis="${axis}"]`);
      if (!wrap) return;
      const section = wrap.querySelector(`section[data-section-key="${CSS.escape(key)}"]`);
      if (!section) return;
      const ul = section.querySelector("ul");
      if (!ul) return;
      const li = document.createElement("li");
      li.innerHTML = html;
      // List view has no per-section sort control — it always uses the
      // default Task ordering (``-updated_at``). A freshly-created task
      // has the freshest timestamp, so it belongs at the top of the
      // section, not the bottom.
      ul.prepend(li);
      if (window.htmx) window.htmx.process(li);
      const counter = section.querySelector("[data-list-count]");
      if (counter) {
        const visible = ul.querySelectorAll("li").length;
        counter.textContent = String(visible);
      }
    });
  });

  // ── Timeline (Gantt) ───────────────────────────────────────────────
  // Ported out of an inline <script> in ``_timeline.html``. The timeline is
  // a lazy panel (``?panel=timeline``); HTMX + idiomorph DON'T re-execute
  // inline scripts on swap/morph, so the Gantt was dead after a tab-switch
  // or a wrapper refetch — drag + bar redraw never re-bound (only a full
  // boosted nav re-ran the script). Living here it re-inits on every
  // ``htmx:afterSettle`` against the fresh DOM, exactly like
  // ``initKanbanDnD``. The per-element ``tlInit`` flag on ``#tl-gantt`` keeps
  // it idempotent: a fresh swap clears it (re-init), an unrelated swap that
  // leaves the same gantt in place skips. i18n strings can't use template
  // tags here — they ride in on ``#tl-gantt`` ``data-i18n-*`` attributes.
  function initTimeline() {
    const gantt = document.getElementById("tl-gantt");
    if (!gantt) return;
    if (gantt.dataset.tlInit === "1") return; // already bound on this element
    gantt.dataset.tlInit = "1";

    // Diagnostic marker — detect whether init re-ran after an HTMX swap.
    window.__tlRanAt = Date.now();

    // i18n labels handed over from the template (static JS can't run
    // ``{% trans %}``); fall back to English if an attribute is missing.
    const L = {
      withoutDeadline: gantt.dataset.i18nWithoutDeadline || "without deadline",
      start: gantt.dataset.i18nStart || "Start",
      end: gantt.dataset.i18nEnd || "End",
      due: gantt.dataset.i18nDue || "Due",
      overdue: gantt.dataset.i18nOverdue || "overdue",
      startAfterEnd: gantt.dataset.i18nStartAfterEnd || "start after end",
      endAfterDue: gantt.dataset.i18nEndAfterDue || "ends after deadline",
    };

    const MONTHS = ["January", "February", "March", "April", "May", "June",
      "July", "August", "September", "October", "November", "December"];
    const DAY_W = { day: 44, week: 20, month: 9 };
    const LS_KEY = "acta_timeline_zoom";
    const BAR_CLASS = {
      "planned": "tl-c-planned",
      "ready": "tl-c-ready",
      "to-do": "tl-c-todo",
      "in-progress": "tl-c-inprogress",
      "in-review": "tl-c-inreview",
      "done": "tl-c-done",
    };
    const STATUS_COLOR = {
      "planned": "rgb(82 82 91)",
      "ready": "rgb(6 182 212)",
      "to-do": "rgb(37 99 235)",
      "in-progress": "rgb(124 58 237)",
      "in-review": "rgb(217 119 6)",
      "done": "rgb(5 150 105)",
    };

    // Live CSS-var references so JS-built header cells track theme switches.
    const CSS_BORDER = "rgb(var(--border))";
    const CSS_SFGD = "rgb(var(--subtle-foreground))";
    const CSS_PFGD = "rgb(var(--placeholder-foreground))";
    const CSS_BRAND_A = "rgb(108 126 251)"; // brand-400

    const tlEsc = (s) =>
      s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

    const parseDate = (s) => (s ? new Date(s + "T00:00:00") : null);
    const addDays = (d, n) => { const r = new Date(d); r.setDate(r.getDate() + n); return r; };
    const diffDays = (a, b) => Math.round((b - a) / 86400000);
    const toISO = (d) => d.toISOString().slice(0, 10);
    const fmtDate = (d) => (d ? `${MONTHS[d.getMonth()].slice(0, 3)} ${d.getDate()}` : "—");

    function weekNum(date) {
      const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
      const dn = d.getUTCDay() || 7;
      d.setUTCDate(d.getUTCDate() + 4 - dn);
      const y0 = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
      return Math.ceil((((d - y0) / 86400000) + 1) / 7);
    }

    const chartStart = parseDate(gantt.dataset.chartStart);
    const chartEnd = parseDate(gantt.dataset.chartEnd);
    const today = parseDate(gantt.dataset.today);
    const totalDays = diffDays(chartStart, chartEnd);

    let zoom = localStorage.getItem(LS_KEY) || "week";

    function setZoom(z) {
      zoom = z;
      localStorage.setItem(LS_KEY, z);
      document.querySelectorAll(".tl-zoom-btn").forEach((btn) => {
        const on = btn.dataset.zoom === z;
        btn.classList.toggle("bg-brand-500/15", on);
        btn.classList.toggle("text-brand-300", on);
        btn.classList.toggle("text-subtle-foreground", !on);
      });
      render();
    }

    document.querySelectorAll(".tl-zoom-btn").forEach((btn) =>
      btn.addEventListener("click", () => setZoom(btn.dataset.zoom)),
    );

    function renderHeader(dayW) {
      const monthsEl = document.getElementById("tl-months");
      const unitsEl = document.getElementById("tl-units");
      monthsEl.innerHTML = "";
      unitsEl.innerHTML = "";

      const renderDays = zoom === "week" ? Math.ceil(totalDays / 7) * 7 : totalDays;

      let curMonth = -1, mEl = null;
      for (let i = 0; i < renderDays; i++) {
        const d = addDays(chartStart, i);
        if (d.getMonth() !== curMonth) {
          curMonth = d.getMonth();
          mEl = document.createElement("div");
          mEl.style.cssText = "flex-shrink:0;display:flex;align-items:center;padding:0 8px;" +
            "font-size:10px;font-family:ui-monospace,monospace;font-weight:700;" +
            `text-transform:uppercase;letter-spacing:.06em;color:${CSS_SFGD};` +
            `border-right:1px solid ${CSS_BORDER};overflow:hidden;width:0px;`;
          mEl.textContent = `${MONTHS[curMonth]} ${d.getFullYear()}`;
          monthsEl.appendChild(mEl);
        }
        mEl.style.width = (parseFloat(mEl.style.width) + dayW) + "px";
      }

      if (zoom === "week") {
        for (let i = 0; i < renderDays; i += 7) {
          const d = addDays(chartStart, i);
          const now = diffDays(chartStart, today) >= i && diffDays(chartStart, today) < i + 7;
          const el = document.createElement("div");
          el.style.cssText = "flex-shrink:0;display:flex;align-items:center;justify-content:center;" +
            `font-size:10px;font-family:ui-monospace,monospace;font-weight:${now ? "700" : "500"};` +
            `color:${now ? CSS_BRAND_A : CSS_SFGD};` +
            `border-right:1px solid ${CSS_BORDER};width:${7 * dayW}px;`;
          el.textContent = `W${weekNum(d)}`;
          unitsEl.appendChild(el);
        }
      } else if (zoom === "month") {
        let prevM = -1, el = null;
        for (let i = 0; i < totalDays; i++) {
          const d = addDays(chartStart, i);
          if (d.getMonth() !== prevM) {
            prevM = d.getMonth();
            const now = d.getMonth() === today.getMonth() && d.getFullYear() === today.getFullYear();
            el = document.createElement("div");
            el.style.cssText = "flex-shrink:0;display:flex;align-items:center;justify-content:center;" +
              `font-size:10px;font-family:ui-monospace,monospace;font-weight:${now ? "700" : "500"};` +
              `color:${now ? CSS_BRAND_A : CSS_SFGD};` +
              `border-right:1px solid ${CSS_BORDER};width:0px;`;
            el.textContent = MONTHS[d.getMonth()].slice(0, 3);
            unitsEl.appendChild(el);
          }
          el.style.width = (parseFloat(el.style.width) + dayW) + "px";
        }
      } else {
        for (let i = 0; i < totalDays; i++) {
          const d = addDays(chartStart, i);
          const now = diffDays(chartStart, today) === i;
          const we = d.getDay() === 0 || d.getDay() === 6;
          const el = document.createElement("div");
          el.style.cssText = "flex-shrink:0;display:flex;align-items:center;justify-content:center;" +
            `font-size:10px;font-family:ui-monospace,monospace;font-weight:${now ? "700" : "500"};` +
            `color:${now ? CSS_BRAND_A : we ? CSS_PFGD : CSS_SFGD};` +
            `background:${we ? "rgb(var(--muted) / .3)" : "transparent"};` +
            `border-right:1px solid ${CSS_BORDER};width:${dayW}px;`;
          el.textContent = d.getDate();
          unitsEl.appendChild(el);
        }
      }

      const fullW = (renderDays * dayW) + "px";
      gantt.style.minWidth = fullW;
      document.getElementById("tl-body").style.minWidth = fullW;
    }

    function barClass(status, overdue) {
      return overdue ? "tl-c-overdue" : (BAR_CLASS[status] || "tl-c-planned");
    }

    function renderBars(dayW) {
      document.querySelectorAll(".tl-row").forEach((row) => {
        row.querySelectorAll(".tl-gwrap,.tl-nodate,.tl-deadline").forEach((el) => el.remove());

        const start = parseDate(row.dataset.start);
        const end = parseDate(row.dataset.end);
        const due = parseDate(row.dataset.due);
        const status = row.dataset.status;
        // "overdue" tracks the DEADLINE (due_date), not the bar: not done and
        // the hard deadline has passed.
        const overdue = due && due < today && status !== "done";
        const title = row.dataset.title || "";

        // ── the work bar (start → end), DISPLAY-ONLY ──────────────────
        // start/end are automatic (set on in-progress / done); the bar just
        // shows the actual span. Nothing about the bar is draggable — the only
        // draggable thing on the timeline is the deadline (below). Corrections
        // to start/end go through the rail / modal date cells.
        if (start && end) {
          // start after end → a data error: render across the real span,
          // semi-transparent + rose-dashed (``tl-invalid``). Fixed in the modal.
          const inverted = start > end;
          const lo = inverted ? end : start;
          const hi = inverted ? start : end;
          const left = diffDays(chartStart, lo) * dayW;
          const width = Math.max((diffDays(lo, hi) + 1) * dayW, dayW * 2);
          const cls = inverted
            ? barClass(status, false) + " tl-invalid"
            : barClass(status, overdue) + (overdue ? " tl-overdue" : "");

          const wrap = document.createElement("div");
          wrap.className = "tl-gwrap";
          wrap.style.left = left + "px";
          wrap.style.width = width + "px";
          wrap.innerHTML = `<div class="tl-gbar ${cls}"><span class="tl-label">${tlEsc(title)}</span></div>`;
          wrap.addEventListener("mouseenter", (e) => showTip(e, row));
          wrap.addEventListener("mousemove", moveTip);
          wrap.addEventListener("mouseleave", hideTip);
          wrap.addEventListener("click", (e) => openModal(e, row));
          row.appendChild(wrap);
        } else if (start || end) {
          // Open-ended: only one of start / end is set (e.g. in-progress with
          // a start but not yet done). Solid edge on the known date, fading
          // toward the unknown side. Display-only.
          const color = STATUS_COLOR[status] || STATUS_COLOR["planned"];
          const fadeW = Math.max(dayW * 6, 96);
          const wrap = document.createElement("div");
          wrap.className = "tl-gwrap";

          if (start) {
            const left = diffDays(chartStart, start) * dayW;
            wrap.style.cssText = `left:${left}px;width:${fadeW}px;`;
            wrap.innerHTML =
              `<div class="tl-fadebar" style="background:linear-gradient(to right, ${color} 0%, ${color} 45%, transparent 100%);">` +
                `<span class="tl-label">${tlEsc(title)}</span>` +
              "</div>";
          } else {
            const endX = diffDays(chartStart, end) * dayW;
            const right = endX + dayW;
            const left = Math.max(0, right - fadeW);
            wrap.style.cssText = `left:${left}px;width:${right - left}px;`;
            wrap.innerHTML =
              `<div class="tl-fadebar" style="justify-content:flex-end;background:linear-gradient(to left, ${color} 0%, ${color} 45%, transparent 100%);">` +
                `<span class="tl-label">${tlEsc(title)}</span>` +
              "</div>";
          }
          wrap.addEventListener("mouseenter", (e) => showTip(e, row));
          wrap.addEventListener("mousemove", moveTip);
          wrap.addEventListener("mouseleave", hideTip);
          wrap.addEventListener("click", (e) => openModal(e, row));
          row.appendChild(wrap);
        }

        // ── the deadline ◆ — the ONLY draggable element ──────────────
        // Solid when a deadline is set; a faint "ghost" at today when not
        // (drag it to set the deadline; a plain click opens the task). Rose
        // when breached (overdue, or the plan ends after it).
        const breached = due && (overdue || (end && end > due && status !== "done"));
        const anchorDay = due ? diffDays(chartStart, due) : diffDays(chartStart, today);
        const x = Math.max(0, anchorDay) * dayW + dayW / 2;
        const mark = document.createElement("div");
        mark.className =
          "tl-deadline" + (breached ? " tl-deadline-late" : "") + (due ? "" : " tl-deadline-ghost");
        mark.style.left = (x - 6) + "px";
        mark.innerHTML = '<span class="tl-diamond"></span>';
        mark.addEventListener("mouseenter", (e) => showTip(e, row));
        mark.addEventListener("mousemove", moveTip);
        mark.addEventListener("mouseleave", hideTip);
        attachDeadlineDrag(mark, row, dayW);
        row.appendChild(mark);
      });

      updateMissingCount();
    }

    function updateMissingCount() {
      // Flag tasks with no deadline — the one date you plan ahead here.
      let missing = 0;
      document.querySelectorAll("#tl-body .tl-row:not([hidden])").forEach((row) => {
        if (!row.dataset.due) missing++;
      });
      const badge = document.getElementById("tl-missing");
      badge.textContent = missing > 0 ? `${missing} ${L.withoutDeadline}` : "";
    }

    const tip = document.getElementById("tl-tip");
    const tipTitle = document.getElementById("tl-tip-title");
    const tipStart = document.getElementById("tl-tip-start");
    const tipEnd = document.getElementById("tl-tip-end");
    const tipDue = document.getElementById("tl-tip-due");
    const ICO_CLOCK = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>';
    const ICO_FLAG = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>';
    const ICO_CAL = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>';
    const WARN = (txt) => ` <span style="color:rgb(248 113 113);font-weight:700;"> — ${txt}</span>`;

    function showTip(e, row) {
      if (_tlDragging) return;
      const start = parseDate(row.dataset.start);
      const end = parseDate(row.dataset.end);
      const due = parseDate(row.dataset.due);
      const status = row.dataset.status;
      tipTitle.textContent = `${row.dataset.slug} — ${row.dataset.title || ""}`;
      tipStart.innerHTML = `${ICO_CLOCK} ${L.start}: ${fmtDate(start)}`;

      let endHtml = `${ICO_FLAG} ${L.end}: ${fmtDate(end)}`;
      if (start && end && start > end) endHtml += WARN(L.startAfterEnd);
      tipEnd.innerHTML = endHtml;

      let dueHtml = `${ICO_CAL} ${L.due}: ${fmtDate(due)}`;
      if (due && status !== "done") {
        if (due < today) dueHtml += WARN(`${diffDays(due, today)}d ${L.overdue}`);
        else if (end && end > due) dueHtml += WARN(L.endAfterDue);
      }
      tipDue.innerHTML = dueHtml;

      tip.classList.add("tl-tip-on");
      moveTip(e);
    }

    function openModal(e, row) {
      if (_tlDragging) return;
      const url = row.dataset.url;
      if (!url || typeof htmx === "undefined") return;
      htmx.ajax("GET", url + "?modal=1", { target: "#modal-root", swap: "innerHTML" });
    }

    function moveTip(e) {
      const x = e.clientX + 14;
      const y = e.clientY - tip.offsetHeight / 2;
      tip.style.left = Math.min(x, window.innerWidth - tip.offsetWidth - 8) + "px";
      tip.style.top = Math.max(8, Math.min(y, window.innerHeight - tip.offsetHeight - 8)) + "px";
    }

    function hideTip() {
      tip.classList.remove("tl-tip-on");
    }

    // Shared flag — suppresses the click→modal that fires after a deadline
    // drag gesture, and the hover tooltip mid-drag.
    let _tlDragging = false;

    // Drag the deadline diamond ◆ horizontally to set ``due_date`` inline.
    // A move (not a resize): the marker tracks the cursor day-by-day; on
    // release it POSTs ``set_task_due_date``. Past dates allowed. Tapping it
    // (no move) opens the task modal.
    function attachDeadlineDrag(mark, row, dayW) {
      const csrf = row.dataset.csrf;
      let dragging = false, startX = 0, origL = 0, moved = false;

      mark.addEventListener("pointerdown", (e) => {
        dragging = true;
        moved = false;
        _tlDragging = false;
        startX = e.clientX;
        origL = parseInt(mark.style.left, 10);
        mark.setPointerCapture(e.pointerId);
        mark.classList.add("tl-deadline-active");
        hideTip();
        e.preventDefault();
        e.stopPropagation();
      });

      mark.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        const dx = e.clientX - startX;
        if (Math.abs(dx) > 3) { moved = true; _tlDragging = true; }
        const snapDx = Math.round(dx / dayW) * dayW;
        mark.style.left = Math.max(0, origL + snapDx) + "px";
        const snap = document.getElementById("tl-snap");
        snap.style.display = "block";
        snap.style.left = (parseInt(mark.style.left, 10) + 6) + "px";
      });

      mark.addEventListener("pointerup", (e) => {
        if (!dragging) return;
        dragging = false;
        mark.classList.remove("tl-deadline-active");
        document.getElementById("tl-snap").style.display = "none";
        setTimeout(() => { _tlDragging = false; }, 0);
        if (!moved) {
          openModal(e, row);
          return;
        }
        // Marker centre sits at ``left + 6`` → recover the day under it.
        const centerX = parseInt(mark.style.left, 10) + 6;
        const newDue = addDays(chartStart, Math.round((centerX - dayW / 2) / dayW));
        row.dataset.due = toISO(newDue);
        patchDate(row.dataset.dueUrl, { due_date: toISO(newDue) }, csrf);
        renderBars(dayW);
      });
    }

    function patchDate(url, data, csrf) {
      fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(data),
      }).catch((err) => console.error("[timeline] patch failed:", err));
    }

    function renderTodayLine(dayW) {
      const line = document.getElementById("tl-today-line");
      const snap = document.getElementById("tl-snap");
      const rows = document.querySelectorAll("#tl-body .tl-row:not([hidden])").length;
      line.style.left = (diffDays(chartStart, today) * dayW) + "px";
      line.style.height = (rows * 40) + "px";
      line.style.display = "block";
      snap.style.height = (rows * 40 + 56) + "px";
    }

    // Re-run by acta.js applyClientFilters after a client-side filter pass.
    window.__tlAfterFilter = () => {
      renderTodayLine(DAY_W[zoom]);
      updateMissingCount();
    };

    const rightCol = document.getElementById("tl-right");
    const leftBody = document.getElementById("tl-left-body");
    let syncLock = 0; // 0 none · 1 right is source · 2 left is source

    rightCol.addEventListener("scroll", () => {
      if (syncLock === 2) { syncLock = 0; return; }
      syncLock = 1;
      leftBody.scrollTop = rightCol.scrollTop;
    }, { passive: true });
    leftBody.addEventListener("scroll", () => {
      if (syncLock === 1) { syncLock = 0; return; }
      syncLock = 2;
      rightCol.scrollTop = leftBody.scrollTop;
    }, { passive: true });

    document.getElementById("tl-today-btn").addEventListener("click", () => {
      const w = DAY_W[zoom];
      rightCol.scrollTo({ left: Math.max(0, diffDays(chartStart, today) * w - rightCol.clientWidth * 0.35), behavior: "smooth" });
    });

    function render() {
      const dayW = DAY_W[zoom];
      renderHeader(dayW);
      renderBars(dayW);
      renderTodayLine(dayW);
      requestAnimationFrame(() => {
        rightCol.scrollLeft = Math.max(0, diffDays(chartStart, today) * dayW - rightCol.clientWidth * 0.4);
      });
    }

    setZoom(zoom);

    // Re-run today-line when Alpine un-hides the panel (bars/line computed
    // while ``display:none`` resolve against a 0-width box). The ``[x-show]``
    // wrapper is the persistent panel slot — it survives lazy re-fetches —
    // so disconnect any observer a prior init left on it before attaching a
    // fresh one bound to the current render closure (else observers stack
    // and the callback points at detached elements).
    const panel = rightCol.closest("[x-show]");
    if (panel) {
      if (panel._tlObs) panel._tlObs.disconnect();
      panel._tlObs = new MutationObserver(() => {
        if (panel.style.display !== "none") renderTodayLine(DAY_W[zoom]);
      });
      panel._tlObs.observe(panel, { attributes: true, attributeFilter: ["style"] });
    }
  }

  document.body.addEventListener("htmx:afterSettle", initTimeline);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTimeline);
  } else {
    initTimeline();
  }

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
              bg: av.dataset.avatarBg || av.style.backgroundColor,
              initial: av.dataset.avatarInitial || av.textContent.trim(),
              name: av.dataset.avatarName || av.getAttribute("title") || "",
              url: av.dataset.avatarUrl || "",
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
          let el;
          if (src.url) {
            el = document.createElement("img");
            el.src = src.url;
            el.className = "w-3.5 h-3.5 rounded-full object-cover" + (i > 0 ? " -ml-1" : "");
          } else {
            el = document.createElement("span");
            el.className =
              "w-3.5 h-3.5 rounded-full text-white grid place-items-center text-[8px] font-medium" +
              (i > 0 ? " -ml-1" : "");
            el.style.backgroundColor = src.bg;
            el.textContent = src.initial;
          }
          el.style.boxShadow = "0 0 0 1.5px rgb(var(--card))";
          el.setAttribute("title", src.name);
          avatarsEl.appendChild(el);
        });
      }

      // Collapse the whole substatus row when a filter leaves it with
      // nothing to show (no overdue / done-trend / avatars), so it stops
      // reserving a blank 14px band under the header.
      const hasContent = showOverdue || showDoneThisWeek || avatarSources.length > 0;
      row.classList.toggle("hidden", !hasContent);
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
  // Open the Create Task modal, pre-selecting the project the user is
  // currently inside. The slug is derived from the URL
  // (``/projects/<slug>/…`` — covers the project detail page and a full
  // task page) so there's one source of truth; the create-task GET view
  // reads ``?project=<slug>`` and marks that option ``selected`` (still
  // changeable). Pages with no project context (All Tasks, My Work,
  // Inbox, the projects list) open the picker with no prefill.
  function openCreateTaskModal() {
    const root = document.getElementById("modal-root");
    if (!root || root.innerHTML.trim() !== "") return; // a modal is already open
    const shell = document.querySelector("[data-create-task-url]");
    if (!shell || !window.htmx) return;
    let url = shell.dataset.createTaskUrl;
    const m = window.location.pathname.match(/^\/projects\/([^/]+)\//);
    if (m) url += (url.includes("?") ? "&" : "?") + "project=" + encodeURIComponent(m[1]);
    // Object form is the documented htmx 2.x signature for target+swap;
    // bare-string target works in practice but the explicit form is
    // less surprising when you read the code later.
    window.htmx.ajax("GET", url, { target: "#modal-root", swap: "innerHTML" });
  }
  document.addEventListener("keydown", function onCreateTaskHotkey(evt) {
    if (evt.key !== "c" && evt.key !== "C") return;
    if (evt.metaKey || evt.ctrlKey || evt.altKey) return;
    if (isTypingTarget(evt.target)) return;
    evt.preventDefault();
    openCreateTaskModal();
  });

  // Global command palette toggle. Works from anywhere — including
  // inputs / contenteditable — because Cmd/Ctrl+K isn't an editor
  // shortcut in any of our surfaces (TipTap doesn't bind it). Browsers
  // map ⌘+K / Ctrl+K to the URL bar in Chromium/Safari, but only when
  // the page hasn't preventDefault'd — we do, so the palette wins.
  document.addEventListener("keydown", function onPaletteHotkey(evt) {
    const isMod = evt.metaKey || evt.ctrlKey;
    if (!isMod) return;
    if (evt.key !== "k" && evt.key !== "K") return;
    evt.preventDefault();
    window.dispatchEvent(new CustomEvent("acta:palette-toggle"));
  });

  // Recents tracking: the task-detail template ships a hidden
  // ``[data-acta-track-recent]`` marker with slug / title / project /
  // url so we don't have to parse ``document.title`` or guess. Every
  // initial load and every htmx swap into ``#app-content`` re-runs the
  // scan — if the marker is present, push to localStorage via the
  // palette's exported recorder.
  function scanRecentMarker() {
    const node = document.querySelector("[data-acta-track-recent]");
    if (!node) return;
    if (!window.acta || typeof window.acta.recordRecentTask !== "function") return;
    window.acta.recordRecentTask({
      slug: node.dataset.taskSlug || "",
      title: node.dataset.taskTitle || "",
      project: node.dataset.taskProject || "",
      url: node.dataset.taskUrl || window.location.pathname,
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scanRecentMarker);
  } else {
    scanRecentMarker();
  }
  // Any swap might bring a fresh task-detail body into view. Re-scanning
  // unconditionally is cheap: the marker only exists on task-detail
  // pages, and ``recordRecentTask`` dedupes by slug — so a fragment
  // swap (status pill, comment list) just promotes the already-top
  // entry. We can't filter by ``evt.detail.target ⊂ #app-content``
  // because ``outerHTML`` swaps replace the element, leaving the
  // pre-swap target detached and the new ``#app-content`` a different
  // object — the containment check then always fails.
  document.addEventListener("htmx:afterSettle", scanRecentMarker);
  // Topbar global ``+`` button (declarative hx-get would skip the
  // project prefill, so it routes through the opener instead).
  document.addEventListener("click", function onCreateTaskClick(evt) {
    const trigger = evt.target.closest && evt.target.closest("[data-create-task-trigger]");
    if (!trigger) return;
    evt.preventDefault();
    openCreateTaskModal();
  });

  // ── Create task from selected text ────────────────────────────────
  // Open the create modal prefilled with ``text`` as the title. On a
  // task detail page (``/projects/<slug>/<n>/``) the new task auto-links
  // (related) to the current task via ``link_related`` — the server
  // wires the link. Shared by the rendered-selection bubble below and
  // the description editor's "Create task" toolbar button.
  function createTaskFromText(text) {
    const title = (text || "").replace(/\s+/g, " ").trim().slice(0, 200);
    if (!title) return;
    const root = document.getElementById("modal-root");
    const shell = document.querySelector("[data-create-task-url]");
    if (!root || root.innerHTML.trim() !== "" || !shell || !window.htmx) return;
    const params = { title };
    const m = window.location.pathname.match(/^\/projects\/([^/]+)\/(\d+)\//);
    if (m) {
      params.project = m[1];
      params.link_related = `${m[1]}-${m[2]}`;
    }
    const qs = new URLSearchParams(params).toString();
    window.htmx.ajax("GET", shell.dataset.createTaskUrl + "?" + qs, { target: "#modal-root", swap: "innerHTML" });
  }
  window.acta.createTaskFromText = createTaskFromText;

  // Floating "Create task" affordance on a text selection inside any
  // ``[data-create-from-selection]`` region (rendered comment bodies).
  (function initSelectionCreateBubble() {
    let bubble = null;
    const shell = document.querySelector("[data-create-task-url]");
    const label = (shell && shell.dataset.createTaskLabel) || "Create task";
    function hide() {
      if (bubble) bubble.style.display = "none";
    }
    function getBubble() {
      if (bubble) return bubble;
      bubble = document.createElement("button");
      bubble.type = "button";
      bubble.id = "acta-selection-create";
      bubble.textContent = label;
      bubble.className =
        "fixed z-30 items-center gap-1 px-2 py-1 rounded-md bg-card border border-border shadow-lg text-xs text-foreground hover:bg-muted";
      bubble.style.display = "none";
      // Keep the selection alive: a plain click would collapse it first.
      bubble.addEventListener("mousedown", (e) => e.preventDefault());
      bubble.addEventListener("click", () => {
        const sel = window.getSelection();
        const text = sel ? sel.toString() : "";
        hide();
        if (sel) sel.removeAllRanges();
        createTaskFromText(text);
      });
      document.body.appendChild(bubble);
      return bubble;
    }
    function maybeShow() {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.toString().trim()) return hide();
      const node = sel.anchorNode;
      const el = node && (node.nodeType === 1 ? node : node.parentElement);
      if (!el || !el.closest("[data-create-from-selection]")) return hide();
      const rect = sel.getRangeAt(0).getBoundingClientRect();
      const b = getBubble();
      b.style.display = "inline-flex";
      let top = rect.top - b.offsetHeight - 6;
      if (top < 4) top = rect.bottom + 6;
      const left = Math.max(8, Math.min(rect.left, window.innerWidth - b.offsetWidth - 8));
      b.style.top = `${top}px`;
      b.style.left = `${left}px`;
    }
    document.addEventListener("mouseup", () => window.setTimeout(maybeShow, 0));
    document.addEventListener("selectionchange", () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) hide();
    });
    document.addEventListener("scroll", hide, true);
  })();

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
  // Kanban-card selector. The card is a ``<div data-kanban-card>``
  // rendered by ``_task_card.html`` (a plain div, not an anchor, so
  // native drag doesn't race the browser's link-drag). ``data-kanban-
  // card`` deliberately excludes other ``data-task-id`` carriers — the
  // list-view ``<a data-task-id>`` rows, table ``<tr data-task-id>``,
  // and activity rows (which use ``data-activity-for-task``).
  const KANBAN_CARD = (id) => `[data-kanban-card][data-task-id="${id}"]`;

  function applyCardReplace(taskId, cardHtml) {
    if (!cardHtml) return;
    const existing = document.querySelector(KANBAN_CARD(taskId));
    if (!existing) return;
    const tmp = document.createElement("div");
    tmp.innerHTML = cardHtml.trim();
    const fresh = tmp.firstElementChild;
    if (!fresh) return;
    existing.replaceWith(fresh);
    // ``replaceWith`` swaps the DOM node — HTMX has not scanned the fresh
    // element's ``hx-get`` / ``hx-trigger`` attrs yet, so click-to-open-modal
    // is dead until something else processes the subtree. Without this the
    // card silently swallows clicks after any non-status mutation (priority,
    // assignee, due, labels, etc.) on the same-tab actor. Status changes go
    // through ``applyCardMove`` below — same fix.
    if (window.htmx) window.htmx.process(fresh);
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
    // Same as applyCardReplace: HTMX needs to scan the fresh element so
    // click-to-open-modal works on the moved card.
    if (window.htmx) window.htmx.process(fresh);
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
        // Any event means data changed somewhere — drop the page cache so a
        // Back/Forward to another page refetches instead of restoring a stale
        // snapshot. Done before the self-event filter on purpose: our own
        // edits can affect pages we're not currently looking at.
        invalidatePageCache();
        // Drop self-events to avoid double-rendering (kanban drag,
        // inline edits etc.) — *except* when the event came in via MCP
        // (Claude Desktop, Cursor, curl): those write through a different
        // client process the local tab doesn't know about, so the local
        // tab must apply the SSE swap to stay in sync. The context menu
        // also opts a task in (``actaForceApplySelfEvents``) because it
        // posts with ``hx-swap="none"`` — the HTTP response doesn't touch
        // the row, so the SSE swap is the only thing that updates it.
        if (String(data.actor_id) === meId && !data.via_mcp) {
          const tid = Number(data.target_id);
          if (window.__actaForceApplySelf && window.__actaForceApplySelf.has(tid)) {
            window.__actaForceApplySelf.delete(tid);
          } else {
            return;
          }
        }
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

    handle("task.project_changed", (d) => {
      // A move changes which project a task belongs to (and its slug).
      // Drop the now-stale card / table row immediately so a peer never
      // sees a foreign slug sitting in the old project's board, then let
      // the board panel refetch itself (``data-task-list-root`` listens
      // for ``acta:task-moved``). The refetch re-renders for whatever
      // scope this page is: the old project loses the task, the new one
      // gains it, and the cross-project All Tasks view shows the updated
      // project + slug. List view re-syncs via its own panel refetch.
      applyCardRemove(d.target_id);
      document.querySelectorAll(`tr[data-task-id="${d.target_id}"]`).forEach((el) => el.remove());
      document.body.dispatchEvent(new CustomEvent("acta:task-moved", { bubbles: true }));
      refreshListPanel();
    });

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
        invalidatePageCache();
        applyTaskUpdate(d);
      });
    });
    handle("task.deleted", (d) => {
      applyCardRemove(d.target_id);
      document.querySelectorAll(`tr[data-task-id="${d.target_id}"]`).forEach((el) => el.remove());
      refreshListPanel();
    });

    // New task from another user — server's ``task.created`` broadcast
    // includes the pre-rendered kanban card in ``broadcast_extras``
    // (see ``log_event`` + ``_create_task_post``). If the peer is on
    // the kanban view, insert the card straight into the matching
    // ``#kanban-col-<status>`` column — same target the local create
    // flow's HX-Retarget hits — and run ``htmx.process`` so click-to-
    // open wires up. ``acta:task-created`` still fires for the
    // column-count recount and any other listeners.
    //
    // Non-kanban peers (table / list / timeline) won't have a target
    // — they'll see the new task on next nav. Extending to table-row
    // / list-row inserts is a follow-up (needs view detection +
    // per-view HTML in the broadcast).
    handle("task.created", (data) => {
      const html = data && data.html_kanban;
      const status = data && data.status;
      if (html && status) {
        const col = document.getElementById("kanban-col-" + status);
        if (col) {
          col.insertAdjacentHTML("beforeend", html);
          const card = col.lastElementChild;
          if (card && window.htmx) window.htmx.process(card);
        }
      }
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
      "task.end_changed",
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
    // Comment events bypass the self-event filter on purpose. The filter
    // keys on ``actor_id`` (the USER), but the same user can have the task
    // open in two tabs: the posting tab updated its own timeline via the
    // HTTP response, while the OTHER tab only learns about the comment
    // through SSE. Dropping it as a "self" event left that second tab
    // stale. Refreshing the timeline is idempotent (a full fragment
    // reload), so re-running it on the posting tab is harmless.
    const commentEvents = ["comment.created", "comment.updated", "comment.deleted"];
    commentEvents.forEach((name) => {
      source.addEventListener(name, (e) => {
        let d;
        try {
          d = JSON.parse(e.data);
        } catch (_) {
          return;
        }
        invalidatePageCache();
        refreshTimeline(d.task_id);
      });
    });
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
    // Project updates surface only in the Updates tab, not Notifications,
    // so skip the live badge bump + row injection here. The server's
    // unread count already excludes them, so the badge HTML in this very
    // event carries the unchanged number — ignoring it keeps them in sync.
    if (d && d.kind === "project_update") return;
    // The inbox is scoped to the active workspace; a notification for a
    // workspace the user isn't currently in must not bump the badge or
    // inject a row. ``#app-content`` carries the active workspace id.
    const appEl = document.getElementById("app-content");
    const activeWs = appEl && appEl.dataset.activeWorkspace;
    if (d && d.workspace_id != null && activeWs && String(d.workspace_id) !== String(activeWs)) return;
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
      // Leave native ``title`` on anything inside a kanban card: the card
      // is ``overflow-hidden`` and sits in a scrolling column, so a CSS
      // ``::after`` pop clips whichever way it points. The OS tooltip
      // renders un-clipped. Matches the intent in ``_task_link_badges.html``.
      if (el.closest("[data-kanban-card]")) return;
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

  // ----- Image lightbox (delegated) -------------------------------
  // Open ``img`` in the shared lightbox as a gallery: the siblings in its
  // nearest gallery root (an explicit ``[data-image-gallery]`` — attachment
  // panels — else the rendered ``.prose`` block, else the parent) become
  // the prev/next set, starting at the clicked image. Exposed globally so
  // the per-thumbnail ``onclick`` handlers can reuse it.
  window.actaLightbox = function (img) {
    const root = img.closest("[data-image-gallery]") || img.closest(".prose") || img.parentElement;
    const imgs = root ? Array.from(root.querySelectorAll("img")) : [img];
    const images = imgs.map((el) => ({ src: el.currentSrc || el.src, alt: el.alt || "" }));
    let index = imgs.indexOf(img);
    if (index < 0) index = 0;
    window.dispatchEvent(new CustomEvent("lightbox:open", { detail: { images, index } }));
  };
  // Rendered markdown images (comment bodies, etc.) are plain ``<img>`` with
  // no per-element handler — bleach strips any ``onclick``. Delegate a
  // click. Skip images inside the TipTap editor (``.ProseMirror`` /
  // contenteditable — there a click edits, not previews) and any image that
  // already carries its own trigger.
  document.addEventListener("click", (evt) => {
    const img = evt.target.closest("img");
    if (!img || !img.closest(".prose")) return;
    if (img.closest(".ProseMirror, [contenteditable='true']")) return;
    if (img.hasAttribute("onclick")) return;
    evt.preventDefault();
    window.actaLightbox(img);
  });
  // Inside the TipTap editor (descriptions) a single click selects the
  // image for editing, so previewing is bound to DOUBLE-click instead —
  // that doesn't fight node selection / deletion.
  document.addEventListener("dblclick", (evt) => {
    const img = evt.target.closest(".ProseMirror img");
    if (!img) return;
    evt.preventDefault();
    window.actaLightbox(img);
  });


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
  // after the swap settles, so we highlight in the promise callback. The
  // modal is a pure overlay — we don't touch the address bar. Modifier /
  // middle clicks fall through to the native deep link.
  document.addEventListener("click", (e) => {
    const link = e.target.closest && e.target.closest("a.acta-comment-link");
    if (!link) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    const taskUrl = link.getAttribute("data-task-url");
    const commentId = link.getAttribute("data-comment-id");
    if (!taskUrl || !window.htmx) return;
    e.preventDefault();
    window.htmx
      .ajax("GET", taskUrl + "?modal=1", { target: "#modal-root", swap: "innerHTML" })
      .then(() => {
        setTimeout(() => highlightCommentById(commentId), 60);
      });
  });

  // Task-mention chip → open the task in the modal instead of a full
  // page nav. The chip is rendered server-side through bleach (which
  // strips ``hx-*``), so we intercept the click here and drive the same
  // ``?modal=1`` → ``#modal-root`` overlay flow the kanban cards use (no
  // address-bar change). Modifier / middle clicks fall through to the
  // native link (open in new tab).
  document.addEventListener("click", (e) => {
    const chip = e.target.closest && e.target.closest("a.acta-task-mention");
    if (!chip) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    const href = chip.getAttribute("href");
    if (!href || href === "#" || !window.htmx) return;
    e.preventDefault();
    window.htmx.ajax("GET", href + "?modal=1", { target: "#modal-root", swap: "innerHTML" });
  });

  // Right-click context menu on task rows / cards. One global menu lives
  // in ``#context-menu-root``; the per-task fragment is fetched on demand
  // (server-rendered with every submenu pre-populated) and positioned at
  // the cursor with viewport edge-flipping. Actions inside post to the
  // ``set_task_*`` endpoints and the menu fires ``acta:task-changed`` so
  // the board panel refetches.
  // Tasks whose next self-event the SSE handler should apply rather than
  // drop — populated by the context menu (which posts ``hx-swap="none"``,
  // so only the SSE swap updates the row). Entries auto-expire so a stale
  // id never force-applies an unrelated later edit.
  window.__actaForceApplySelf = window.__actaForceApplySelf || new Set();
  window.actaForceApplySelfEvent = function (id) {
    const n = Number(id);
    window.__actaForceApplySelf.add(n);
    // 30 s window — comfortably covers a slow activity-log fanout or a
    // queued Telegram callback that delays the SSE broadcast beyond the
    // raw HTTP round-trip. Was 4 s; observed in audit (docs/audit/05-nav-router.md
    // §4.5) as too tight under heavy peer activity, where the id would
    // expire before the SSE event landed and the row stayed stale.
    setTimeout(() => window.__actaForceApplySelf.delete(n), 30000);
  };

  (function initTaskContextMenu() {
    const root = document.getElementById("context-menu-root");
    if (!root) return;

    function closeMenu() {
      if (root.style.display === "none") return;
      root.style.display = "none";
      root.innerHTML = "";
    }

    function positionMenu(clientX, clientY) {
      const menu = root.firstElementChild;
      root.style.left = "0px";
      root.style.top = "0px";
      root.style.display = "block";
      const mw = menu ? menu.offsetWidth : 240;
      const mh = menu ? menu.offsetHeight : 360;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      // Submenus swap in place (no right cascade), so we only clamp the
      // single card to the viewport — flip left / up when it would spill.
      let left = clientX;
      if (clientX + mw > vw) left = clientX - mw;
      left = Math.max(8, Math.min(left, vw - mw - 8));
      let top = clientY;
      if (clientY + mh > vh) top = vh - mh - 8;
      top = Math.max(8, top);
      root.style.left = left + "px";
      root.style.top = top + "px";
    }

    function openMenu(url, x, y) {
      if (!url || !window.htmx) return;
      window.htmx.ajax("GET", url, { target: "#context-menu-root", swap: "innerHTML" }).then(() => positionMenu(x, y));
    }
    // Bulk bar's "Actions" button opens the same selection menu anchored
    // above it (positionMenu flips up since the bar sits at the bottom).
    window.actaOpenBulkMenu = (x, y) => openMenu("/tasks/bulk-menu/", x, y);

    document.addEventListener("contextmenu", (e) => {
      // Leave the native menu alone inside text inputs / editors.
      if (e.target.closest && e.target.closest("input, textarea, [contenteditable], .ProseMirror")) return;
      const row = e.target.closest && e.target.closest("[data-task-id][data-context-menu-url]");
      if (!row || !window.htmx) return;
      e.preventDefault();
      // Selection-aware: right-clicking a task that's part of a 2+
      // selection acts on the WHOLE selection (bulk menu); otherwise it's
      // the single-task menu. Right-clicking an unselected task never
      // touches the current selection.
      const taskId = Number(row.dataset.taskId);
      const store = window.Alpine && window.Alpine.store("selection");
      const bulk = store && store.size >= 2 && store.has(taskId);
      const url = bulk ? "/tasks/bulk-menu/" : row.getAttribute("data-context-menu-url");
      openMenu(url, e.clientX, e.clientY);
    });

    document.body.addEventListener("acta:close-context-menu", closeMenu);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeMenu();
    });
    document.addEventListener("mousedown", (e) => {
      if (root.style.display === "none") return;
      if (!root.contains(e.target)) closeMenu();
    });
    // Capture-phase scroll fires for the menu's own scrollable submenus
    // too — only close when the scroll happened OUTSIDE the menu (i.e. the
    // page scrolled under it), never when scrolling a submenu list.
    window.addEventListener(
      "scroll",
      (e) => {
        if (root.style.display === "none") return;
        if (e.target && root.contains(e.target)) return;
        closeMenu();
      },
      true,
    );
    window.addEventListener("resize", closeMenu);
    window.addEventListener("popstate", closeMenu);
  })();

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
      // Legacy open/closed flag, kept for the old sidebar layout. The
      // new v2 layout uses ``mode`` ('rail' | 'expanded') instead — the
      // two states aren't compatible so we keep both during transition.
      open: localStorage.getItem("filtersOpen") !== "false",
      // New layout state. ``mode`` persists; ``openSection`` is
      // ephemeral (popover state, drops on reload).
      mode: localStorage.getItem("acta:filters-mode") || "rail",
      openSection: null,
      openTop: 0,
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
      // --- v2 layout ---
      setMode(value) {
        this.mode = value === "expanded" ? "expanded" : "rail";
        this.openSection = null;
        localStorage.setItem("acta:filters-mode", this.mode);
      },
      toggleMode() {
        this.setMode(this.mode === "rail" ? "expanded" : "rail");
      },
      openOnly(section, ev) {
        // Click on a rail icon → pop only this section. Toggle off if
        // the same icon is clicked again. No effect in expanded mode
        // (everything is visible). When ``ev`` is passed, the popover's
        // top is aligned to the clicked button so the user doesn't have
        // to chase the cursor up to the panel header for icons that sit
        // low on the rail.
        if (this.mode !== "rail") return;
        if (this.openSection === section) {
          this.openSection = null;
          return;
        }
        this.openSection = section;
        if (ev && ev.currentTarget) {
          const btn = ev.currentTarget;
          const aside = btn.closest(".acta-flt-aside-v2");
          if (aside) {
            const btnRect = btn.getBoundingClientRect();
            const asideRect = aside.getBoundingClientRect();
            this.openTop = Math.max(0, Math.round(btnRect.top - asideRect.top));
          }
        }
      },
      closePopover() {
        this.openSection = null;
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
    const VIEW_MODES = new Set(["overview", "kanban", "table", "list", "timeline", "backlog"]);
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
        // Lazy panels (list / timeline) fill on first paint, but a slow
        // or missed initial fetch can leave the slot empty — the user
        // then switches to that tab and sees nothing. Retrigger the load
        // for any still-empty slot now that they're looking at it.
        if (window.actaLoadPanels) window.actaLoadPanels();
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

    // Bulk drivers hitting ``/api/v1/tasks/bulk/`` for every selected id.
    // Used by the floating action bar AND the bulk context menu (right-
    // click on a selected task). The endpoint contract is in
    // docs/decisions/0012-bulk-operations.md. On success we clear the
    // selection and fire ``acta:bulk-changed`` (+ legacy
    // ``acta:bulk-archived``); page panels listen via ``hx-trigger`` and
    // refetch their fragment, so the board reflects the change without a
    // full reload / SSE reconnect.
    function csrfToken() {
      const m = document.cookie.match(/csrftoken=([^;]+)/);
      return m ? decodeURIComponent(m[1]) : "";
    }
    async function bulkRequest(method, body, failLabel, opts) {
      const store = window.Alpine.store("selection");
      if (!store || store.size === 0) return false;
      const ids = [...store.ids];
      const resp = await fetch("/api/v1/tasks/bulk/", {
        method,
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify({ ids, ...body }),
      });
      if (resp.ok) {
        // Keep the selection for repeatable actions (e.g. toggling several
        // labels in a row); otherwise the operation is terminal and we
        // clear it so the bulk bar / menu dismiss.
        if (!(opts && opts.keepSelection)) store.clear();
        document.body.dispatchEvent(new CustomEvent("acta:bulk-changed", { bubbles: true }));
        document.body.dispatchEvent(new CustomEvent("acta:bulk-archived", { bubbles: true }));
        return true;
      }
      let detail = "";
      try {
        const data = await resp.json();
        detail = data.detail || JSON.stringify(data);
      } catch (_) {
        detail = resp.statusText;
      }
      window.actaToast(failLabel + ": " + detail, "error");
      return false;
    }
    // Apply a field map (e.g. ``{status: 'done'}``, ``{archived: true}``,
    // ``{labels_add: [3]}``) to every selected task. Pass
    // ``{keepSelection: true}`` to leave the selection intact (labels).
    window.actaBulkPatch = (updates, opts) => bulkRequest("PATCH", { updates }, "Bulk update failed", opts);
    // Hard-delete every selected task.
    window.actaBulkDelete = () => bulkRequest("DELETE", {}, "Bulk delete failed");
    window.actaBulkArchive = () => window.actaBulkPatch({ archived: true });

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
