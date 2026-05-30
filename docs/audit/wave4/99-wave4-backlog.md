# 99 — Wave 4 synthesis + prioritized backlog

> **Wave 4 / Chunk Z** — final pass.
> Date: 2026-05-30. Read-only. **No code changed.**
> Consolidates findings from E1, E2, E3, E4 into a ranked fix queue.

---

## 1. Bottom line

Four per-domain audits (a11y / mobile / bundle / Playwright+CI).
**67 findings total**, none P0. Wave 4 is large by design — these
chunks were explicitly deferred from Waves 1-3 as "ship-when-ready
investment", not "ship-now bug-fix".

**Three headline ships:**

1. **PR-W4-A — vitest unblock + CI pipeline** (5 h). One-time
   infra; unblocks every Wave 4 PR's regression net + future
   contributors.
2. **PR-W4-D — TipTap lazy-load + timeline lazy-load** (4 h).
   Cold-load savings ~160 KB gzip on the 70 % of routes that
   don't open the editor — biggest perf delta in Wave 4.
3. **PR-W4-E — a11y P1 sweep** (8-10 h). Modal focus trap +
   inline-cell keyboard reach + ARIA dropdowns + icon hidden
   discipline. Unlocks WCAG 2.1 Level A baseline.

**Full queue PR-W4-A → PR-W4-N: ~50 h** spread.

**Wave 5 candidates** in §5 (full mobile-optimal pass, colour
contrast measurement, build-tooling migration).

---

## 2. PR queue (proposed execution order)

Each entry small + focused. **Impact** (1-5) = perceptible user
value or developer-velocity. **Effort** (1-5) = developer-hours.
**Risk** (1-5) = likelihood of regression.

### PR-W4-A — Vitest unblock + minimal CI pipeline `[I:4 / E:2 / R:1]`

Bundles E4 F1 + F3 + F5 + F7.

- Add `frontend-test` service to `docker-compose.dev.yml` with named
  `node_modules` volume (E4 §3); `docker compose --profile test run
  --rm frontend-test` runs vitest.
- Decide CI platform (GitHub Actions recommended for MVP, Woodpecker
  defer); write `.github/workflows/ci.yml` with stages: **lint**
  (`pre-commit run --all-files`) + **pytest** (postgres sidecar) +
  **vitest**.
- Set `master` branch protection: all stages required.

Estimated: **5 h**. Risk: low — purely additive. Unlocks every
downstream Wave 4 PR's regression net.

### PR-W4-B — Playwright wire-up + 8 smoke specs `[I:3 / E:4 / R:2]`

E4 F2 + F4 + F6 + F9. Depends on PR-W4-A.

- Install Playwright; add `playwright.config.ts` (Chromium-only,
  headless, retries=2 on CI).
- Add `e2e` compose service + `seed_e2e_data` management command.
- 8 smoke specs (auth / palette / mention-picker-esc / kanban-dnd /
  sse / a11y-tab / mobile-viewport / editor-mount) per E4 §5.
- Wire Playwright stage into the CI YAML from PR-W4-A.

Estimated: **6-8 h**. Closes out deferred Wave 3 PR-8 + creates a
regression net for the a11y/mobile PRs below.

### PR-W4-C — A11y P1 critical sweep `[I:4 / E:3 / R:2]`

Bundle of E1 P1 findings that block keyboard-only users.

- **E1 F1** — focus trap + restore on `_modal_shell.html` (Alpine
  lifecycle hooks, `inert` on siblings on open).
- **E1 F16-F19** — convert ~25 interactive `<span>`/`<div>` inline
  cells (task row, kanban card, task detail edit) to
  `role="button"` + `tabindex="0"` + Enter/Space handlers.
- **E1 F21-F22** — `aria-hidden="true"` discipline on all decorative
  Lucide icons; `alt` on avatar `<img>` (sweep grep).
- **E1 F11** — `aria-required` / `aria-invalid` /
  `aria-describedby` on the 5 forms (create-task, create-project,
  create-workspace, comment, settings).

