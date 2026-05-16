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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initStickyStacks);
  } else {
    initStickyStacks();
  }
  document.body.addEventListener("htmx:afterSwap", initStickyStacks);

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
