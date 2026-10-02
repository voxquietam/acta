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
- **The graph gets a second edge kind.** It already draws `parent`;
  epic membership is a different relationship and must not be drawn as
  if it were hierarchy.
- **Every surface has to respect the switch**, not just render it: the
  rail row, the pickers, the tab, the REST and MCP fields all check it,
  and the model refuses an epic in a workspace that turned it off.
- **The REST API and MCP gain the field**, with the same validation as
  the web layer. MCP's `parent_slug` keeps its same-project rule
  untouched; `epic_slug` is a separate argument with its own.
- **"Turn into epic" is a real operation**, not a flag flip: subtasks
  become the epic's first tasks, due / size / cycle are dropped, and the
  task leaves the board for the Epics tab. The confirmation has to say
  all of that before it happens.

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

## Still open

- The exact rule for undoing "turn into epic". The design says
  "reversible while the epic has no tasks from other projects", which is
  the right instinct — once work from elsewhere has joined, demoting the
  epic would orphan it — but it needs stating as a condition the code
  can check.
- Progress percentages are shown for every epic in the picker and on the
  tab, so they come from one aggregate query, never from a loop.