Estimated: **8-10 h**. Highest user-impact a11y PR in Wave 4.
Regression net: PR-W4-B's `a11y.spec.ts`.

### PR-W4-D — TipTap + timeline lazy-load `[I:4 / E:2 / R:2]`

E3 F1 + F2 + F12. The headline perf PR for Wave 4.

- TipTap (`description_editor.bundle.js`, ~150 KB gzip) currently
  loads on every page; mount it only when a task-detail panel or
  comment composer opens. Use `<link rel="modulepreload">` on detail
  routes to mitigate first-open latency.
- Timeline `initTimeline` (~12 KB minified) lives in `acta.js`; pull
  to a separate module that loads on the Gantt route only.

Estimated: **3-4 h**. Δ cold-load: **~160 KB gzip** on
`/tasks/?panel=list` (the heaviest non-timeline route).

### PR-W4-E — A11y dropdowns + popovers `[I:3 / E:3 / R:2]`

E1 F6-F10. Status / priority / assignee / labels pickers and the
bulk context menu need `role="menu"` / `role="listbox"` + arrow-key
nav + `aria-expanded` on trigger.

Estimated: **5-6 h**. Cmd+K and filter sidebar already done in
Wave 3 PR-5; this extends the pattern.

### PR-W4-F — Mobile drawer + hamburger reach `[I:3 / E:3 / R:2]`

E2 F1. Sidebar invisible below `md:` with no fallback today.

- Unconditionally render the hamburger button in topbar.
- Add slide-in drawer (Alpine + Tailwind transitions) that mirrors
  the rail's nav links + workspace switcher on `<md` viewports.
- Sidebar in the drawer remains the same template (DRY).

Estimated: **4-6 h**. Closes `[[project-todo-mobile-viewable]]`
headline gap. Without it, the app is unnavigable on phones.

### PR-W4-G — Touch-visible actions + kanban touch DnD `[I:2 / E:3 / R:2]`

E2 F4 + F5. Two sub-fixes:

- F4: replace `opacity-0 group-hover:opacity-100` on 31+ controls
  (clear ✕, promote chip, label-row reorder, etc.) with
  `opacity-60 hover:opacity-100` — visible on touch, still subtle on
  desktop. Or feature-detect touch and force opacity.
- F5: SortableJS supports touch but needs `delay: 100` +
  `delayOnTouchOnly: true`; verify config in `acta.js:kanban-dnd`
  module and add explicit touch handlers.

Estimated: **4 h**.

### PR-W4-H — `acta.js` 13-file split `[I:2 / E:3 / R:2]`

E3 F8. Refactor only — no KB win on its own, but unblocks future
per-module lazy-load and reduces cognitive load on `acta.js`
edits.

- Extract `static_src/js/acta/` per the 13-file table (E3 §2).
- Update `package.json` `build:js:acta` script: esbuild bundle +
  terser.
- Sanity-check all 27 `window.acta.*` exports still present
  (Wave 3 PR-9 map).

Estimated: **3 h**. Low-risk refactor; tests + Playwright catch
regressions.

### PR-W4-I — Live regions + reduced motion `[I:2 / E:1 / R:1]`

E1 F29 + F31 + F32.

- Toast container: `aria-live="polite"` + `role="status"`.
- Inbox badge: `aria-live="polite"` on counter element.
- Wrap Alpine `x-transition` + Tailwind `transition-*` classes with
  `@media (prefers-reduced-motion: reduce)` to disable.

Estimated: **2 h**.

### PR-W4-J — Page structure (skip-link / landmarks / heading hierarchy) `[I:2 / E:1 / R:1]`

E1 F26-F28.

- Add a visually-hidden "Skip to main content" link as the first
  focusable element in `base_app.html`.
- Audit `<main>` / `<nav>` / `<aside>` landmarks; add missing.
- Verify single `<h1>` per page; downgrade duplicates.

Estimated: **2 h**.

### PR-W4-K — Full-screen modals on small viewports `[I:2 / E:1 / R:1]`

