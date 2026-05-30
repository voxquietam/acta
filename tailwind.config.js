/**
 * Tailwind v3 config used by the build step.
 *
 * Content paths cover every place a Tailwind class might appear: all
 * Django templates and Python view modules (some classes live in
 * inline-rendered ``class="…"`` strings produced from Python, e.g.
 * filter chip ``:class`` bindings dispatched as strings — Tailwind's
 * extractor reads the source for ``token-like`` runs).
 *
 * Replaces the previous Tailwind Play CDN setup so the stylesheet
 * ships as a static, pre-compiled file (no FOUC, no in-browser JIT).
 */
module.exports = {
  darkMode: "class",
  content: [
    "./templates/**/*.html",
    "./apps/**/*.py",
    "./apps/**/*.html",
    "./static_src/js/**/*.js",
    "./static/js/**/*.js",
    // ``dashboard.css`` is handwritten (not piped through PostCSS) and
    // lives next to ``main.bundle.css`` as a separate file inlined via
    // ``{% inline_static %}`` on the dashboard page. Today it uses no
    // Tailwind utility classes (everything is ``.dash``-prefixed), so
    // scanning it here is a no-op. The entry is defensive: when someone
    // adds e.g. ``.bg-zinc-900`` inside, Tailwind's purge keeps the
    // utility instead of dropping it from the bundle.
    "./static/css/dashboard.css",
  ],
  safelist: [
    // Project icon picker palette (apps/projects/icons.py). Class names
    // are composed dynamically from a color key (``bg-{{ color }}-500``)
    // so Tailwind's content scanner can't pick them up; the regex
    // safelists every text-/bg-500 pair we ever render.
    {
      pattern:
        /^(text|bg)-(red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|slate|gray|zinc|stone)-500$/,
    },
  ],
  theme: {
    extend: {
      colors: {
        // Brand palette — custom indigo-blue, anchored at #425af5.
        // Swapped from the Tailwind lavender ramp on 2026-05-18 per
        // the design system in `acta-design-system/colors_and_type.css`.
        // ``brand-500`` is the focus ring / accent fill; ``brand-600``
        // is the primary CTA fill with ``hover:bg-brand-500`` (lighter
        // on hover — the signature "lift" move).
        brand: {
          50:  "#f0f3ff",
          100: "#dfe5ff",
          200: "#bcc8ff",
          300: "#94a3ff",
          400: "#6c7efb",
          500: "#425af5",
          600: "#2e44d6",
          700: "#2435ad",
          800: "#1d2a87",
          900: "#182466",
          950: "#0e1340",
        },

        // Semantic colour tokens — backed by CSS variables defined in
        // ``static_src/css/main.css`` under ``:root`` (light) and
        // ``html.dark`` (dark). Use these for any surface / text /
        // border that should track the active theme. Hardcoded
        // ``bg-zinc-*`` / ``text-zinc-*`` should be reserved for cases
        // where a specific shade is intentional regardless of theme
        // (status / priority badges keep their own palette).
        //
        // ``<alpha-value>`` is Tailwind's placeholder so utilities like
        // ``bg-muted/50`` or ``border-border/30`` work correctly — the
        // generator injects the alpha channel without us re-declaring
        // each colour for every opacity step.
        background: "rgb(var(--background) / <alpha-value>)",
        foreground: "rgb(var(--foreground) / <alpha-value>)",
        card: "rgb(var(--card) / <alpha-value>)",
        muted: "rgb(var(--muted) / <alpha-value>)",
        subtle: "rgb(var(--subtle) / <alpha-value>)",
        "muted-foreground": "rgb(var(--muted-foreground) / <alpha-value>)",
        "subtle-foreground": "rgb(var(--subtle-foreground) / <alpha-value>)",
        "placeholder-foreground": "rgb(var(--placeholder-foreground) / <alpha-value>)",
        border: "rgb(var(--border) / <alpha-value>)",
        "border-strong": "rgb(var(--border-strong) / <alpha-value>)",
        ring: "rgb(var(--ring) / <alpha-value>)",
      },
    },
  },
  plugins: [
    // ``@tailwindcss/typography`` is intentionally NOT included here —
    // it ships ~20 KB of selectors used only on /inbox/, project
    // overview, and task description / comments. It now lives in a
    // standalone ``static/css/prose.bundle.css`` built from
    // ``tailwind.prose.config.js``, loaded non-blocking from
    // ``templates/base.html`` so it stays off the render-blocking
    // critical path on All Tasks / kanban / dashboard.
  ],
};
