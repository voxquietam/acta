# ADR 0029: PWA installability

**Status:** accepted
**Date:** 2026-06-09

## Context

We wanted Acta to be installable as an app — pinned to the home screen on
mobile and to the dock / launcher on desktop, opening in its own standalone
window rather than a browser tab. The minimum a browser requires to offer
"Install" is a web app manifest, a registered service worker with a `fetch`
handler, a secure context, and icons at 192 and 512 px.

The open question was how much the service worker should cache. Acta is
server-rendered (Django templates + HTMX) with live SSE and heavily
per-user content. Aggressive offline caching (an "app shell" that serves
cached HTML) would routinely show stale pages and demands careful cache
invalidation across deploys — high effort, high risk, low value for a tool
that is useless without its live data anyway.

## Decision

**Install-only, not offline-first.** Ship the manifest + worker needed to be
installable, with a deliberately conservative caching strategy that can never
serve stale content.

**Manifest is a rendered view, not a static file.** `web:pwa_manifest`
(`/manifest.webmanifest`) renders `templates/web/pwa_manifest.json` so the
icon `src` URLs go through `{% static %}`. Production hashes static filenames
via WhiteNoise's `CompressedManifestStaticFilesStorage`, so a fixed-path
static manifest would 404 after every asset rebuild. Served as
`application/manifest+json`. Public — the unauthenticated login page links it.

**Service worker at root scope.** `web:service_worker` serves
`templates/web/service_worker.js` from `/sw.js` (not under `/static/`) so the
worker's scope covers the whole origin, with `Service-Worker-Allowed: /` and
`Cache-Control: no-cache` so an updated worker is picked up next visit.
Strategy:

- **Navigations (top-level HTML)** → network-first; a small inline offline
  page is returned only on a true network failure. Pages are therefore always
  fresh.
- **`/static/` assets** → cache-first. Filenames are content-hashed in prod,
  so a cached entry can never be stale; `CACHE` is bumped to evict on a
  breaking deploy (the `activate` step deletes non-current caches).
- **Everything else** — HTMX/XHR requests, `/api/`, `/events/` (SSE),
  `/admin/`, `/mcp/` — is passed straight through, untouched. HTMX partial
  swaps are not `navigate`-mode requests, so the worker never interferes with
  them.

**Icons.** `icon-192.png` / `icon-512.png` (purpose `any`) are rendered from
`favicon.svg`; `icon-maskable-512.png` (purpose `maskable`) is rendered from a
full-bleed `icon-maskable.svg` (no rounded corners, mark inside the 80% safe
zone) so a platform mask never reveals transparent corners. `theme_color` and
`background_color` are the dark default surface (`#09090b`, zinc-950) so the
standalone chrome and splash blend with the app's default theme.

**Registration** is an inline snippet in `base.html`, deferred to `window
load` so it never competes with the critical path; it is a no-op outside a
secure context (the browser only treats https / localhost as installable).

## Consequences

- The app is installable on Chromium (desktop + Android) and via "Add to Home
  Screen" on iOS Safari (covered by the `apple-mobile-web-app-*` meta tags).
- No offline use of live data: opening the installed app without a connection
  shows the offline fallback page. This is intentional.
- `theme_color` is static while the app has three themes (light / dark /
  midnight). The dark default is used; revisiting per-theme `theme-color` is a
  follow-up if it bothers light-theme users.
- Editing the worker or manifest behaviour is a code change (rendered views),
  not a static-asset swap.