E2 F11. Modals are centered with `max-w-xl`; on < 375px they cut
off. Make them fill viewport below `sm:` (`sm:max-w-xl
sm:rounded-lg` pattern; full-screen on mobile).

Estimated: **1 h**.

### PR-W4-L — Memory-leak follow-ups (E3 F3 + F4) `[I:1 / E:1 / R:1]`

E3 F3 (listener accumulation on swap) + F4 (kanban substatus MO not
cleaned). Wave 3 PR-3 cleaned the main acta.js listeners; this
extends to per-element MOs and Sortable instances.

Estimated: **1.5 h**.

### PR-W4-M — Vitest coverage + CONTRIBUTING.md `[I:1 / E:1 / R:1]`

E4 F7 + F10. Add `"test:coverage"` script. Create CONTRIBUTING.md
that documents the `frontend-test` compose service, the rollup
binary pitfall, and the CI workflow.

Estimated: **1 h**.

### PR-W4-N — Form polish + low-impact a11y `[I:1 / E:2 / R:1]`

E1 F2 / F5 / F12-F15 / F23 / F30. Misc form/icon polish that doesn't
fit a single coherent theme but is cheap to land in a final sweep.

Estimated: **2-3 h**.

**Running total: ~50 h for PR-W4-A → PR-W4-N.**

---

## 3. Recommended ship order

Audit's vote, with rationale:

| # | PR | When | Why |
|---|---|---|---|
| 1 | **PR-W4-A** | Immediately | 5 h infra unblocks everything downstream + protects future PRs |
| 2 | **PR-W4-D** | Right after A | Visible perf win (~160 KB gzip) is the biggest user-facing Wave 4 delta |
| 3 | **PR-W4-B** | In parallel with C / E | Playwright net catches a11y/mobile regressions before they merge |
| 4 | **PR-W4-C** | After B starts | A11y P1 sweep is the biggest a11y user-impact PR |
| 5 | **PR-W4-F** | After D ships | Mobile drawer unlocks phone use; visible win |
| 6 | **PR-W4-E** | After C | Extends the a11y pattern |
| 7 | **PR-W4-G** | After F | Touch polish only matters once nav works on mobile |
| 8 | **PR-W4-I/J/K/L/M/N** | Opportunistic | Cheap, defensive, no blocking dep |
| 9 | **PR-W4-H** | When schedule allows | Refactor; lowest user impact, but eases future maintenance |

---

## 4. UAT (in-browser checks)

| # | UAT | Outcome |
|---|---|---|
| W4-U1 | Lighthouse on `/tasks/?panel=list` before/after PR-W4-D | Confirm −160 KB transfer + LCP delta |
| W4-U2 | Tab through filter sidebar + Cmd+K after PR-W4-E | No keyboard trap, all controls reachable |
| W4-U3 | Tab through create-task modal after PR-W4-C | Focus trap holds; Esc restores prior focus |
| W4-U4 | Open Acta on 375px iPhone after PR-W4-F | Hamburger reachable; drawer slides in; workspace switch works |
| W4-U5 | Drag kanban card on iPad after PR-W4-G | DnD lands; no scroll fight |
| W4-U6 | axe-core scan on key routes after PR-W4-C/E/I/J | < 5 critical issues; track remaining as Wave 5 |

---

## 5. Deferred — Wave 5 candidates

| # | Item | Source | Trigger |
|---|---|---|---|
| W4-R1 | Full colour contrast pixel measurement | E1 F24 + F25 | Design review session with Vox; needs WCAG tooling |
| W4-R2 | Mobile-optimal polish (target sizing, gesture, table mobile) | E2 F6 + F7 + F8 + F9 + F10 + F12 + F13 | Mobile traffic share crosses 10 % post-launch |
| W4-R3 | Build-tooling migration to Vite | E3 F11 | `acta.js` > 4 000 LOC or TipTap plugin set expands |
| W4-R4 | Visual regression (Percy / Chromatic) | (not in this audit) | Playwright stable for 30 days |
| W4-R5 | Per-route CSS extraction | E3 F10 | Cold-load CSS > 30 KB gzip on a single route |
| W4-R6 | Mobile-specific Cmd+K fallback (FAB-driven) | E2 F2 + Wave 3 D6 §8 | Mobile drawer ships (PR-W4-F) |
| W4-R7 | Mention picker keyboard polish | E1 F13 + Wave 3 D5 F5 | Post-PR-W4-B (specs catch regressions) |

