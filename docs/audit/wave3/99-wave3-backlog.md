# 99 — Wave 3 synthesis + prioritized backlog

> **Wave 3 / Chunk G** — final pass.
> Date: 2026-05-30. Read-only. **No code changed.**
> Consolidates findings from chunks D2, D3, D4, D5, D6 into a
> ranked fix queue.

---

## 1. Bottom line

Five per-surface audits on the frontend (`acta.js`, filter sidebar
template, row partials, TipTap + attachments, Cmd+K palette).

**Wave 1 + Wave 2 invariants all hold** at the template / JS layer:
- ADR 0024 (custom history router) holds — D2 §5 verified all 7
  checks.
- Wave 2 C7 SSE drift table holds — D2 §3 confirms zero rename drift
  across 14 event types.
- Wave 1 PR-4 `actaForceApplySelfEvent` opt-in present on all 9
  inline cells — D4 references, unbroken.
- Wave 1 PR-1 `{% with labels=task.labels.all %}` cache present in
  all 3 row partials — D4 verifies.

**Wave 3 finds two real engines for visible perf wins**:
- **The list view byte-shave (D4)**: a ~5 h focused PR (F1+F2+F3+F4
  in `03-row-partials.md`) cuts ~1.6 MB off `?panel=list` on ksu24
  (3.7 MB → 2.1 MB, **~43 %**). Followed by an optional Lucide
  sprite PR for another −250 KB on every panel.
- **Polish-level memory leaks + silent failures (D2 + D6)**: ~3 h
  total across 4 findings. None observable today; all
  defensive-future.

**Effort for the headline PR (PR-1 below): ~5 h.** Effort for the
full queue PR-1 → PR-8: **~17 h** spread.

**Out of scope by design:**
- A11y full pass (D3 + D6 surface gaps) — Wave 4 / R-list.
- `acta.js` 13-file split (D2 §7) — Wave 4 long-term R4.
- TipTap code-split (D5 §7) — Wave 4 alongside the split.

---

## 2. PR queue (proposed execution order)

Each entry is small + focused. **Impact** (1-5) = perceptible user
value or developer-velocity. **Effort** (1-5) = developer-hours.
**Risk** (1-5) = likelihood of regression.

### PR-1 — List view byte-shave bundle `[I:4 / E:3 / R:2]`

The single headline user-visible Wave 3 outcome. Bundles 4 D4
findings:

- **D4 F4** (`03-row-partials.md`): axis-scoped `data-*` attrs.
  Only emit filter attributes the *active* axis (deadline / status /
  priority / assignee / project) reads on `_task_row.html`. Hidden
  axes today waste ~80 % of their data bytes. Δ ≈ **−1.2 MB** on
  the 260-task list view.
- **D4 F1**: pre-compute status dot colour classes. The 6-branch
  conditional in `_task_row.html`/`_task_card.html` runs per row
  for the same `task.status` value — move the lookup into a
  context annotation. Δ ≈ −300 KB.
- **D4 F2**: extract the 5-branch priority icon into a
  `_priority_icon.html` partial. Δ ≈ −60 KB.
- **D4 F3**: route both row templates through a single
  `_assignee_avatar.html` partial. Δ ≈ −80 KB.

