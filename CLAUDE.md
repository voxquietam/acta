# CLAUDE.md — Working Conventions for Acta

This file is read by Claude on every session in this repo. It records
project-specific conventions, agreed-upon code style, and gotchas. Update
it whenever a new rule comes up so future sessions follow it.

## Language

- All committed artifacts (code, docs, ADRs, commit messages, comments,
  `docs/`) are in **English**.
- Conversation with the user, planning notes, and scratch files outside
  the repo stay in Russian/Ukrainian.

## Code Style

### Django model fields — multi-line with trailing comma

Every field declaration that takes one or more arguments must put each
argument on its own line, with a trailing comma after the last one.

```python
# Correct — even with a single argument
name = models.CharField(
    max_length=60,
)

slug = models.SlugField(
    max_length=60,
    unique=True,
)

workspace = models.ForeignKey(
    "workspaces.Workspace",
    on_delete=models.CASCADE,
    related_name="labels",
)
```

```python
# Wrong — single-line with arguments
name = models.CharField(max_length=60)
slug = models.SlugField(max_length=60, unique=True)
```

Zero-argument calls stay on one line:

```python
body = models.TextField()       # OK — no args to expand
created_at = models.DateTimeField(
    auto_now_add=True,
)                                # OK — has an arg
```

Applies to: `models.CharField`, `models.TextField`, `models.ForeignKey`,
`models.ManyToManyField`, `models.DateTimeField`, all field types.

### Iterable literals — multi-line with trailing comma

Every list, tuple, or dict literal that holds at least one item is
multi-line: opening bracket, each item on its own line with a trailing
comma, closing bracket on its own line. Applies even to single-item
iterables.

```python
# Correct
list_display = (
    "slug",
    "title",
    "status",
)
ordering = (
    "-updated_at",
)
fields = [
    "project",
    "number",
]

# Wrong
list_display = ("slug", "title", "status")
ordering = ("-updated_at",)
fields = ["project", "number"]
```

Empty iterables (`[]`, `()`, `{}`) stay on one line. List/dict
comprehensions and generator expressions stay single-line unless the
expression is genuinely long.

This rule covers admin `list_display` / `list_filter` /
`autocomplete_fields` / etc., `Meta.ordering`, `Meta.constraints`,
`Meta.indexes`, `models.Index(fields=...)`,
`models.UniqueConstraint(fields=...)`, validator lists, choices lists —
everything.

**Use lists (`[...]`), not tuples (`(...)`), for class attributes that
hold sequences.** Reason: black's "magic trailing comma" preserves
multi-line formatting for `[a,]` but cannot distinguish a multi-line
intent on `(a,)` because the trailing comma there is syntactically
required for a single-element tuple. Using lists keeps black aligned with
the multi-line rule.

```python
# Correct — list, multi-line preserved by black
readonly_fields = [
    "next_task_number",
]

# Wrong — black collapses single-element tuple to one line
readonly_fields = ("next_task_number",)
```

Django admin and Meta accept both lists and tuples interchangeably for
`list_display`, `ordering`, `search_fields`, etc.

### `help_text` on every model field

Every Django model field must include `help_text=...` in English, on its
own line as the last keyword argument. The text describes what the field
represents — short, factual, in present tense, **no trailing period**.

```python
title = models.CharField(
    max_length=200,
    help_text="Short title shown in lists and the kanban board",
)
```

This text powers Django admin tooltips and form help, and serves as
canonical documentation for the field. Update it when semantics change.

### Docstrings — Google style on every method

Every method (including `__str__`, properties, classmethods, save/clean
overrides, helpers) gets a Google-style docstring. Same for top-level
functions. Format:

```python
def allocate_task_number(self) -> int:
    """Reserve and return the next task number for this project.

    Must be called inside ``transaction.atomic()``. The row-level lock is
    held until the surrounding transaction commits, so concurrent calls
    on the same project serialize safely.

    Returns:
        The newly reserved task number.

    Raises:
        Project.DoesNotExist: If the project row was deleted while locked.
    """
```

Sections to use when applicable: `Args:`, `Returns:`, `Yields:`,
`Raises:`, `Example:`. Skip empty sections. One-line docstring is fine
for trivial methods (`__str__` etc.) — just the summary line in triple
quotes.

### Inline comments

- Default: write none inside method bodies.
- Add an inline comment only when the *why* is non-obvious: a hidden
  constraint, an invariant, a workaround tied to a specific bug,
  behavior that would surprise a reader.
