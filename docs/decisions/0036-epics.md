# 0036 — Epics: a task that collects other tasks

**Status:** accepted · 2026-10-02

## Context

ADR 0003 looked at this and deferred it. Its reasoning was that the real
need behind "sub-projects or epics" is *splitting a big thing into
pieces*, and that subtasks solve it with one nullable field instead of a
new entity. That was right, and it is still right.

But splitting is only half of the need. The other half is **collecting
work that already exists**, across projects. A billing migration touches
Backend, Mobile and Infra; a release pulls tasks from four projects; an
audit remediation is spread over everything it found. The tasks are
already filed, in the projects they belong to, and nothing in the model
says they are one effort.

`Task.parent` cannot say it:

- a task has one parent, so a subtask could never also belong to a
  grouping;
- depth is capped at one level (ADR 0007), so a collected task could not
  keep its own subtasks;
- and a subtask must share its parent's project (ADR 0007) — which is
  exactly the boundary a grouping has to cross.

Production is 18 projects and ~945 tasks, so the surface this touches is
not hypothetical: every list, count and metric in the app would have to
decide what an epic is to it.

## Decision

**An epic is a task.** `Task.kind` is `task` or `epic`. Not a new model:
of the eleven things an epic needs — description, comments, attachments,
activity, labels, assignee, status, links, search, the graph, a
reference like `BCK-214` — nine already exist on `Task`, and a separate
entity would mean growing all of them again. This closes the Initiative
layer ADR 0003 deferred: we are not building it.

**It collects through its own field, `Task.epic`, not through
`parent`.** The two are orthogonal, and that is the whole point:

- a subtask keeps its parent *and* can belong to an epic;
- epic → task → subtask works without touching the depth-1 rule, so the
  "three levels" problem never arises;
- the same-project rule on `parent` stays exactly as ADR 0007 wrote it,
  and so does the cascade in `set_task_project`, which renumbers
  subtasks into the new project *because* of that rule — an epic's child
  lives in its own project and must not be dragged anywhere;
- `epic` gets its own, different rule: same workspace, and the target
  must itself be an epic.

The design arrived at the same shape independently: the task rail draws
**Parent** and **Epic** as two separate rows. One field could not have
produced that.

**Nothing about an epic's state is typed by hand.** Progress (done over
total), the date span, and the status are all computed from the direct
children; subtasks fold into their parent rather than counting
separately. A status someone can set is a second source of truth about
the same thing, and it loses: an epic marked Done at four of fourteen is
simply wrong, and the board would believe it. Freezing an epic is
`archived_at`, which already exists and is orthogonal to status.

**Due date, size and cycle do not apply.** An epic's dates come from its
tasks, its size is the sum of theirs, and a cycle is a commitment that
belongs to the work, not to the umbrella over it.

**The whole feature is a workspace switch, on by default.**
`Workspace.epics_enabled` decides whether the Epics tab, the Epic row on
a task and the pickers exist at all. Not every team groups work this
way, and a tracker that shows an empty Epics tab forever is a tracker
with a dead tab in it. Turning it off hides the feature and refuses new
epics; it does not unpick what exists, so the work comes back intact on
re-enable. Default on, because the teams on this instance asked for it.

**Epics do not appear on the board, the table or the list.** They have
their own tab, where the useful view is epic × status — the cell that is
dark is where work is piling up. An epic sitting in the To-do column of
a kanban is a row that can never move on its own.

## Consequences

- **Every queryset that counts work has to decide.** This is the cost of
  the decision, and it is most of the work: 76 call sites outside tests
  build task querysets — dashboards, cycle metrics, the CFD, bulk
  operations, My Work, exports, the semantic neighbour search. A site
  that forgets about epics does not crash; it quietly returns a number
  that is too big by the count of epics. The audit is not separable from
  the first slice.
- **A default manager only covers part of it.** Django uses the *base*
  manager for related descriptors, so `project.tasks.all()` would not
  see a filter installed on the default one. The exclusion has to be
  explicit wherever it matters, and the manager is a safety net rather
  than the mechanism.
- **An epic takes a number in its project** (`BCK-214`), because it is a
  task and numbering is per project (ADR 0007). It lives in one project
  while collecting from the whole workspace — the project says who owns
  the effort, not where its work is.
