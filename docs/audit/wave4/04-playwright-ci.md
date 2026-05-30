# 04 — Playwright + CI infrastructure

> **Wave 4 / Chunk E4** — read-only audit of the test-runner / CI
> surface. Date: 2026-05-30. Branch: `dev`. **No code changed.**
>
> Sources: Wave 3 PR-8 deferral (`docs/audit/wave3/99-wave3-backlog.md
> §2 PR-8 + §5 I-list`), Wave 3 D5 F5 + D6 F11, the broken-vitest note
> in handoff, the absent CI pipeline (`[[project-todo-woodpecker-ci]]`).

---

## 1. Bottom line

Three blockers, in order of dependency:

1. **Vitest is unrunnable today** because no compose service has both
   Node + the repo mounted. The "frontend" stage in `Dockerfile` is
   build-only; the `web` container is `python:3.13-slim-bookworm` (no
   `npm`). Host-side `npm test` is the *only* path, and the
   `node_modules/@rollup/*` set produced by `npm ci` on macOS does not
   carry the Alpine binary needed if anyone tries to invoke it from
   the build stage. This is the "rollup native module mismatch"
   referenced in the handoff. **Fix: add a `frontend-test` compose
   service (Node + repo mount), run `npm test` there.** ~0.5 h.
2. **Playwright is not installed.** `package.json` has only vitest +
   jsdom (lines 28-32 of `devDependencies`). No `playwright.config.*`
   anywhere in the repo. Wave 3 PR-8 is blocked behind this and the
   CI question. **~6-8 h** to wire config + Chromium-only smoke suite +
   compose glue.
3. **There is no CI pipeline.** No `.github/workflows/`,
   `.woodpecker.yml`, or `.gitlab-ci.yml` in the tree. Pre-commit
   hooks are tracked but local-only — black/isort/flake8/Django
   comment check fire only on `git commit`, never on push.
   **~4-6 h** to wire a minimal pipeline (whichever platform Vox
   picks; see §7).

**Total to "Playwright gating PRs": ~12-15 h** spread across three
PRs. Phase 1 (compose service) unblocks vitest *today*; phases 2 + 3
can land in parallel.

---

## 2. Test runner inventory

| Runner | Coverage | Location | Status |
|---|---|---|---|
| **pytest** | Backend (models, services, views, API, templates, SSE handlers) | 16 apps × `tests/` dir | ✓ 568 passing |
| **vitest** | Frontend parity (filter + sort) | `static_src/js/lib/__tests__/` | ✗ Can't run in current compose; passes on host with darwin binaries |
| **Playwright** | E2E (browser automation) | — | ⊘ Not installed |
| **pre-commit** | Lint (black 24.8.0, isort 5.13.2, flake8 7.1.1, no-multiline-django-comments) | `.pre-commit-config.yaml` | ✓ Local hooks, ✗ not in CI |

### Python — `pyproject.toml:50-66`

```toml
[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "acta.settings.dev"
python_files = ["test_*.py"]
testpaths = ["apps"]
addopts = ["--reuse-db", "-q"]
```

`--reuse-db` keeps the test DB across runs. `testpaths = ["apps"]`
limits discovery to the 16 `apps/*/tests/` directories.

### JavaScript — `vitest.config.js:1-23`

```javascript
export default {
  test: {
    environment: "jsdom",
    include: ["static_src/js/**/*.test.js"],
    coverage: { … include: ["static_src/js/lib/**/*.js"] },
  },
};
```

Tests live at `static_src/js/lib/__tests__/filter.test.js` (parity
with Python filter logic) and `sort.test.js`. Both passing on host
last time they ran.

### Playwright — absent

No config, no `tests/e2e/`, no devDependency entry. Wave 3 PR-8
needs all of the above.

---

## 3. Why vitest can't run from inside compose today

### What the user means by "broken in dev"

The handoff line:

