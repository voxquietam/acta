# ADR 0014: Frontend Architecture

**Status:** accepted
**Date:** 2026-05-15

## Context

[0001](0001-stack.md) committed to HTML + Tailwind + vanilla JS with no build step, mirroring the `ksu24.back` setup. This ADR pins down the open details: page architecture, Tailwind delivery, markdown rendering, theming.

## Decisions

### Page architecture — server-rendered with vanilla JS interactivity

- Pages are rendered server-side by Django templates. URL → view → context → template → HTML.
- Each top-level surface is its own page:
  - `/login/`
  - `/` — workspace dashboard / recent activity feed
  - `/projects/`
  - `/projects/{slug_prefix}/` — project page (kanban + table tabs)
  - `/projects/{slug_prefix}/{number}/` — task detail
  - `/activity/` — workspace-wide activity feed
  - `/members/` — workspace members (admin)
  - `/settings/`
- Inside a page, vanilla JS handles inline interactions: label picker dropdown, bulk-select toolbar, status quick-change, inline comment posting, due date picker. JS calls the JSON API (`/api/v1/...`) directly and updates the DOM.
- No SPA, no client-side router, no history API manipulation beyond the browser's native behavior.

### JavaScript approach

- **Pure vanilla JS.** `fetch()`, `document.querySelector`, event delegation. No React, Vue, Svelte, Alpine, or HTMX.
- One global utility module (`static/js/acta.js`) for shared concerns: CSRF token retrieval, JSON fetch wrapper, toast notifications, modal helpers.
- Per-page JS lives in `static/js/pages/{page}.js` and is included only on pages that need it.
- Third-party JS allowed when it's a small, focused, no-dep library — e.g. `sortable.js` for drag-drop kanban (post-MVP).

### Tailwind delivery — Play CDN

- `<script src="https://cdn.tailwindcss.com"></script>` in the base template. No npm, no PostCSS, no build step.
- Tailwind config (theme extensions, dark mode strategy) goes inline in the base template via the Play CDN config script.
- Trade-off accepted: slower first paint, ~300 KB of Tailwind runtime per pageview. Acceptable for an internal tool with caching.
- Switch to the standalone Tailwind CLI (single binary, compiles CSS to `static/css/acta.css`) if (a) perf becomes a real complaint, or (b) we deploy to a network where the CDN is blocked. Migration is mechanical: swap the `<script>` for a `<link>` and run the binary.

### Markdown rendering — server-side

- Markdown source is stored in the model (`Task.description`, `Comment.body`, `ProjectUpdate.body`).
- Rendered HTML is computed in the serializer/view layer using:
  - **`markdown`** library (or `markdown-it-py`) for HTML generation. Final pick during implementation; both are pure Python and well-maintained.
  - **`bleach`** for sanitization with a whitelist of safe tags/attributes.
- API responses include both fields where applicable:
  ```json
  { "description": "...raw markdown...", "description_html": "<p>...</p>" }
  ```
- The frontend renders `description_html` directly (it's already sanitized) and uses `description` for edit forms.
- Rendering is cheap enough to do per request in MVP; if it becomes a bottleneck, cache the HTML in a DB column on save.

### Theme — dark default with toggle

- Dark theme is the default. Light theme is opt-in via a setting on the user profile.
- Tailwind's `dark:` variant is used everywhere (Tailwind `darkMode: 'class'`); the `<html>` element gets `class="dark"` or no class based on the user's setting, set server-side in the base template.
- An unauthenticated user (login page) sees dark theme.
- No system-preference auto-switch in MVP; opinionated default reduces config surface.

### Accessibility baseline

- Semantic HTML (`<button>` not `<div onclick>`).
- Keyboard navigation for kanban and task lists (arrow keys to move between cards) — nice-to-have, not required for MVP.
- Color contrast: rely on Tailwind's default palette in both light and dark; no hand-picked colors that fail WCAG AA.

## Why

- **Server-rendered pages** with vanilla JS interactivity is the same pattern Vox already runs in `ksu24.back` (~8000 lines, working). Zero learning cost, zero build cost, fast iteration.
- **No HTMX**, even though it would be a fit, because it's a new dependency and a new mental model to learn during a time-boxed MVP. Pure vanilla wins on "I know exactly what this code does."
- **Play CDN** for Tailwind is the cheapest path to a working UI. No npm dependency for a Python project.
- **Server-side markdown** is the secure default — sanitization on the server, no client-side XSS risk, no need for DOMPurify, smaller JS bundle.
- **Dark default** matches the developer-tool aesthetic and is what most modern trackers (Linear, Vercel, GitHub-in-dark) start with.

## Consequences

- Real-time updates (someone else moves a card → you see it instantly) require either polling or post-MVP SSE/WebSocket. Polling on the activity feed every 30s is acceptable; out-of-MVP for kanban.
- The Play CDN tag means `cdn.tailwindcss.com` must be reachable from end-user browsers. If the deployment ever sits behind a firewall, switch to the standalone CLI (one-day migration).
- Vanilla JS means more imperative DOM code than HTMX or a framework would need. Mitigated by keeping interactions small and per-page modules thin.
- Server-side markdown rendering requires a sanitizer dependency (`bleach`) and a small allowlist of HTML tags. Documented in `spec/markdown.md` (TBD).
- Light/dark toggle requires storing the preference somewhere. Default: `User.preferences` JSONField or a separate `UserPreference` model with theme + locale. To be decided during accounts app design.

## Open Questions

- Whether to use `markdown` (older, simpler) or `markdown-it-py` (more modern, better extension support). Decide at implementation time based on which has cleaner integration with `bleach` allowlists.
- Whether mentions (`@username`) inside comments and task descriptions are MVP scope — currently no, but the markdown pipeline should be designed so adding mentions later is a serializer extension, not a frontend rewrite.
- Whether to ship a minimal CSS file alongside the Tailwind CDN for app-specific tweaks (utility classes Tailwind doesn't cover) — likely yes, a single small `acta.css` in `static/css/`.