- **The graph gets a region, not an edge.** Epics are out of the graph
  until this is built — an epic has no `blocks` / `related` / `parent`
  edges of its own, so drawing it as a node today would put an isolated
  dot on the canvas and spend payload budget on it. The design settled
  on a **container**: a tinted region around the epic's tasks, assembled
  from their rectangles plus corridors along the edges between them, and
  **broken into parts** with a shared colour when the tasks sit on
  different islands rather than stretched across half the screen. A
  region and not an edge because membership has no direction — an epic
  blocks nothing — and an arrow would read as "this, then that".
- **Every surface has to respect the switch**, not just render it: the
  rail row, the pickers, the tab, the REST and MCP fields all check it,
  and the model refuses an epic in a workspace that turned it off.
- **The REST API and MCP gain the field**, with the same validation as
  the web layer. MCP's `parent_slug` keeps its same-project rule
  untouched; `epic_slug` is a separate argument with its own.
- **"Turn into epic" is a real operation**, not a flag flip: subtasks
  become the epic's first tasks, due / size / cycle are dropped, status
  restarts at planned because it is computed from here on, and the task
  leaves the board for the Epics tab. The confirmation has to say all of
  that before it happens — including what is *kept*, since the usual
  fear is that the comments and the files go with it. It is refused
  rather than adapted in three cases: the task is already an epic, it is
  a subtask (promoting it would change its parent's board behind
  someone's back), or the workspace turned epics off. The dialog states
  them as sentences, not as a 400.
- **An epic is filled in bulk**, through the one universal endpoint
  ([0012](0012-bulk-operations.md)) rather than a dedicated route:
  `epic` is a scalar update like `status`, because the epic is a column
  on the task. The selection context menu is the everyday way an epic
  gets its tasks, so this is the path that has to be cheap, and "No
  epic" in the same menu is the same call with `null`.

## Alternatives considered

- **Initiative as its own model** — the layer ADR 0003 deferred.
  Rejected for the reason above: nine of eleven capabilities would be
  rebuilt, and in practice the thing people want to do to an epic is
  comment on it and attach a document, which is what a task already is.
- **Collecting through `parent`**, which is what this project's own
  plan said before the design existed. It needs the same-project rule
  relaxed conditionally in three places (`Task.clean`,
  `TaskSerializer.validate`, the MCP write tools), makes a subtask
  ineligible for an epic, and pushes "epic → task → subtask" into a
  second pass against the depth-1 rule. One field looked cheaper and was
  not.
- **A label.** Labels already group across projects and cost nothing.
  But a label has no progress, no owner, no description, no comments and
  no state — and "where is the billing migration" is a question about
  all five.
- **A project.** Moving the tasks into a project called "Billing
  migration" loses the project they belong to, which is the thing that
  says who maintains them afterwards.

## How the region is drawn

No polygon arithmetic anywhere. The figure is a padded rounded box per
member plus a thick rounded stroke along each edge between two of them;
the corridor following the edge's own route, rather than a hull, is what
keeps a foreign task lying between two members out of the region.

That figure is drawn twice at two paddings, and the ring between them is
cut with a `<mask>` of the part's own — white at the outer padding, black
at the inner. The first attempt painted the inner figure in the canvas
colour instead, which is simpler and wrong: canvas-coloured paint also
lands on whatever is already there, so wherever two regions met, one
erased the other's border. A mask affects only the shape it is applied
to. The mask needs explicit bounds, or it takes a region derived from the
viewport and silently clips parts far along a board thousands of units
wide.

Two things hold their size against the zoom, both for the same reason —
the region has to survive the zoom at which cards become dots:

- the ring is widened by `1 / zoom`, since a width in board units would
  be a fifth of a pixel at 15% and vanish under the cards;
- the label counter-scales by `1 / zoom`, the text equivalent of the
  `vector-effect="non-scaling-stroke"` the edges already use — and is
  then clamped to the width of the region it names, or a zoomed-out board
  ends up with a name lying across work it has nothing to do with.

**Gathering** (`G`) is dagre's own clustering, not a layout hint. Merging
the components alone hands dagre two unrelated chains in one run and it
interleaves them across the ranks, which produced an epic drawn as a band
with other people's work inside it. A compound graph with one cluster per
epic keeps the members together and everyone else out. It stays off by
default: the board's first job is what blocks what, and membership is
noise against that.

## Still open

- The exact rule for undoing "turn into epic". The design says
  "reversible while the epic has no tasks from other projects", which is
  the right instinct — once work from elsewhere has joined, demoting the
  epic would orphan it — but it needs stating as a condition the code
  can check.
- Progress percentages are shown for every epic in the picker and on the
  tab, so they come from one aggregate query, never from a loop.