> vitest in dev is broken (rollup native module mismatch between
> mac-host node_modules and alpine runner)

What's actually happening:

- The **frontend** stage (Dockerfile lines 8-22, `node:20-alpine`)
  exists only at build time. It runs `npm ci && npm run build:css &&
  npm run build:js` and is COPYed into the python stage. Once built,
  the stage is gone.
- The **web** service is `python:3.13-slim-bookworm` (Dockerfile
  line 25). It has no `npm`, no Node, no `node_modules`. `docker
  compose exec web npm test` errors with "npm: not found".
- On the **host** (macOS), `npm test` works *iff* `npm install` was
  run on the host (drops `@rollup/rollup-darwin-x64` into
  `node_modules`). If the only `npm ci` ever invoked was inside the
  Alpine builder, the host `node_modules` has only the Alpine binary
  (`@rollup/rollup-linux-x64-musl`) and vitest fails to start on
  macOS — and vice-versa. The `node_modules/` volume mount in
  `docker-compose.dev.yml` (`.:/app`) blurs this further: whatever
  `npm install` last wrote leaks across.

So "rollup native module mismatch" = the platform of the binaries in
`node_modules/@rollup/` doesn't match the platform of the process
trying to import them. Vitest pulls rollup as a vite-internal
dependency.

### Cleanest fix: dedicated `frontend-test` compose service

Mirror the build stage as a runtime service that mounts the repo and
runs vitest. New entry in `docker-compose.dev.yml`:

```yaml
  frontend-test:
    image: node:20-alpine
    working_dir: /app
    volumes:
      - .:/app
      - frontend-node-modules:/app/node_modules
    profiles: ["test"]            # skipped by default `make up`
    command: ["sh", "-c", "npm ci && npm test"]

volumes:
  frontend-node-modules: {}
```

A named volume on `/app/node_modules` keeps the Alpine binaries
isolated from the host. The host can still run `npm install &&
npm test` directly on macOS; the two `node_modules` instances stop
fighting.

Then: `docker compose --profile test run --rm frontend-test`.

**Alternative (simpler, less isolated):** drop the named volume and
let host + container clash, but always re-run `npm rebuild` before
each side runs. Loses determinism; not recommended.

### Effort

**0.5 h:** add the compose entry, verify `docker compose run
frontend-test` exits 0, document in `docs/development.md` (if it
exists) or CONTRIBUTING.

---

## 4. Client-side test inventory

### What exists

| File | LOC | What | Confidence |
|---|---:|---|---|
| `static_src/js/lib/__tests__/filter.test.js` | ~230 | Parity with Python filter logic | High — direct mirror |
| `static_src/js/lib/__tests__/sort.test.js` | ~180 | Parity with Python sort logic | High |

Both are pure-logic mirrors — no DOM, no Alpine, no fetch. Useful as
regression nets but cover < 5 % of `static/js/acta.js` (3 331 LOC).

### What is NOT tested

Sweep of `static/js/acta.js` + Alpine components in templates surfaces
the following untested logic (Wave 3 D2 + D6 already flagged most):

| Module | LOC | Why testable matters |
|---|---:|---|
| `acta.js` event-listener wiring | ~900 | Listener accumulation on swap (Wave 3 D2 F1) |
| Cmd+K palette Alpine component | ~500 (template-inline) | Cursor wrap (Wave 3 D6 F1), search debounce, escape mid-select (D5 F5), recents dedup (D6 F7) |
| Filter sidebar Alpine | ~250 | Filter state ↔ URL sync, applied-count badge |
| Kanban DnD glue | ~120 | Optimistic style apply, SSE collision, x-show drift |
| SSE handlers (`acta.js`) | ~400 | Self-actor filter (handoff §Context), event-type routing |
| TipTap mount lifecycle | ~80 | Editor remount on nav swap |
| Mention picker | ~60 | Escape mid-select (Wave 3 D5 F5) |
| Timeline `initTimeline` | ~470 | Deadline patch silent failure (D2 F3) |

