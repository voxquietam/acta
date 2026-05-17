# ADR 0014: Frontend Architecture

**Status:** accepted
**Date:** 2026-05-15
**Note:** First version of this ADR specified pure vanilla JS. Updated on the same day after MVP scope was widened to include real-time kanban, dashboards, and notifications — see [0006](0006-mvp-scope.md). The new requirements changed the cost calculus for vanilla JS, and HTMX + Alpine became the better fit.

## Context

[0001](0001-stack.md) committed to HTML + Tailwind + no-build-step frontend, but left the JS approach open. The first version of this ADR picked pure vanilla JS, on the assumption that MVP interactivity would be limited to inline edits and form-style updates.

Subsequent scope expansion added:

- **Real-time kanban** — task cards move on every connected client without a page refresh.
- **Dashboards** — charts, correlations, statistics across tasks and projects.
- **Notifications** — in-app toasts when something relevant happens to the user.

These features pushed the interactivity ceiling well past what vanilla JS can carry without becoming a mess. The natural next step is either (a) a full SPA in React/Next.js or (b) a server-rendered stack augmented with HTMX, Alpine, and a charting lib. Option (b) preserves the no-build-step ethos and the Django-native mental model while supporting the new requirements.

## Decisions

### Page architecture — server-rendered, no full SPA

- Pages are rendered server-side by Django templates. URL → view → context → template → HTML.
- Top-level surfaces are real pages with real URLs:
  - `/login/`
  - `/` — workspace dashboard (overview + recent activity)
  - `/projects/`
  - `/projects/{slug_prefix}/` — project page with kanban/table tabs and project dashboard
  - `/projects/{slug_prefix}/{number}/` — task detail
  - `/activity/` — workspace-wide activity feed
  - `/members/`
  - `/settings/`
  - `/me/` — "my work" personal dashboard
- Inside a page, **HTMX** drives partial fragment updates (no full reload). The server returns small HTML snippets which HTMX swaps into the DOM.

### Interactivity stack

- **HTMX** for server-driven partial updates and SSE subscription. Loaded from CDN.
- **Alpine.js** for client-side local state (dropdowns open/closed, draft form validation, toast lifecycle). Loaded from CDN.
- **Chart.js** for dashboards and analytics. Loaded from CDN.
- **`sortable.js`** for drag-and-drop kanban (no deps; HTMX wires the drop event to a `PATCH` request).
- Pure-vanilla JS for the small handful of cross-cutting helpers (CSRF retrieval, custom event dispatch, toast factory). One file: `static/js/acta.js`.

No build step. No npm. No bundler. No node_modules. Four `<script>` tags in the base template.

### Real-time — SSE via HTMX SSE extension

- Server-Sent Events stream pushes events from Django to every connected client. See [0015](0015-real-time.md) for the full design.
- HTMX's `hx-ext="sse"` plus `sse-swap` attributes mean a kanban card auto-updates without a single line of custom JS: the server pushes new HTML for the card, HTMX swaps it.
- Toasts for personal notifications are delivered via the same stream and surfaced by an Alpine component listening to a custom event.

### Tailwind delivery — Play CDN

Unchanged from the first version of this ADR. `<script src="https://cdn.tailwindcss.com"></script>`. Tailwind config inline in base template. Switch to standalone CLI later if perf becomes a real complaint.

> **Superseded 2026-05-17** by the build-step amendment below — Tailwind now compiles to a static `main.bundle.css`.

### Markdown rendering — server-side

Unchanged from the first version. `markdown` + `bleach` on the backend. API and template context include `description_html` rendered and sanitized; templates inject it directly. Edit forms use the raw `description`.

### Theme — dark default with toggle

Unchanged from the first version. Tailwind `darkMode: 'class'`. Class on `<html>` set server-side from the user's preference. Login page sees dark. No auto system-preference matching in MVP.

### Accessibility baseline

- Semantic HTML (`<button>`, not `<div onclick>`).
- HTMX preserves focus and ARIA attributes on swaps when the target is configured correctly.
- Color contrast: rely on Tailwind palette in both themes.
- Keyboard navigation for kanban is post-MVP polish.

## Why

- **HTMX + server-rendered fragments** gives 80% of an SPA's UX with 20% of the cost. No JSON serializer roundtrips, no client-side state machine, no compiled bundle — but also no full page reloads.
- **Alpine.js** is the smallest viable "useState for HTML" library (~15 KB). It covers the few cases where pure HTMX would feel awkward (toast animations, modal open/close, draft validation feedback).
- **Chart.js** has the cleanest declarative API for the chart types we need (bar, line, doughnut) and works fine with a single `<canvas>` plus a JSON data block.
- **No build step** preserves the original "one process, Django everywhere" property and matches what Vox already runs in `ksu24.back`. The cost of `npm install`, bundler config, and a separate dev server is not worth paying for the marginal UX gain over HTMX.
- **HTMX SSE extension** turns real-time kanban into a feature with almost no JS authoring — the heaviest lifting is on the server (which has to actually emit events; see [0015](0015-real-time.md)).
- Earlier rejection of HTMX in this same ADR was tied to a narrower scope. The scope changed; the answer changes.

## Consequences

