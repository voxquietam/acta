# 00 — Wave 4 baseline (infra / a11y / mobile / bundle)

> **Wave 4 / Chunk A** — read-only project-wide audit, fourth pass.
> Date: 2026-05-30. Branch: `dev`. HEAD: `997d047` (11 commits ahead
> of `origin/dev`, ~47 ahead of prod `c514014`, 568 web+activity
> tests pass).
> Sources: 4 parallel Explore agents (E1 a11y, E2 mobile, E3 bundle,
> E4 Playwright/CI) — the four Wave 4 chunks pre-scoped in Wave 3's
> `99-wave3-backlog.md §5`.
> Purpose: anchor Wave 4's findings on the actual surface area
> across infra + a11y + mobile + bundling. **No code changed.**

---

## 1. Wave 4 chunk reports

| # | Chunk | Report | Findings | Headline |
|---|---|---|---:|---|
| E1 | A11y full pass | `01-a11y.md` | 32 (14 P1 / 18 P2) | Modal focus trap absent; ~6 interactive `<span>` elements not keyboard-reachable |
| E2 | Mobile / touch | `02-mobile.md` | 13 (4 P1 / 6 P2 / 3 P3) | Sidebar hidden below `md:` with **no hamburger fallback**; 31+ `group-hover` controls invisible on touch |
| E3 | Bundle / code-split | `03-bundle.md` | 12 (3 P1 / 4 P2 / 5 P3) | TipTap (~150 KB gzip) loads on every page; lazy-load = headline win |
| E4 | Playwright / CI | `04-playwright-ci.md` | 10 (3 P1 / 4 P2 / 3 P3) | No CI pipeline; vitest unrunnable inside compose; Playwright absent |

**Total findings: 67.** None are P0 correctness regressions. The
spread reflects deferred items the previous waves explicitly punted
(a11y / mobile / split / infra are all "Wave 4 candidates" per
Wave 3 backlog §4-5).

## 2. Surface area inventory (anchors)

### A11y surface (E1)

| Surface | Count | Notes |
|---|---:|---|
| Modals via `_modal_shell.html` | 4 (create-task, create-project, create-workspace, bulk-archive) | Focus trap missing on all 4 |
| Alpine dropdowns / popovers | ≥ 8 (status, priority, assignee × 2 sites, labels, bulk context, workspace switcher, user menu) | Wave 3 PR-5 covered Cmd+K + filter sidebar only |
| Inline interactive `<span>` / `<div>` elements | ~25 across `_task_row.html`, `_task_card.html`, `_table_row.html` | Not keyboard-reachable |
| Lucide icons in templates | ~260 unique symbols via sprite (Wave 3 PR-2) | None carry `aria-hidden`/`role="img"` discipline |

### Mobile surface (E2)

| Breakpoint | Used at | Count |
|---|---|---:|
| `sm:` (≥ 640px) | Cmd+K hints, a few text-sizes | ~6 sites |
| `md:` (≥ 768px) | Sidebar visibility, topbar layout, table show | ~30 sites |
| `lg:` (≥ 1024px) | Filter sidebar, dashboard panels | ~15 sites |
| `xl:` (≥ 1280px) | Two-column overview | 2 sites |

Below `md:` (i.e. < 768px): sidebar gone, hamburger hidden, no
drawer. Workspace switching / nav unreachable without manual URL.

### Bundle surface (E3)

| Asset | Size | Lazy? |
|---|---:|---|
| `static/js/acta.min.js` | 54 KB (post-terser, ~145 KB source) | No |
| `static/js/description_editor.bundle.js` | ~500 KB / ~150 KB gzip | **No — loads everywhere** |
| `static/js/reactions.bundle.js` | < 1 KB | No |
| `static/css/main.bundle.css` | 111 KB / ~16 KB gzip | No |
| `static/css/prose.bundle.css` | 19 KB | ✓ (Wave 1 PR-7) |
| Vendor (`/static/vendor/`) | htmx, sse, idiomorph, Sortable, Exo 2, JetBrains Mono, Flatpickr, emoji data, Lucide sprite | All self-hosted ✓ |

E3 §2 maps `acta.js` to 13 modules with zero circular deps. The
13-file split is a refactor (no KB win on its own) but unblocks
per-route lazy-load (timeline ~12 KB, hover-cards, hotkeys).

### Test / CI surface (E4)

| Runner | Files | Status |
|---|---:|---|
| pytest | 95 across 16 apps | ✓ 568 passing |
| vitest | 2 (filter parity, sort parity) | ✗ can't run via compose; works on host with caveats |
| Playwright | 0 | ⊘ not installed |
| pre-commit | 4 hooks (black, isort, flake8, no-multiline-django-comment) | ✓ local only |
| CI pipeline | — | ✗ no `.github/workflows/`, `.woodpecker.yml` |

## 3. Effort spread

| Chunk | Total effort if shipped end-to-end |
|---|---:|
| E1 (a11y) | ~40-44 h (WCAG Level A baseline) |
| E2 (mobile) | 32-39 h full; 13-18 h for "mobile-viewable minimum" |
| E3 (bundle) | ~14 h (TipTap + timeline lazy + split refactor) |
| E4 (Playwright/CI) | ~13 h (compose service + Playwright + CI) |

**Cumulative Wave 4 if shipped in full: ~100-110 h.** Backlog
synthesis (`99-wave4-backlog.md`) ranks PRs so a meaningful subset
(~25-30 h) lands first and the rest queues for Wave 5.

## 4. Methodology

- Five-chunk parallel sweep (E1-E4 + baseline), all read-only.
- All four agent reports include file:line references.
- Where Wave 3 already shipped a fix (PR-5 a11y minimum, PR-9 docs,
  cb7f771 SSE), agents marked ✓ and pivoted to the gap.
- Effort estimates in developer-hours; severity in P0-P3.

## 5. Wave 4 status

- 4 audit chunks complete (E1-E4) + this baseline.
- Read-only methodology held; no code touched.
- Synthesis (`99-wave4-backlog.md`) ranks PRs by impact / effort /
  risk and pulls Wave 5 candidates forward.
- Headline question (for Vox's decision): infra PR-W4-A (vitest
  unblock, 0.5 h) is a no-brainer first ship; the rest is a menu.
