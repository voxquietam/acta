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
      },
    },
  },
  plugins: [
    require("@tailwindcss/typography"),
  ],
};
