# ADR 0024: Client-Owned History Navigation

**Status:** accepted
**Date:** 2026-05-21

## Context

Top-level navigation (sidebar + topbar links) uses `hx-boost` so only
`#app-content` swaps and the sidebar stays mounted — see
[0014](0014-frontend-architecture.md). The sidebar's active-link
highlight is JS-driven off `location.pathname` (`refreshSidebarActive`
in `static/js/acta.js`) because the sidebar element survives the swap and
Django's template-time active branch can't repaint it.

This worked for forward clicks but Back/Forward was unreliable. With
htmx's built-in history we hit, in sequence:

- **Large pages overflow the snapshot cache.** All Tasks is ≈260 KB; its
  body snapshot didn't fit, so restores missed the cache.
- **`refreshOnHistoryMiss: true` → full reload on every miss.** Correct
  but slow — every Back/Forward reloaded all CSS/JS/assets.
- **Async restore-on-miss can't keep up with rapid Back/Forward.**
  Instrumenting `htmx:historyRestore` / `historyCacheMiss` / `popstate`
  showed popstate events outrunning the async server load: htmx dropped
  the last restore and `#app-content` was left showing a page that didn't
  match the URL (Projects under `/tasks/`). The root cause is **out-of-order
  responses to the same swap target**, not a cache-config knob.
- **Stale lazy-panel loads.** `lazyLoadPanels` schedules `?panel=…`
  fetches on a 50 ms delay; a fast navigation fired them after we'd left,
  loading e.g. `/tasks/?panel=timeline` into a different page's slot.

This supersedes the "browser back/forward works for free" line in
[0019](0019-filters.md): with boosted partial swaps it does not.

## Decisions

### htmx history is disabled; the client owns navigation

`base.html` sets `htmx-config` `historyEnabled: false`. htmx no longer
snapshots, pushes, or restores. `hx-boost` still swaps `#app-content` for
forward clicks; everything history-related is driven from a small router
in `static/js/acta.js` ("Own the history navigation").

### Latest navigation always wins

A monotonic `navToken` guards every navigation. Forward clicks bump it;
each popstate bumps it. The cache-miss fetch path checks the token before
swapping and aborts the previous in-flight request (`AbortController`), so
a slow earlier response can never overwrite a newer page. This is the
direct fix for the out-of-order race.

### A client-owned page cache makes Back/Forward instant

`pageCache` (a `Map`, LRU-capped at 20) stores `#app-content` innerHTML
keyed by URL. We snapshot the page we are **leaving** synchronously,
paired with the exact URL it was shown under (`lastUrl`, which we update
only after a confirmed swap). Because the pairing is explicit and
synchronous — not htmx's debounced "save current body under
location-at-fire-time" — content can never be filed under the wrong URL.

- Forward leave: snapshot on `htmx:beforeSwap` (target `#app-content`,
  has a `requestConfig`).
- Back/Forward leave: snapshot on `popstate` before restoring.
- Restore: cache hit → instant `htmx.swap` from memory; miss →
  token-guarded `fetch` (with `HX-History-Restore-Request`, so
  `_is_htmx_partial` returns the full page), then cache it.

`htmx.swap()` runs the full afterSwap/afterSettle lifecycle, so lazy
panels, SSE binding, icon render and active-nav re-run on restore.

### Forward URL push is manual

With htmx history off, `hx-push-url` no longer fires. On a boosted swap
that settles into `#app-content` we `history.pushState` the request path
ourselves and update `lastUrl`. (Our own `htmx.swap` carries no
`requestConfig`, and lazy-panel swaps target a slot, so neither is
mistaken for a page navigation.)

### Freshness guarantee: invalidate the cache on any data change

A cached snapshot is only safe while the underlying data hasn't changed —
SSE only updates the *mounted* DOM, not a cached (unmounted) page. So we
clear the whole `pageCache` on any mutation signal:

- **Incoming SSE event** (someone else, or MCP, or our own echo) — wired
  into the `handle()` dispatcher and the link-event listeners in
  `initOneWorkspaceSse`, before the self-event filter so our own edits to
  *other* pages count too.
- **Our own write** — any non-GET htmx request that succeeds
  (`htmx:afterRequest`).

Whole-cache clear is intentional: a task appears on many pages and we
can't cheaply know which snapshot it touched. The result: a Back/Forward
restore never shows data older than the last change. Between changes,
navigation is instant from cache; after a change, the next visit refetches.

### `lazyLoadPanels` guards against stale firings

Before loading, it compares its captured `basePath` against the current
`location.pathname` and bails if they differ — the page moved on during
the 50 ms delay, so its slots belong to another page.

## Consequences

- **Correct + fast.** Forward nav is the usual boost swap; Back/Forward is
  instant from the page cache and provably matches the URL; the only
  network cost is the first visit to a page and the first revisit after a
  data change.
- **The task-detail modal is a pure overlay — no URL.** `historyEnabled:
  false` kills `hx-push-url`, so `open_task_modal_attrs` dropped it. We do
  *not* re-add a URL by other means: opening a task in the modal swaps into
  `#modal-root` and leaves the address bar on the page behind it; closing
  just clears `#modal-root`. A shareable task URL comes from the modal's
  expand button (full page) or opening the card in a new tab
  (Ctrl / middle-click), not from the modal. This kept the modal fully
  decoupled from the history router and retired the `_actaModalReturnTo`
  bookkeeping the old `replaceState`-on-close needed.
- **Cache is per-tab and in-memory.** Cleared on a full reload; that's
  fine — a full reload re-renders from the server anyway.
- **Coarse invalidation.** A busy workspace with constant SSE traffic will
  clear the cache often and fall back to fetch-per-nav. Acceptable at our
  scale (self-hosted, ~20 users); revisit with per-URL invalidation if it
  ever bites.
