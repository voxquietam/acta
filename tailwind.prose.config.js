/**
 * Tailwind v3 config for the standalone ``prose.bundle.css``.
 *
 * Split out from the main bundle (see ``tailwind.config.js``) because
 * ``@tailwindcss/typography`` ships ~20 KB of selectors used only where
 * Markdown is rendered: ``/inbox/``, project overview, task description,
 * comments. All Tasks list / kanban / dashboard never reference
 * ``prose-*`` — so the cost was pure unused-CSS on those pages.
 *
 * Build via ``npm run build:css:prose`` →
 * ``static/css/prose.bundle.css``. Loaded with a non-blocking
 * ``rel="preload" as="style" onload`` swap in ``templates/base.html``
 * so it never sits on the render-blocking critical path.
 *
 * ``corePlugins.preflight: false`` skips Tailwind's reset (already
 * applied by the main bundle). ``content`` is scoped to templates, the
 * only place ``prose-*`` class strings exist.
 */
module.exports = {
  darkMode: "class",
  content: [
    "./templates/**/*.html",
    "./apps/**/*.html",
  ],
  corePlugins: {
    preflight: false,
  },
  plugins: [
    require("@tailwindcss/typography"),
  ],
};