Total: ~2 800 LOC client behaviour without a regression net. None of
this needs 100 % coverage; the Wave 3 D5 F5 / D6 F11 list narrows to
8-10 critical flows worth automating.

---

## 5. Playwright wire-up plan

### Layout

| Artefact | Location | Why |
|---|---|---|
| `playwright.config.ts` | repo root | Conventional; vitest config lives at root, mirror it. |
| `tests/e2e/` | new directory | Keep separate from `apps/<x>/tests/` (those are pytest). |
| Smoke specs | `tests/e2e/*.spec.ts` | Per-flow file (auth, palette, dnd, sse). |
| Seed command | `apps/common/management/commands/seed_e2e_data.py` | Idempotent: clean → create ws + user + project + 5 tasks. |
| Playwright service | new compose entry | `mcr.microsoft.com/playwright:v1.45.0-jammy` (Chromium pre-installed). |

### Compose entry

```yaml
  e2e:
    image: mcr.microsoft.com/playwright:v1.45.0-jammy
    working_dir: /app
    volumes:
      - .:/app
      - e2e-node-modules:/app/node_modules
    profiles: ["test"]
    environment:
      BASE_URL: http://web:8000
    depends_on:
      - web
    command: ["sh", "-c", "npm ci && npx playwright test"]
```

`BASE_URL=http://web:8000` lets Playwright hit the web service through
the compose network. Seed runs separately: `docker compose exec web
python manage.py seed_e2e_data`.

### Smoke-test scope (Wave 3 PR-8 + a11y/mobile pre-merge net)

| # | File | Flow | Maps to |
|---|---|---|---|
| 1 | `auth.spec.ts` | Login → dashboard renders | sanity |
| 2 | `palette.spec.ts` | Cmd+K opens, search, arrow-nav, Enter, Esc | Wave 3 D6 F11 |
| 3 | `palette.spec.ts` | Mention picker: open with `@`, Esc closes picker only | Wave 3 D5 F5 |
| 4 | `kanban.spec.ts` | DnD card column-to-column, optimistic style applies | Wave 3 D4 |
| 5 | `sse.spec.ts` | Two contexts (storageState swap), edit in A, B updates without reload | handoff §Context (GZip+SSE fix) |
| 6 | `a11y.spec.ts` | Tab through filter sidebar; ARIA expanded toggles | Wave 4 E1 PR-5 follow-on |
| 7 | `mobile.spec.ts` | Viewport 375×667; hamburger reachable; modal full-screen | Wave 4 E2 |
| 8 | `editor.spec.ts` | TipTap mount + remount on nav swap | Wave 1 c220584 regression guard |

8 specs × ~20 min = ~3 h to write. Plus 1-2 h of flake-debugging on
SSE timing.

### Effort

| Step | Hours |
|---|---:|
| Add `playwright` to `package.json` + `npm install` | 0.25 |
| `playwright.config.ts` (baseURL, Chromium-only, headless, retries) | 0.5 |
| Compose `e2e` service + seed command | 1.0 |
| 8 smoke specs | 3.0 |
| Debug + stabilise (SSE timing especially) | 1.5 |
| Wire into CI (see §7) | 0.5 (incremental) |
| **Total** | **6.75** |

---

## 6. CI pipeline (proposal)

### Current state

No CI file in the tree. Closest is `[[project-todo-woodpecker-ci]]`,
which scopes "PR check + master + tag" but is unscheduled. Pre-commit
hooks are in `.pre-commit-config.yaml` but only fire on `git commit`
locally; nothing enforces them on push.

### Pipeline stages (platform-agnostic)

1. **lint** — `pre-commit run --all-files` (covers black, isort,
   flake8, no-multiline-django-comments). Single stage, no DB.
