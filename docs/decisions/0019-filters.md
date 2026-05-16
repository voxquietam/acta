# ADR 0019: Task Filter Architecture

**Status:** accepted
**Date:** 2026-05-16

## Context

The MVP needs scoped task views: ``/tasks/`` (All Tasks across the
user's workspaces), ``/my-work/`` (the user's personal inbox), and
``/projects/<slug>/`` (single project). All three share the same
filter dimensions — search, status, priority, label, workspace,
project, assignee, "show done" — but each has its own context (My
Work pins assignee to the current user, per-project drops the
project picker, etc).

Several constraints shape the design:

- **No SPA, no build step.** Per [0014](0014-frontend-architecture.md)
  the stack is Django templates + HTMX + Alpine + Tailwind.
- **Shareable URLs.** Every filter combination has to be expressible
  as a querystring so links to filtered views work.
- **Pagination-free task list.** Vox explicitly rejected paginated
  task tables — the team is small enough that hundreds of rows
  render fine, and pagination breaks the filter-and-scan workflow.
- **Always-visible scroll cue.** macOS auto-hides scrollbars by
  default; the UI needs to telegraph "you can scroll" without one.

## Decisions

### One form, many surfaces

Every filter input — sidebar checkboxes, the page-top assignee /
project strip, the search box — lives inside (or is associated
with) a single ``<form id="filter-form">``. Inputs placed outside
the form's DOM tree (e.g. the top strips, which sit above the
content/sidebar flex row) use the standard HTML ``form="filter-form"``
attribute to participate in form submission.

On any input ``change`` the form submits via HTMX:

```html
<form id="filter-form"
      hx-get="{{ filter_form_url }}"
      hx-target="{{ filter_htmx_target }}"
      hx-swap="innerHTML"
      hx-push-url="true">
```

``hx-push-url="true"`` keeps the URL in sync with selected filters
so every view is bookmarkable and shareable.

### Sidebar UI state is preserved, never re-rendered by HTMX

HTMX targets only the result list (``#task-list-wrapper``,
``#my-work-content``, or ``#project-view-panel`` depending on the
page). The sidebar form itself is *never* the swap target, which
means:

- Alpine state per checkbox (``selected``) survives every filter
  submit.
- The sidebar's open/collapsed state stays as the user set it.
- Server-rendered initial filter state is set once on a real page
  load and stays in sync from there.

### Reset = full page navigation, not HTMX

The Reset control is a plain ``<a href="...">`` link that triggers
a full browser navigation back to the unfiltered URL (with
preserved params like ``?view=table`` kept intact). HTMX-based
clearing was tried first but hit a race between programmatic
``input.checked = false`` and Alpine's ``x-model`` reactivity — the
form would submit before the sticky-stack classes updated.
Full-nav re-renders the sidebar from the server with a clean Alpine
init, sidestepping the race entirely.

### Multi-select via Q-OR for assignee

Assignee accepts multiple values plus the special tokens ``me`` and
``unassigned``. The filter function composes them with ``Q``:

```python
assignees = params.getlist("assignee")
q_assignee = Q()
user_ids = []
for a in assignees:
    if a == "me":
        q_assignee |= Q(assignee=request_user)
    elif a == "unassigned":
        q_assignee |= Q(assignee__isnull=True)
    else:
        try:
            user_ids.append(int(a))
        except (TypeError, ValueError):
            pass
if user_ids:
    q_assignee |= Q(assignee_id__in=user_ids)
qs = qs.filter(q_assignee)
```

This keeps the URL shape readable (``?assignee=me&assignee=3``) and
lets selected user IDs combine with the virtual ``unassigned``
filter in a single query.

### Shared context helper

``apps.web.filters.filter_sidebar_context()`` is the single source
of the sidebar's template variables. Three views call it with
different ``hide_*`` flags:

| Page          | hide_assignee | hide_workspace | hide_project |
|---------------|---------------|----------------|--------------|
| All Tasks     | yes (in strip)| no             | no           |
| My Work       | yes (implicit me) | no         | yes (in strip)|
| Per-project   | yes (in strip)| yes (scoped)   | yes (scoped) |

The helper also returns ``selected_*`` / ``excluded_*`` sets,
``available_*`` lists, and ``active_filter_count`` — used for the
bright pill next to the "FILTERS" header so it's obvious how many
filters are live.

### Tri-state filters: include + exclude via right-click

Every multi-select chip in the sidebar and every chip in the
page-top strips supports three states:

- **none** (default) — chip not active.
- **included** — left-click; corresponding ``?<field>=<value>`` adds
  matching rows. Visually brand-tint background + brand-ring.