- HTMX patterns are a new mental model: the server is the source of HTML truth, not just JSON. There's a learning curve of a day or two.
- Two extra CDN dependencies (HTMX, Alpine) in addition to Tailwind. If any CDN is blocked, fallback is to self-host the JS files — one-line change in the base template.
- Real-time kanban requires a stable SSE infrastructure on the server side. ASGI deployment (e.g. Daphne or Uvicorn behind nginx/Caddy) instead of WSGI-only Gunicorn — see [0015](0015-real-time.md).
- Chart.js is loaded on every page that has a dashboard. Lazy-load via inline `<script>` only on the relevant pages.
- Drag-and-drop kanban is now in MVP scope (was out previously). `sortable.js` plus HTMX `PATCH` handler — about a day of work.
- Per-page JS modules under `static/js/pages/{page}.js` are still allowed for page-specific glue logic, but should stay small. Anything growing past ~200 lines should be re-examined: probably should be an HTMX fragment endpoint instead.

## Open Questions

- Whether to use `markdown` (older, simpler) or `markdown-it-py` (more modern). Same as before — pick at implementation time.
- Whether some screens (e.g. the kanban) eventually warrant an Alpine-driven "client state" model in addition to HTMX. Default is no — prefer HTMX server fragments. Revisit case-by-case.
- Whether to keep Chart.js or evaluate ApexCharts / lightweight-charts when dashboard design firms up. Defer; Chart.js is the safe default to start.

## Amendment (2026-05-16): single-bundle build step for the rich-text editor

The "no build step" rule above held for HTMX, Alpine, Tailwind (Play CDN), Chart.js, and sortable.js — each loads as a single `<script>` from a CDN with no peer-dep gymnastics. It did **not** hold for **TipTap** (ProseMirror-based WYSIWYG editor used for inline task description editing, see Stage 5d-2):

- TipTap is split across ~10 packages (`@tiptap/core`, `@tiptap/pm/*`, `@tiptap/starter-kit`, `@tiptap/extension-link`, `@tiptap/extension-placeholder`, `@tiptap/extension-bubble-menu`, `tippy.js`, `tiptap-markdown`) with cross-package imports.
- ESM-CDN delivery (esm.sh + importmap) loaded the modules but quietly failed on peer deps — bubble menu positioning broke (tippy.js not pulled), inline `code` mark misbehaved.

**Decision**: bundle TipTap (and only TipTap) with **esbuild**, output `static/js/description_editor.bundle.js`. Source lives under `static_src/js/`. Bundle is committed so deploy does not need Node.

- Build runs via `make build-js` inside a throwaway `node:20-alpine` container — host never installs Node.
- `package.json` + `package-lock.json` are checked in; `node_modules/` is gitignored.
- Watch mode for editor work: `make watch-js`.
- Everything else (HTMX, Alpine, Tailwind, sortable, Chart.js) stays on CDN per the original decision.

**Scope of the exception**: this bundle is *only* for editor JS that fundamentally requires bundling. Page-glue JS, Alpine snippets, and HTMX wiring stay vanilla / inline / `static/js/*.js`. If a new feature wants to add another bundle entry, push back first — almost everything is one HTMX fragment away from not needing one.

**Why this isn't a slippery slope**: the build pipeline is one esbuild command, no transpiler, no framework, no dev server. Adding a second entry costs minutes. The cost of a Node toolchain on the host is zero (Docker-wrapped). The boundary is "WYSIWYG editors and similar JS libraries that ship as a peer-dep graph" — not "any JS we want to write."

## Amendment (2026-05-17): compile Tailwind to a static bundle

The Tailwind Play CDN (`cdn.tailwindcss.com`) was the original choice for "no build step" — drop a `<script>` in the page, get Tailwind. It worked for early Stage 5 but stopped being viable:

- **First-paint flash**. The CDN script downloads, parses the DOM, JIT-generates the matching CSS, then injects it. Users on slow networks see unstyled HTML for 200-500 ms on cold load. Vox flagged this directly: "вижу как интерфейс покосоебился пока прогружается".
- **Inline config drift**. Brand palette + `darkMode: "class"` had to live as an inline `<script>` in `base.html` before the CDN script loaded — fragile, no source of truth for tooling.
- **No tree-shake of unused utilities**. The Play CDN ships *every* utility on every page; the compiled bundle is roughly 60 KB minified after content-scanning only the classes actually used.

**Decision**: compile Tailwind with the `tailwindcss` CLI inside the same Docker-wrapped node container that builds the editor bundle. Source: `static_src/css/main.css` (`@tailwind base/components/utilities` + project-specific custom rules). Output: `static/css/main.bundle.css`. `tailwind.config.js` holds the brand palette + `darkMode: "class"` + `content` paths for the extractor.

- `make build-css` rebuilds the stylesheet (also via `make build-front` alongside the JS bundle).
- `make watch-css` rebuilds on every template / CSS save during dev.
- The compiled bundle is committed so deploy doesn't need Node — same shape as the description-editor JS bundle.
- Inline `<script>` for Tailwind config and the `cdn.tailwindcss.com` `<script>` are removed from `base.html`; a single `<link rel="stylesheet" href="…/main.bundle.css">` replaces both.
- `@tailwindcss/typography` plugin handles `.prose` styles; we register it in `tailwind.config.js`.

**Why this isn't a slippery slope (again)**: same Node-in-Docker pipeline that bundles TipTap. No new tooling, no dev server. HTMX / Alpine / Chart.js / Lucide / sortable.js continue to load from CDN as before — they don't need a build step.

**Trade-offs**:

- Every template / Python file change that introduces a *new* utility class needs `make build-css` (or running `make watch-css` in another terminal) before the class shows up styled. The extractor scans `templates/**/*.html`, `apps/**/*.py`, `apps/**/*.html`, `static_src/js/**/*.js`, `static/js/**/*.js`, so dynamically composed class strings still need to appear as plain text somewhere — the same constraint as Tailwind anywhere else.
- One more committed artefact (`static/css/main.bundle.css`) churns on every CSS-relevant template change. Worth it for first-paint and a single source of truth for theme tokens.
