# 00 — Wave 3 baseline (frontend)

> **Wave 3 / Chunk A** — read-only project-wide audit, third pass.
> Date: 2026-05-30. Branch: `dev`. HEAD: `557e36f` (18 commits ahead
> of prod `c514014`, 930 tests pass).
> Sources: 5 parallel Explore agents (D2 acta.js, D3 filter sidebar
> template, D4 row partials, D5 TipTap + attachments, D6 Cmd+K
> palette) + LOC + bundle size inventory.
> Purpose: anchor Wave 3's findings on the actual frontend surface
> area and bundle weights. **No code changed.**

---

## 1. JS / CSS bundle inventory

| File | LOC / Bytes | Notes |
|---|---:|---|
| `static/js/acta.js` | 3 331 LOC / **145.4 KB** | hand-written, single bundle, SPA brain |
| `static/js/description_editor.bundle.js` | 128 LOC / ~500 KB build | TipTap editor (built) |
| `static/js/reactions.bundle.js` | 4 LOC | tiny shim |
| `static/css/main.bundle.css` | 1 LOC (minified) / **111.4 KB** | Tailwind build |
| `static/css/dashboard.css` | 323 LOC | now in Tailwind content scan (Wave 1 PR-7) |

Wave 1 baseline measured `acta.js` at 3 244 LOC; current 3 331 is +87
from Wave 1 PR-4/5/8/10 fixes. No churn since 2026-05-29 19:48.

## 2. Heaviest templates (top 15)

| Template | LOC | Wave 3 chunk |
|---|---:|---|
| `_filters_sidebar.html` | 661 | D3 ✓ |
| `base_app.html` | 594 | (not in scope) |
| `_dashboard_inner.html` | 536 | (not in scope) |
| `_command_palette.html` | 501 | D6 ✓ |
| `projects/_timeline.html` | 455 | (not in scope) |
| `projects/_task_context_menu.html` | 396 | (touched by D4 cross-ref) |
| `projects/_overview_panel.html` | 391 | (not in scope) |
| `accounts/settings.html` | 328 | (not in scope) |
| `projects/_bulk_context_menu.html` | 272 | (not in scope) |
| `_create_task_modal.html` | 265 | (not in scope) |
| `projects/list.html` | 247 | (not in scope) |
| `projects/_links_panel.html` | 228 | (not in scope) |
| `workspaces/_settings_labels.html` | 227 | (not in scope) |
| `projects/_kanban.html` | 224 | D4 cross-ref |
| `projects/_table_row.html` | 202 | **D4 ✓** |
| `projects/_task_card.html` | 161 | **D4 ✓** |
| `_task_row.html` | 150 | **D4 ✓** |

## 3. Per-row payload anchors (Wave 2 M1/M3 carry-over)

| Panel | KB / row on ksu24 | × 260 tasks |
|---|---:|---:|
| `_table_row.html` (table) | 5.2 KB | 1.3 MB |
| `_task_card.html` (kanban) | 2.3 KB | 604 KB |
| `_task_row.html` (list × 5 axes) | **14.4 KB** | **3.7 MB** ← Wave 3 target |

## 4. Wave 3 chunk reports

| # | Chunk | Report | LOC | Findings |
|---|---|---|---:|---:|
| D2 | acta.js | `01-acta-js.md` | 782 | 8 |
| D3 | _filters_sidebar.html | `02-filter-sidebar-template.md` | 644 | 10 (8 ✓, 3 ⚠) |
| D4 | row partials | `03-row-partials.md` | 413 | 9 (incl. headline 4-fix bundle) |
| D5 | TipTap + attachments | `04-tiptap-attachments.md` | 682 | 9 |
| D6 | Cmd+K palette | `05-cmd-k-palette.md` | 648 | 12 |

**Total finding count: ~48.** All low-to-medium severity. 0 P0
correctness bugs. Synthesis in `99-wave3-backlog.md`.

## 5. Deferred — frontend measurements (M-series cont'd)

Browser-side measurements still need Vox + a Lighthouse / DevTools
session.

| # | Item | Why | Trigger |
|---|---|---|---|
| W3-M1 | Lighthouse on `/tasks/?panel=list` ksu24 | quantify Wave 3 PR-1 perf win in real Lighthouse score | post PR-1 land |
| W3-M2 | `pageCache` memory footprint | acta.js D2 §6 estimate is 10 MB cap; verify with DevTools heap snapshot | tab open > 30 min |
| W3-M3 | Timeline Gantt render on 500+ tasks | D2 §2 marked unknown above 100 | once a workspace crosses 500 |
| W3-M4 | TipTap mount cost on cold load | D5 §7 estimate is 200 ms first paint | next perf pass |
| W3-M5 | Cmd+K keystroke latency | D6 §6 marked "needs measurement"; estimate < 150 ms | post Lighthouse pass |

## 6. Wave 3 status

- 5 audit chunks complete (D2-D6) + this baseline.
- Read-only methodology held; no code touched.
- All 5 reports include file:line refs throughout.
- Wave 3 synthesis (`99-wave3-backlog.md`) ranks PRs by impact /
  effort / risk and pulls Wave 4 candidates forward.