- **excluded** — right-click (``@contextmenu.prevent``); corresponding
  ``?x<field>=<value>`` drops matching rows. Visually rose-tint
  background + rose-ring + line-through text.

Per-chip Alpine state machine is the single source of truth:

```html
<label x-data="{ state: '{% if ... %}excluded{% elif ... %}included{% else %}none{% endif %}' }"
       @contextmenu.prevent="state = state === 'excluded' ? 'none' : 'excluded';
                              $nextTick(() => document.getElementById('filter-form').requestSubmit())">
  <input type="checkbox"
         :name="state === 'excluded' ? 'xstatus' : 'status'"
         :checked="state !== 'none'"
         @change.stop="state = $event.target.checked ? 'included' : 'none'; …">
  …
</label>
```

The input's ``name`` attribute is dynamic — Alpine swaps it between
``status``/``xstatus`` so the form submits the right param without
needing two inputs per chip.

``apply_task_filters`` reads both shapes:

```python
statuses = params.getlist("status")
if statuses:
    qs = qs.filter(status__in=statuses)
excluded_statuses = params.getlist("xstatus")
if excluded_statuses:
    qs = qs.exclude(status__in=excluded_statuses)
```

A one-line hint banner at the top of the sidebar plus the same text
in each chip's ``title=`` tooltip telegraphs the right-click
affordance — without it users miss the feature.

For ``xlabel`` the implementation is a subquery: ``exclude(labels__id__in=…)``
would drop a task if *any* of its labels matched; the intent is "drop
tasks that carry this label at all", so we resolve the matching task
ids in a subquery and ``.exclude(id__in=…)``.

``xassignee`` reuses the same ``me`` / ``unassigned`` / ``<int>``
parsing as the include path.

### Smart per-column sort

Clicking a column header in the table view sets ``?order=<key>`` (or
``?order=-<key>`` for descending) and cycles asc → desc → none on each
subsequent click. ``apps.web.filters.apply_task_ordering`` maps the
key to a logical clause set:

- **status** → ``Case(planned → 0, todo → 1, in-progress → 2, in-review → 3, done → 4)``
  — workflow order rather than alphabetical.
- **priority** → two-stage: a ``no-priority-last`` flag (always asc)
  followed by ``priority`` field in the user's chosen direction.
  ``NO_PRIORITY (0)`` always sinks to the bottom in both asc and desc;
  meaningful priorities 1–4 sort urgent → low (asc) or low → urgent
  (desc).
- **title** / **project** → ``Lower(F("..."))`` for case-insensitive
  alphabetical sort.
- **assignee** → ``first_name``, ``last_name``, ``username`` with
  ``nulls_last`` so unassigned tasks sink to the bottom both ways.
- **id** → ``(project__slug_prefix, number)`` so cross-project lists
  stay grouped by project (``AUD-1, AUD-2, … AUD-205, MYP-1, …``)
  rather than interleaving identical per-project counters.
- **size**, **due** → direct field with ``nulls_last``; ``-priority``
  as secondary key on ``due`` so equal-deadline rows fall back to
  urgency.
- **updated** → ``F("updated_at")`` direct.

Sort columns are whitelisted (``SORTABLE_COLUMNS``) so unknown keys
fall back to the page default. Kanban view skips ``apply_task_ordering``
and keeps the fixed ``(status, -priority, -updated_at)`` so column
groupings stay coherent.

Header template uses two simple template tags — ``{% sort_url request "key" %}``
builds the next-state URL preserving every other querystring param,
``{% sort_indicator request "key" %}`` returns ``↑`` / ``↓`` / ``""``
for the active state.

### Done shown by default — per-user setting later

``apply_task_filters`` accepts ``default_show_done`` (default ``True``).
Every page passes the default today, so done tasks are visible
everywhere unless the user narrows the ``status`` filter. The flag is
kept as a seam: future work will add a per-user ``hide_done_by_default``
boolean on ``User`` plumbed through this argument, so a user who
prefers an "only active work" list can flip it once in their settings
and have it apply to All Tasks, per-project list, and My Work.

The earlier "Show done" toggle in the sidebar was removed — it
duplicated the existing status checkbox set (selecting only ``to-do``,
``in-progress``, ``in-review`` is equivalent to "hide done"). The
toggle pattern is reserved for the future Archive feature where it
will gate an orthogonal axis.

### Sticky-stack for the in-sidebar project list