- Never write an inline comment that restates what the code does — that
  belongs in the docstring or is implicit in good naming.

### Database query discipline (no N+1)

Avoiding N+1 is a hard rule. Before declaring an endpoint, serializer
method, or `perform_*` hook done, verify it does not explode query
counts.

```python
from django.db import connection
from django.test.utils import CaptureQueriesContext

with CaptureQueriesContext(connection) as ctx:
    list(SomeModel.objects.filter(...))  # plus serializer reads if needed
print(len(ctx.captured_queries))
```

`qs.query` shows the SQL Django will run without executing it.
`CaptureQueriesContext` counts real queries during evaluation.

Active patterns to audit when writing a new view:

1. Serializer methods touching FK chains (`obj.project.workspace.name`)
   → `select_related("project__workspace")` on the queryset.
2. Loop bodies accessing FK attributes → `select_related`.
3. M2M iteration per row → `prefetch_related`.
4. `perform_create` / `perform_destroy` that read FK chains after save
   (e.g. `log_event(workspace=task.project.workspace, ...)`) → make
   sure the lookup queryset preloads them.

Workflow for any new endpoint: write it, exercise it through
`CaptureQueriesContext`, confirm the query count does not grow linearly
with the row count, fix proactively.

### i18n / translation workflow

The repo supports English (source language, default) and Ukrainian.
See `docs/decisions/0018-i18n.md` for the policy.

- Wrap every user-visible string with translation calls:
  - Python: `from django.utils.translation import gettext_lazy as _` then
    `_("Save")`.
  - Templates: `{% load i18n %}` and `{% trans "Save" %}` or
    `{% blocktrans %}…{% endblocktrans %}`.
- Internal codes (status keys, event types) must **not** be translated.
- Workflow:
  ```bash
  docker compose exec web python manage.py makemessages -l uk
  # …edit locale/uk/LC_MESSAGES/django.po…
  docker compose exec web python manage.py compilemessages
  ```
- `.po` files are committed; `.mo` files are built at deploy time and
  not committed.

### Formatting and linting

The repo uses **black** (line-length 120), **isort** (profile=black),
and **flake8** (max-line 120, max-complexity 25), matched to
`ksu24.back`'s versions for consistency. Configuration lives in
`pyproject.toml` (black, isort) and `.flake8` (flake8).

A `.pre-commit-config.yaml` wires all three as pre-commit hooks. Set up
once after cloning:

```bash
pre-commit install
```

Hooks run automatically on `git commit`. To run manually across the repo:

```bash
pre-commit run --all-files
```

## Testing Rules

### UI consistency — design tokens

When building or editing a template, stick to the tokens below. New
components that introduce off-grid values (random radii, ad-hoc grey
shades, one-off padding) erode the look-and-feel — the rule of thumb
is "if you need a value not in this table, the design needs a
decision, not a new arbitrary value."

#### Surfaces and borders

| Token              | Use                                              |
|--------------------|--------------------------------------------------|
| `bg-zinc-900`      | Panel / card / dropdown / list backgrounds       |
| `bg-zinc-800`      | Sub-panel, inline input, "raised within panel"   |
| `border-zinc-800`  | Default border on every panel-level surface      |
| `border-zinc-700`  | Input borders / hover-darker borders             |
| `rounded-lg`       | **Every** panel, card, dropdown, list, popover   |
| `rounded-md`       | Reserved for small inline buttons (CTA chips)    |
| `rounded-full`     | Avatars, status/priority dots                    |
| `shadow-lg`        | Floating overlays (dropdowns)                    |

**Hard rule:** mixing `rounded-md` and `rounded-lg` on adjacent
containers reads as inconsistency. Default to `rounded-lg`.

#### Text colours (dark theme)

| Token              | Use                                              |
|--------------------|--------------------------------------------------|
| `text-zinc-100`    | Section heading on a dark surface (rare, strong) |
| `text-zinc-200`    | Body text in inputs / textareas                  |
| `text-zinc-300`    | Default primary text on panels                   |
| `text-zinc-400`    | Subtle hover / secondary chips                   |
| `text-zinc-500`    | Section labels, metadata, muted text             |
| `text-zinc-600`    | Placeholder / empty-state ("Set deadline" etc.)  |

Italic + `text-zinc-600` is reserved for empty-state placeholders. Do
not italicise interactive elements (the `Unassign` button taught us).

#### Type scale