2. **pytest** — needs postgres sidecar. `uv pip install -r
   requirements/dev.txt && pytest apps/web/`. Cache `node_modules` not
   needed (no JS in this stage).
3. **vitest** — node:20-alpine + repo checkout. `npm ci && npm test`.
   Fast (< 30 s).
4. **playwright** — Playwright image + postgres + web booted in
   compose. `npm ci && npm run seed_e2e && npx playwright test`.
5. **gate** — depends on all of 1-4. Required for merge to `master`.

PR runs all 5; push to `master` re-runs and tags green if all pass.

### Platform choice

The TODO names Woodpecker. Pros for Vox's setup: self-hosted on her
VM, no per-minute billing, lives next to the deploy box. Cons:
operator overhead (Woodpecker server + agent containers, OAuth wire).

GitHub Actions alternative: zero operator overhead, generous free
tier on public repos (private = 2 000 min/mo on Free plan). Simpler
to bootstrap; YAML idioms more widely documented.

**Recommendation:** GitHub Actions for MVP if the repo is GitHub-hosted
already, swap to Woodpecker later if cost/ops drive it. Either way the
five stages above are identical; only the YAML dialect differs.

### Effort

| Step | Hours |
|---|---:|
| Confirm platform with Vox | 0 (decision) |
| Write CI YAML for lint + pytest + vitest | 1.5 |
| Add playwright stage (after §5 lands) | 1.0 |
| Branch protection rule + test PR | 1.0 |
| Document in `CONTRIBUTING.md` | 0.5 |
| **Total** | **4.0** |

---

## 7. Findings

### F1 — Compose has no Node service; vitest can't run via `docker compose exec` [P1]

**Where:** `Dockerfile:8-22` (frontend stage is build-only), `docker-compose.dev.yml` (no node service), `package.json:scripts.test`.
**What:** The handoff describes vitest as "broken in dev"; the actual root cause is that no compose service has both Node and the repo mounted. The frontend stage exists only at build time. Host-side `npm test` is the only path today, and the host/Alpine binary-set divergence makes that flaky.
**Fix sketch:** Add a `frontend-test` compose service per §3 (node:20-alpine, named-volume `node_modules`, `profiles: ["test"]`). Invoke via `docker compose --profile test run --rm frontend-test`.
**Effort:** 0.5 h.
**Δ:** unblocks F2 + F3 + F8.

---

### F2 — Playwright not installed; PR-8 blocked [P1]

**Where:** `package.json:devDependencies` (no playwright entry), repo root (no `playwright.config.*`), `tests/e2e/` absent.
**What:** Wave 3 PR-8 (`docs/audit/wave3/99-wave3-backlog.md §2 PR-8`) was sized at 2 h on the assumption Playwright was wired. It isn't. The 2 h budget covers test-writing only; ~7 h of infra precedes it.
**Fix sketch:** Add `playwright@^1.45.0` to devDependencies; create `playwright.config.ts` (Chromium-only, headless, baseURL from env); add the `e2e` compose service per §5; write the 8-spec smoke suite per §5 table; add seed command.
**Effort:** 6-8 h.
**Δ:** unblocks Wave 3 PR-8 + Wave 4 a11y/mobile regression net.

---

### F3 — No CI pipeline; pre-commit local-only [P1]

**Where:** Repo root (no `.github/workflows/`, `.woodpecker.yml`, `.gitlab-ci.yml`); `.pre-commit-config.yaml` exists but isn't enforced server-side.
**What:** Vox can ship to `master` without running lint or tests. Wave 1-3 have been clean only because she ran tests by hand each time. Risk grows with every contributor.
**Fix sketch:** Choose platform (Woodpecker per TODO vs GitHub Actions; recommend Actions for MVP). Wire the 5 stages from §6. Set branch protection on `master`.
**Effort:** 4 h (without playwright stage; +1 h with).
**Δ:** unblocks F8; enables auto-deploy gating.

---

