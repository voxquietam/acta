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
        // Brand palette mirrors the inline config that lived in
        // base.html under the Play CDN. Lavender by default; swap
        // here to re-skin the whole app.
        brand: {
          50:  "#faf5ff",
          100: "#f3e8ff",
          200: "#e9d5ff",
          300: "#d8b4fe",
          400: "#c084fc",
          500: "#a855f7",
          600: "#9333ea",
          700: "#7e22ce",
          800: "#6b21a8",
          900: "#581c87",
          950: "#3b0764",
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
    require("@tailwindcss/typography"),
  ],
};