Estimated: **5 h** (including a `?panel=list` payload-bound test
tighten and a quick Lighthouse before/after on Vox's browser).
Expected delta: **−1.5 to −1.7 MB / ~43 % of `?panel=list`**.

Locks in `[[project-todo-all-tasks-lazy-panels]]` headline scope.

### PR-2 — Lucide sprite extraction `[I:3 / E:2 / R:2]`

D4 F6 (Wave 1 R6 deferred). Replace inline `<i data-lucide="...">`
SVGs across all 3 row partials + the kanban / table headers with a
single `<symbol id="lucide-...">` definition near `base_app.html` and
`<svg><use href="#lucide-..."/></svg>` everywhere else.

Δ ≈ **−250 KB** on `?panel=list`, smaller on the other panels but
also positive. Risk: cache invalidation on icon set change — mitigate
with a build-time content hash.

Estimated: **2 h**. Bundle with PR-1 if Vox prefers a single
"byte-shave week"; ship separately if she wants to deploy PR-1 first.

### PR-3 — Memory-leak + missing-listener cleanup `[I:2 / E:1 / R:1]`

- **D2 F1** (`01-acta-js.md`): strip / sticky-stack / scroll-fade
  listeners accumulate on element swap. Practical impact minimal
  today (browser GC handles it), but a `MutationObserver` watching
  the parent could rebind on swap.
- **D2 F2**: kanban column `MutationObserver` reference not
  cleaned. `disconnect()` on column body removal.
- **D2 F3**: timeline deadline patch logs to console only — wrap in
  a `actaToast` so a real user sees the failure.

Estimated: **1.5 h**. Low risk because none of these change
observable behaviour today.

### PR-4 — Silent network errors → toast surface `[I:2 / E:1 / R:1]`

D6 F8 + D2 F3 (some overlap with PR-3). Cmd+K palette network
errors are silently dropped today; the timeline patch route same.
Single `actaToast("…")` wrap on the catch path.

Estimated: **1 h**. Small but visible UX win on a flaky network.

### PR-5 — Cmd+K + filter sidebar a11y `[I:2 / E:2 / R:1]`

Minimum-viable a11y for the two heaviest interactive surfaces.

- **D6 F5 / F6 / F9** (`05-cmd-k-palette.md`): `role="listbox"`,
  `aria-selected`, `aria-live` on result list; visible-on-focus
  cursor.
- **D3 F-acc** (`02-filter-sidebar-template.md`): `aria-label` /
  `aria-expanded` on the rail toggle buttons and date inputs.

Estimated: **2 h**. Defers the full a11y pass to Wave 4 but unblocks
keyboard-only users on the two most-used interactive surfaces.

### PR-6 — Remove emoji-picker package + audit deps `[I:1 / E:1 / R:1]`

D5 F1: `package.json` carries emoji-picker packages no code
imports. Drop them; re-build TipTap bundle.

Estimated: **30 min**. Δ ≈ **−50 KB** on `description_editor.bundle.js`.

### PR-7 — Image alt-text editing `[I:2 / E:2 / R:1]`

D5 F4: TipTap inserts images without an alt-text edit affordance.
Add a small popover on focused image node — input + save / Esc.
Pure UX, no backend change.

Estimated: **2 h**.

### PR-8 — Test gaps: Alpine + palette regression `[I:1 / E:2 / R:1]`

- **D6 F11**: no client-side Alpine tests for the palette. Add
  Playwright smoke (3-4 tests: open, search, select, escape).
- **D5 F5**: mention picker keyboard edge case (escape mid-select).
  One test.

Estimated: **2 h**. Requires Playwright already in CI (Wave 4 / I-list?
verify before scheduling).

### PR-9 — Documentation pass (D2 docstrings + comment fixes) `[I:1 / E:1 / R:1]`

Sweep:
- **D2 F7**: `promoteTask` style consistency comment.
- **D2 §4**: 16 `window.acta` exports — add a brief block-comment
  at the top of `acta.js` mapping them (referenced by D2 §4 table).
- **D6 F12**: comment claim "no bundle" outdated — fix.
- **D6 F1 / F2**: cursor wrap + icon cache scope docs.

Estimated: **1 h**. Pure docs.

**Running total: ~17 h for PR-1 → PR-9.**

---

## 3. UAT (in-browser checks)

PR-1 needs a UAT slot — that's the only PR with visible-to-user
delta:

| # | UAT | Outcome |
|---|---|---|
| W3-U1 | Lighthouse on `/tasks/?panel=list` ksu24 before / after PR-1 | Confirm payload + LCP delta |
| W3-U2 | All 5 list axes still filter correctly post axis-scoped `data-*` | Regression guard against PR-1 |
| W3-U3 | Lucide icons render identically post PR-2 sprite | Visual diff vs. main |

All three fit in a 30-min UAT session after PR-1 / PR-2 land.

---

## 4. Deferred — Wave 4 candidates

| # | Item | Source | Trigger |
|---|---|---|---|
| W3-R1 | `acta.js` 13-file split | D2 §7 (Wave 1 R4) | Vox's bandwidth for a 1-day refactor |
| W3-R2 | Full a11y pass (focus traps, screen-reader walk) | D3 + D6 surface gaps | Wave 4 / accessibility chunk |
| W3-R3 | Search ranking on Cmd+K (relevance > recency) | D6 F4 | Vox decision; today is "recency only" |
| W3-R4 | Recents workspace-scoped | D6 F7 | When a multi-workspace user surfaces the bug |
| W3-R5 | TipTap code-split / lazy-load | D5 §7 | Bundle weight crosses 600 KB |
| W3-R6 | Mobile / touch fallback for Cmd+K | D6 §8 | When mobile audit chunk runs |

---

## 5. Surface to Wave 4

Items that belong in Wave 4's infra / a11y / mobile passes:

| # | Wave 4 chunk | What |
|---|---|---|
| I-list | Playwright in CI | PR-8 prerequisite; check current state of `vitest` config |
| A11y | Full a11y audit | D3 + D6 surface gaps as inputs |
| Mobile | Touch + viewport audit | D6 mobile gap; row partials touch targets |
| Bundle | Frontend bundle split + code-split | D2 §7 + D5 §7 inputs |

---

## 6. Memory hygiene

After Wave 3 ships:

- **Update on merge of PR-1**: close
  `[[project-todo-all-tasks-lazy-panels]]` (axis-scoped data
  was the open scope after Wave 1 lazy panels).
- **Update on merge of PR-2**: refresh
  `[[reference-tailwind-rebuild-on-new-classes]]` with a note
  about sprite cache busting.
- **Update on merge of PR-7**: close part of
  `[[project-todo-editor-images]]` (alt-text edit was the headline
  missing affordance).
- **Keep**: every TODO Wave 3 didn't directly touch.

---

## 7. What this audit did NOT cover

For transparency:

- **`base_app.html` (594 LOC)** — the app shell. Not in Wave 3
  scope; revisit when the SPA brain split (D2 §7) runs.
- **`_dashboard_inner.html` (536 LOC)** — Wave 1 B4 covered the
  Python side; the template internals are deferred.
- **`projects/_timeline.html` (455 LOC) + initTimeline (acta.js)**
  — structural read only in D2; the Gantt math is a Wave 4 chunk.
- **`projects/_overview_panel.html` (391 LOC)** — not in scope.
- **Bulk context menu (272 LOC) + bulk operations templates** —
  flagged in Wave 1 A §4 as recently-modified hotspot; defer.
- **Settings + workspaces UI** — out of Wave 3 scope.

---

## 8. Decision points for Vox

1. **PR-1 + PR-2 bundle or separate?** PR-1 alone is ~5 h and
   −43 % on the heaviest panel; PR-2 adds ~2 h and −250 KB more
   on every panel. Audit recommends shipping PR-1 first to get
   the win observable; PR-2 as a follow-up once Vox sees the
   shape of the sprite diff.
2. **PR-3 timing**: ship now (low risk, defensive future) or wait
   until a real complaint surfaces? Audit votes ship now —
   listener-leak fixes age poorly.
3. **PR-5 a11y minimum**: this is a partial pass deliberately;
   full a11y is Wave 4. OK to commit to "minimum" framing?
4. **PR-8 Playwright dependency**: confirm Playwright already
   wired in CI before scheduling. If not, PR-8 needs an infra
   chunk first.

---

## 9. Wave 3 status

- 5 audit chunks complete (D2, D3, D4, D5, D6) + this synthesis
  + baseline.
- Methodology held: read-only, per-chunk report, no code changed.
- ~48 findings, 9 PR bundles in §2, ~17 h to ship the queue.
- All deferred items logged in §4 with concrete trigger conditions.
- Wave 4 inputs surfaced in §5.
- Memory cleanup plan in §6.

**Next decision** is Vox's. Options:

A) **Ship PR-1 alone** (~5 h, single visible win). Lighthouse
   before/after on ksu24.
B) **Ship PR-1 + PR-2 + PR-3 + PR-4** (~9.5 h). Visible perf +
   silent-error polish in one batch.
C) **Ship the full queue PR-1 → PR-9** (~17 h). End-of-Wave-3
   sweep.
D) **Pause; deploy Wave 1+2 first**, schedule Wave 3 PRs after
   harvesting feedback.
E) **Start Wave 4 audit** (infra / a11y / mobile / bundle split)
   without shipping Wave 3 PRs.

Audit's vote: **A → review → B → C**. PR-1 is the headline win
and clean to ship alone. Once it lands, the other 8 are
opportunistic.