### F4 — Alpine component behaviour 100 % untested client-side [P2]

**Where:** `static/js/acta.js` (~2 800 LOC of Alpine + DOM logic without coverage); per-template Alpine blocks (`_command_palette.html:501 LOC`, `_filters_sidebar.html:661 LOC`).
**What:** Filter + sort tests mirror Python logic only. None of the listener wiring, palette state machine, SSE event dispatch, kanban DnD glue, mention picker, timeline patch, or editor remount has a regression net. Wave 1 c220584 (editor remount) and Wave 3 cb7f771 (GZip+SSE) have no automated guard.
**Fix sketch:** Land the 8 Playwright smoke specs from §5 §Smoke-test scope. Each maps to a specific Wave 1-3 fix. Future Wave 4 a11y/mobile changes ride on top.
**Effort:** 3 h (specs) + 1.5 h (stability), part of F2.

---

### F5 — Pre-commit hooks bypassable; no CI enforcement [P2]

**Where:** `.pre-commit-config.yaml` (tracked), `git --no-verify` (escape hatch), no server-side check.
**What:** Anyone (including future Vox under deadline pressure) can push unlinted code. The bespoke `no-multiline-django-comments` hook is particularly important — multi-line `{# … #}` renders as visible HTML, a bug that bit Vox twice this session per the handoff.
**Fix sketch:** Run `pre-commit run --all-files` as the first stage of CI (§6 stage 1). One YAML stanza.
**Effort:** 0.5 h, part of F3.

---

### F6 — No fixture seed for Playwright; test data strategy undefined [P2]

**Where:** No `seed_e2e_data` management command exists; pytest factories live in `apps/<x>/tests/factories.py` (Django-only).
**What:** Playwright needs a known starting state (workspace, user, project, tasks) every run. Reusing pytest fixtures requires the test runner to call into Django; running through `docker compose exec web python manage.py …` works but needs an explicit command.
**Fix sketch:** Add `apps/common/management/commands/seed_e2e_data.py`: wipes any prior `e2e-fixture` workspace, recreates it idempotently, prints test credentials. Playwright `globalSetup` invokes it via subprocess; alternatively the CI pipeline runs it as a separate step before `npx playwright test`.
**Effort:** 1 h, part of F2.

---

### F7 — Rollup binary mismatch undocumented in `CONTRIBUTING.md` (absent) [P3]

**Where:** No `CONTRIBUTING.md` at repo root; the handoff is the only documentation of the `npm install` host/Alpine pitfall.
**What:** The next contributor (or future Vox after `node_modules` deletion) will lose hours re-diagnosing the rollup mismatch. F1's fix removes the live symptom but the underlying gotcha stays.
**Fix sketch:** Add a `CONTRIBUTING.md` paragraph (3-4 lines) describing the `frontend-test` service from F1 and the reason `node_modules` is volume-mounted. Cross-link from `docs/development.md` if it exists.
**Effort:** 0.25 h, part of F1.

---

### F8 — Wave 3 PR-8 effort estimate is 2 h but actual is 9 h once infra is counted [P2]

**Where:** `docs/audit/wave3/99-wave3-backlog.md` PR-8 box.
**What:** PR-8's 2 h budget assumes Playwright + CI are wired. Both are absent (F2 + F3). Real effort: F1 (0.5 h) + F2 (6.75 h) + the PR-8 spec content itself (~2 h) = ~9.25 h.
**Fix sketch:** When Wave 3 backlog rolls into Wave 4 execution, treat F1/F2/F3 as PR-Z (Wave 4 infra prerequisite) and re-scope PR-8 to "Cmd+K + mention picker smoke spec" only (~1.5 h on top of Playwright already wired).
**Effort:** documentation update only; ~10 min.

---

### F9 — No flake-suppression / retry strategy named for Playwright SSE specs [P3]