The project list can grow to dozens of rows. Inside its
``max-h-64 overflow-y-auto`` container, **selected rows** get
``position: sticky; top: 0; bottom: 0`` plus an opaque
``bg-zinc-900`` so they pin at whichever scroll edge they would
otherwise leave. Multiple pinned rows compete for the same edge —
JS in ``static/js/acta.js`` (``updateStickyStack``) assigns z-index
asymmetrically so the row *closest to the visible area* always
paints on top:

- pinned at top: ``z = 10 + idx`` (later DOM wins)
- pinned at bottom: ``z = 10 + (total - idx)`` (earlier DOM wins)

JS reads ``input.checked`` directly rather than the
``:data-selected`` attribute so it never lags Alpine's microtask
queue. Recomputed on scroll and after each toggle (via a custom
``sticky-row-toggled`` event dispatched on input change).

Documented in
``~/.claude/projects/.../memory/reference_sticky_stack_pattern.md``
for reuse on other multi-select lists later.

### Page-top strips, no horizontal sticky-stack

Assignee (on All Tasks / per-project) and Project (on My Work) live
in a horizontal scrollable strip above the content. A **sticky
horizontal stack** was prototyped (Slack-style avatar group with
sticky offsets) and dropped — the compact / full-width transition
during scroll caused layout cascades that fought back through every
fix.

The final design is a plain ``overflow-x-auto`` flex row of chips
with two overlays:

- **Edge fade gradients** (zinc-950 → transparent) on left / right
  hint that content is scrollable on that side.
- **``+N`` counters** absolutely positioned over each edge show how
  many chips are scrolled off-screen on that side.

Both overlays use ``opacity: 0`` by default and CSS transitions
keyed to ``data-overflow-left`` / ``data-overflow-right`` attributes
that JS toggles on the wrapper. Absolute positioning means appearing
/ disappearing never shifts layout.

The native scrollbar is hidden (``.scrollbar-none``) because a 6px
bar visually overpowers a single-line strip.

### Pagination removed; scroll inside the panel

``AllTasksView`` no longer has ``paginate_by``. The full filtered
set renders into one ``<div>`` that scrolls vertically inside the
content column. To keep the panel's ``rounded-lg`` corners visible
at every scroll position, the **overflow lives on the panel itself**,
not on an outer wrapper:

```html
<div class="bg-zinc-900 border border-zinc-800 rounded-lg
            overflow-auto flex-1 min-h-0">
  <table class="w-full text-sm table-fixed min-w-[900px]">
    …
  </table>
</div>
```

The parent (``task-list-wrapper`` / ``project-view-panel``) is a
``flex flex-col`` container with definite height so the panel's
``flex-1 min-h-0`` correctly fills available space.

### In-flow sidebar, not floating

An earlier iteration made the sidebar ``position: fixed`` to the
right edge with vertical centering. That created two problems:

- The sidebar's top no longer aligned with the task panel's top —
  there was a visible offset.
- Viewport changes (dev-tools open/close) re-ran the centering math
  and the sidebar visibly jumped.

The current design puts the sidebar in the flex row beside the
content as a flex sibling. They share top/bottom edges by default.
Sidebar height ``max-h`` is the row's height. Cleaner DOM, no
positioning math.

## Consequences

- **Same form contract everywhere.** Adding a new filter dimension
  is "render an input with ``name=foo`` (or ``x<foo>`` for exclude)
  somewhere inside or associated with ``#filter-form``, add a handler
  to ``apply_task_filters()``" — no new endpoints, no new HTMX paths.
- **Exclude is symmetric to include.** Anywhere the UI exposes
  ``name=foo`` via a chip it can also expose ``xfoo`` by swapping the
  attribute on right-click; the backend handles both shapes the same
  way.
- **Sort is whitelist-driven.** New sortable columns require both a
  case in ``apply_task_ordering`` and an entry in ``SORTABLE_COLUMNS``;
  unknown keys silently fall back to the page default rather than
  crashing.
- **URL is the source of truth.** Browser back/forward, bookmarks,
  and link-sharing all work for free.
- **Sidebar reactivity is local.** Each row's Alpine state is the
  single source of truth for its own selected class. No global
  store, no OOB swaps needed for sidebar state.
- **Large team scaling.** With many projects (~32 today, growing)
  the in-sidebar sticky-stack keeps selected rows visible during
  scrolling. With many users (~15 today) the assignee strip's ``+N``
  counters keep the picker compact.
- **No pagination.** If we ever hit thousands of tasks the single
  scrollable block will need a different strategy (virtual
  scrolling or progressive loading). Today's scale (low hundreds)
  is well within what the browser handles fluidly.