---

## 6. Surface to future waves

Items already noted in E1-E4 reports as out-of-scope:

| # | Where | What |
|---|---|---|
| `_dashboard_inner.html` internals | E2, prior waves | Not in Wave 4 mobile scope (only frame breakage flagged) |
| `projects/_overview_panel.html` | All waves | Not touched since Wave 2 |
| `_bulk_context_menu.html` interactions | E1 partial (F8) | Bulk-action a11y deeper than current sweep |
| Settings pages | All waves | Out of scope across Wave 1-4; pick up in Wave 5 if usage grows |
| Telegram + Notification config UI | All waves | No a11y or mobile pass yet |

---

## 7. Memory hygiene (post-Wave 4)

After Wave 4 ships:

- **On PR-W4-A**: update `[[project-todo-woodpecker-ci]]` with CI
  platform choice + URL to pipeline; mark "minimum" satisfied.
- **On PR-W4-D**: close `[[project-todo-postdeploy-polish]]`'s
  TipTap code-split bullet.
- **On PR-W4-F**: update `[[project-todo-mobile-viewable]]` — change
  from "TODO" to "Stage 1 (viewable) shipped; Stage 2 (optimal)
  Wave 5".
- **On PR-W4-C/E/I/J**: add new memory `project-a11y-baseline.md`
  documenting which WCAG criteria Acta meets at this point + axe
  remaining-issue count.
- **Keep**: every TODO Wave 4 didn't directly touch.

---

## 8. Decision points for Vox

1. **Ship PR-W4-A immediately?** Audit votes yes — 5 h infra, low
   risk, unblocks every downstream Wave 4 PR's regression net.
2. **CI platform choice**: Woodpecker (per existing TODO) vs GitHub
   Actions (recommended for MVP). One decision; PR-W4-A blocks on
   it.
3. **a11y scope**: ship PR-W4-C only (P1 critical, 8-10 h) and call
   Wave 4 a11y done, or chain PR-W4-E + I + J + N for a full
   ~20 h pass? Audit votes "C only" first, then evaluate axe
   remaining issues.
4. **Mobile scope**: PR-W4-F alone (drawer, viewable) or +
   PR-W4-G + K (touch + modals, ~9 h total)? Audit votes
   "F alone" first; G+K only if users complain.
5. **PR-W4-H (acta.js split)**: ship in Wave 4 or defer? Audit
   votes defer — pure refactor, no user-visible delta. Land when
   bandwidth allows.

---

## 9. Wave 4 status

- 4 audit chunks complete (E1-E4) + baseline + this synthesis.
- Read-only methodology held: zero code touched across all 4 chunks.
- 67 findings (none P0); 14 PR bundles in §2; ~50 h to ship the
  queue.
- All deferred items logged in §5 with concrete trigger conditions.
- Wave 5 inputs surfaced in §5 + §6.

**Next decision** is Vox's. Options:

A) **Ship PR-W4-A alone** (5 h, single infra win). vitest unblocked,
   CI live, branch protection on.
B) **Ship PR-W4-A → D → C** (~18 h). Headline infra + perf + a11y
   P1.
C) **Ship A → D → C → F** (~24 h). + mobile drawer; closes the
   four highest-impact gaps.
D) **Ship full queue A → N** (~50 h). End-of-Wave-4 sweep.
E) **Pause; deploy Wave 1-3 first**, schedule Wave 4 PRs after
   harvesting feedback.

Audit's vote: **A → review → C → review → continue**. PR-W4-A is
risk-free and unblocks everything; the rest is a menu best chosen
after seeing the green CI dashboard and one a11y axe score.
