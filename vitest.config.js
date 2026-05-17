/**
 * Vitest configuration for the front-end unit-test suite.
 *
 * Tests live next to the modules they cover, under
 * ``static_src/js/lib/__tests__/``. jsdom is the default environment
 * so DOM helpers (``document.querySelectorAll`` etc.) work without
 * the test needing a browser.
 *
 * Run:  npm test            (one-shot)
 *       npm run test:watch  (file-watch mode for development)
 */
export default {
  test: {
    environment: "jsdom",
    include: ["static_src/js/**/*.test.js"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["static_src/js/lib/**/*.js"],
    },
  },
};