| Token              | Use                                              |
|--------------------|--------------------------------------------------|
| `text-[10px] uppercase tracking-wider` | Section labels in the rail / cards |
| `text-[11px]`      | Tag / chip text                                  |
| `text-xs`          | Buttons, dropdown rows, metadata lines           |
| `text-sm`          | Body, comment body, search inputs                |
| `text-base`        | H2 inline forms                                  |
| `text-2xl font-semibold leading-tight` | Page H1 (task title etc.) |

#### Spacing

- Section label → control: `mt-1`
- Inline gap (icon + text, dot + label): `gap-1.5`
- Tighter group: `gap-1`, looser group: `gap-2`
- Panel padding: `p-3` (small) / `p-4` (default rail panel)
- Section vertical rhythm: `space-y-3` (within a panel) / `space-y-6`
  (between major task-detail blocks)

#### Brand and state colours

- Primary: `bg-brand-600 hover:bg-brand-500 text-white` for primary
  CTAs; `text-brand-500` for highlights / selected-state text.
- Status palette (badges, dots) — see `_status_cell.html`:
  - `planned` → `zinc-800` / `zinc-400`
  - `to-do` → `blue-900` / `blue-300`, dot `blue-500`
  - `in-progress` → `violet-900` / `violet-300`, dot `violet-500`
  - `in-review` → `amber-900` / `amber-300`, dot `amber-500`
  - `done` → `emerald-900` / `emerald-300`, dot `emerald-500`
  - `cancelled` → `zinc-100`/`zinc-800` bg, `zinc-500` text **+ `line-through`**,
    dot `zinc-600`. Terminal "won't do" state: struck-through everywhere a
    title renders, hidden from the kanban + default lists. See ADR 0004.
- Priority palette (text + dot) — see `_priority_cell.html`:
  - 1 Urgent → `rose-400` / dot `rose-500`
  - 2 High → `orange-400` / dot `orange-500`
  - 3 Medium → `amber-400` / dot `amber-500`
  - 4 Low → `sky-400` / dot `sky-500`
  - 5 No priority → `zinc-500` / dot `zinc-700`
- Destructive hover (clear "✕"): `text-zinc-600 hover:text-rose-400`.

#### Interactive patterns

- **Empty-slot trigger** (no value set yet): dashed circle placeholder
  (`border border-dashed border-zinc-600` with `?` or icon) + muted
  text. Reads as "clickable empty slot". Used for unassigned assignee
  and would suit other empty cells.
- **Clear ("✕") affordance**: `text-zinc-600 hover:text-rose-400 text-xs
  opacity-0 group-hover:opacity-100 transition`, sitting inline next
  to the value. Requires `group` on the wrapping container. Use for
  removing optional values (due date, assignee, future fields).
- **Dropdown z-index**: `z-20` so it sits above the rail but below
  modals.
- **Dropdown autofocus pattern** (with search): `x-effect="if (open)
  $nextTick(() => $refs.search.focus())"` — open the dropdown,
  the search input gets focus on the next tick.

#### Checklist before merging a new component

1. Container: `bg-zinc-900 border border-zinc-800 rounded-lg`?
2. Section label: `text-[10px] uppercase tracking-wider text-zinc-500`?
3. Body text: one of `text-zinc-200/300/400/500/600` — no off-palette
   greys?
4. Empty state: dashed placeholder + `text-zinc-500/600`, no italic
   on interactive elements?
5. Clear / destructive affordance: hover `rose-400`?
6. If introducing a new colour or radius — is it really new, or is
   there already a token for the case?

When in doubt, **grep an existing similar component and copy its
class string verbatim** rather than guessing values.

### Run tests when you touch logic

When you modify any code that has business behavior (models, services,
serializers, views, bulk operations, activity events), you **must**
run the related tests before declaring the change done. If no test
exists for the logic you changed, **write one** in the same pass.

- Run via `docker compose exec web pytest <path>` — targeted, not the
  full suite, unless the change is cross-cutting.
- If a test fails, fix the test only if the new behavior is correct
  and intentional; otherwise fix the code.
- New test files live in `apps/<app>/tests/test_*.py`; factories in
  `apps/<app>/tests/factories.py`.

This is a hard rule. Skipping it leaks regressions into the killer
features (bulk operations, activity log, query counts).

## Process Rules

### Don't run on the user's behalf without permission

- **Tests**: ok to run when you've touched related logic (see above).
  Don't run the full suite without an explicit request.