**Where:** Proposed `playwright.config.ts` (does not yet exist).
**What:** The §5 spec list includes an SSE peer-update test (spec 5). SSE timing is famously flaky in CI — the test will need explicit waits on actor visibility, retry-with-exponential-backoff, or a deterministic "force-flush" hook on the server side.
**Fix sketch:** In `playwright.config.ts` set `retries: 2` on CI only. Use `await expect(locator).toHaveText(...)` (auto-retries up to 5 s) instead of fixed sleeps. If still flaky, add a debug-only Django view that flushes the SSE queue on demand and gate behind `settings.DEBUG`.
**Effort:** 0.5 h within F2.

---

### F10 — Vitest coverage configured but never collected [P3]

**Where:** `vitest.config.js:18-22` defines a `coverage` block but `npm test` doesn't pass `--coverage`.
**What:** Coverage HTML is never generated; no signal on what fraction of `static_src/js/lib/` is exercised. Low priority because the suite is intentionally narrow (filter/sort mirrors).
**Fix sketch:** Add `"test:coverage": "vitest run --coverage"` to `package.json:scripts`. Wire into CI as an optional non-gating stage. Surface the HTML artifact.
**Effort:** 0.25 h.

---

## 8. Roadmap

Ordered execution to "Playwright gating PRs":

| PR | Scope | Effort | Blocks on |
|---|---|---:|---|
| **PR-W4-A** | F1 — add `frontend-test` compose service + `frontend-node-modules` volume; vitest runs via `docker compose --profile test run --rm frontend-test` | 0.5 h | — |
| **PR-W4-B** | F3 + F5 — add CI YAML (lint + pytest + vitest stages), branch protection, `CONTRIBUTING.md` lint section | 4 h | PR-W4-A |
| **PR-W4-C** | F2 + F4 + F6 + F9 — install Playwright, write 8 smoke specs, add `e2e` compose service + seed command, wire flake controls | 6.75 h | PR-W4-A |
| **PR-W4-D** | Wire Playwright stage into CI (extend PR-W4-B YAML) | 1 h | PR-W4-B + PR-W4-C |
| **PR-W4-E** | F8 — close out Wave 3 PR-8 (Cmd+K + mention picker spec is already in PR-W4-C; this is documentation only) | 0.25 h | PR-W4-D |
| **PR-W4-F** | F10 — vitest coverage `npm run test:coverage`; non-gating CI stage | 0.5 h | PR-W4-B |
| **Total** | | **~13 h** | |

PR-W4-A → PR-W4-B and PR-W4-C run in parallel after A lands. PR-W4-D
joins them. PR-W4-E/F are housekeeping.

---

## 9. Verdict

- **Vitest fix verdict:** add a `frontend-test` compose service with
  a dedicated `node_modules` volume. Host `npm install` and Alpine
  `npm ci` stop colliding. ~0.5 h. **(F1)**
- **Playwright wire-up effort:** ~6-8 h end-to-end, including 8
  smoke specs that cover every Wave 3 finding and the Wave 1 editor
  remount. **(F2)**
- **Ship Playwright/CI before or after Wave 4 a11y/mobile PRs?**
  Land PR-W4-A (0.5 h) *first* — frees vitest. Run PR-W4-B + PR-W4-C
  in parallel with the Wave 4 E1/E2 audit PRs. By the time a11y or
  mobile fix-PRs are ready to merge, the Playwright net is in place
  to catch regressions. Net schedule cost: ~1 day.

---

## 10. Out of scope

For transparency — this audit did NOT cover:

- **Visual regression** (Percy / Chromatic style). Defer until
  Playwright is stable; add as a separate Wave 5 chunk.
- **Load testing** (k6 / Locust). The 20-user target makes this
  optional.
- **Mutation testing** (mutmut / Stryker). Premature; the 568-test
  base is already high-quality.
- **Security scanning** in CI (Bandit / Safety / Snyk). Worth a
  follow-up but not blocking.
