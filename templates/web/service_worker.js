// Acta service worker — install-only PWA support (ADR 0029).
//
// Deliberately conservative: this app is server-rendered with live SSE and
// per-user content, so aggressive offline caching would serve stale pages.
// The strategy here is the minimum that makes the app installable without
// risking staleness:
//   * navigations (top-level HTML)  -> network-first, offline page only on
//     a true network failure;
//   * versioned /static/ assets     -> cache-first, but ONLY where the
//     filenames are content-hashed. In production they are
//     (``CompressedManifestStaticFilesStorage``), so a cached entry can
//     never be stale. In development they are not: ``main.bundle.css``
//     and ``acta.min.js`` keep their names forever, and cache-first there
//     pins whatever version the worker saw first — a soft reload keeps
//     serving it and only a hard reload (which bypasses the worker) gets
//     the new one. ``HASHED_STATIC`` is rendered from the storage backend
//     so the strategy follows the deployment instead of an assumption.
//   * everything else (HTMX/XHR, /api/, /events/ SSE, /admin/, /mcp/) ->
//     untouched, straight to the network.
//
// Bump CACHE to invalidate the static cache across a breaking deploy. The
// activate handler drops every cache whose key is not this one, so a bump
// is also how an already-registered worker sheds entries it should never
// have stored — which is what v1 did to every unhashed dev asset.
const CACHE = "acta-static-v2";

// Rendered server-side: true when static filenames carry a content hash.
const HASHED_STATIC = {{ hashed_static|yesno:"true,false" }};

const OFFLINE_HTML = `<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offline — Acta</title>
<style>
  html,body{height:100%;margin:0}
  body{display:flex;align-items:center;justify-content:center;
    background:#09090b;color:#a1a1aa;
    font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .box{text-align:center;padding:2rem;max-width:22rem}
  .mark{width:48px;height:48px;border-radius:11px;background:#2e44d6;margin:0 auto 1rem}
  h1{color:#e4e4e7;font-size:1rem;margin:0 0 .5rem}
  button{margin-top:1.25rem;background:#2e44d6;color:#fff;border:0;border-radius:8px;
    padding:.55rem 1.1rem;font-size:.875rem;cursor:pointer}
</style></head><body>
<div class="box">
  <div class="mark"></div>
  <h1>You're offline</h1>
  <p>Acta needs a connection to load this page. Check your network and try again.</p>
  <button onclick="location.reload()">Retry</button>
</div></body></html>`;

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Real-time + API + admin + MCP must always hit the network untouched.
  if (
    url.pathname.startsWith("/events/") ||
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/mcp/") ||
    url.pathname.startsWith("/admin/")
  ) {
    return;
  }

  // Content-hashed static assets — cache-first, populate on first hit.
  // Unhashed ones go to the network: a stale stylesheet or bundle is far
  // worse than a cache miss, and in development that is every edit.
  if (HASHED_STATIC && url.pathname.startsWith("/static/")) {
    event.respondWith(
      (async () => {
        const cached = await caches.match(req);
        if (cached) return cached;
        try {
          const res = await fetch(req);
          if (res && res.ok) {
            const cache = await caches.open(CACHE);
            cache.put(req, res.clone());
          }
          return res;
        } catch (err) {
          return cached || Response.error();
        }
      })(),
    );
    return;
  }

  // Top-level navigations — network-first, offline fallback on failure only.
  if (req.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          return await fetch(req);
        } catch (err) {
          return new Response(OFFLINE_HTML, {
            status: 503,
            headers: { "Content-Type": "text/html; charset=utf-8" },
          });
        }
      })(),
    );
  }
});