- **Never `git commit` or `git push`** without an explicit request.
- **Never run destructive commands** (`docker compose down -v`,
  `rm -rf`, `git reset --hard`) without an explicit request.

### Read-only investigation is fine

- `ls`, `grep`, `find`, reading files, inspecting git status / log /
  diff — these are free actions.

### Commit message style

When the user asks to commit, use **Conventional Commits**:

```
<type>(<scope>): <subject>
```

**`<scope>` is mandatory on every commit** — never write `test:` /
`fix:` without a scope. If the change is genuinely cross-cutting use
a synthetic scope: `suite`, `repo`, `infra`.

Examples:

```
feat(tasks): add bulk PATCH endpoint with cascade subtask move
fix(activity): preserve actor on user delete via SET_NULL
refactor(bulk): replace per-task save loop with QuerySet.update
docs(adr): record i18n decisions in 0018
test(events): cover diff emission for every watched field
chore(deps): bump pytest-django to 4.9
```

Rules:

- **Subject line ≤ 80 characters**, lowercase, imperative ("add",
  "fix", "refactor", not "added" / "adds").
- **Mark breaking / major commits with `!` after the scope** —
  Conventional Commits' breaking-change marker. We use it whenever
  a commit:
  - introduces a migration (any `apps/*/migrations/*.py` file),
  - changes a public API endpoint shape (URL, request fields,
    response fields, status codes),
  - changes a model field (add / rename / drop / type change),
  - changes settings semantics that downstream code depends on,
  - is otherwise "major" enough that a reviewer or downstream
    deployer should pay extra attention.

  Examples: `fix(labels)!: require valid hex color, color picker
  in admin` (adds a migration), `feat(tasks)!: add bulk PATCH
  endpoint` (new public surface). The `!` does NOT replace a
  proper body explanation — it's a marker, not a substitute.
- **Body: keep it tiny.** Default to subject-only. If a body is
  genuinely needed, 1-3 short lines max — never an essay. The
  diff already shows *what* changed; the message just captures the
  *why* in a sentence. Anything longer belongs in an ADR or a TODO
  memory, not the commit log.
- **Never add a `Co-Authored-By: Claude …` trailer.** The user
  explicitly does not want it.
- Common `<type>`: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`,
  `chore`, `style` (formatting only), `build` (deps / Dockerfile),
  `ci`. `<scope>` is the affected app or area (`tasks`, `bulk`,
  `activity`, `i18n`, `admin`, `deps`, `workspaces`, `projects`,
  `comments`, `labels`, `accounts`, `web`, `adr`).
- Prefer small focused commits that touch one concern. If a single
  commit message can't fit in 80 chars, the change is probably two
  commits.

### Always preview before committing

Before running `git commit`, print to the user:

1. The list of files about to be staged (e.g. `git status -s` output).
2. The proposed commit message exactly as it will be passed to `-m`.

Wait for explicit "ок" / "commit" / "go" before running the actual
commit. This applies to every commit, no exceptions, even when the
user already said "commit it" — the message itself still needs review.

## Architecture Anchors

The full architecture is documented in `docs/decisions/` (ADRs 0001–0017).
A few high-impact rules to remember in every session:

- **ASGI only.** Production runs Uvicorn behind Caddy/nginx. WSGI
  Gunicorn is forbidden because SSE on sync workers fails catastrophically.
  See `docs/decisions/0015-real-time.md`.
- **Actor in activity log is `request.user`, always.** Never derived
  from payload state. This is the headline anti-Kaneo fix; see
  `docs/decisions/0011-activity-log.md`.
- **`log_event()`** in `apps/activity/services.py` is the only writer
  for the activity log. Called from `perform_create/update/destroy` of
  ModelViewSets. Never from signals.
- **Bulk endpoint** is a single universal `PATCH /api/v1/tasks/bulk/`
  with all-or-nothing transactionality. See
  `docs/decisions/0012-bulk-operations.md`.
- **Frontend** is server-rendered Django templates + HTMX + Alpine +
  Chart.js + sortable.js. No React, no build step. See
  `docs/decisions/0014-frontend-architecture.md`.

## Local Setup Notes

- Postgres host port is **5433** (5432 is occupied by ksu24.back).
- Web container host port is **8001** (8000 is occupied by ksu24.back's
  runserver).
- OrbStack DNS works automatically: `web.acta.orb.local`,
  `db.acta.orb.local`.

## Open Conventions

Add new rules below as they come up.
