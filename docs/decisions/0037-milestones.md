# 0037 — Milestones: a date the work aims at

**Status:** accepted · 2026-10-06

## Context

Acta can say what a piece of work *is part of* and *when it runs*, but it
cannot say what a team has **committed to by a date**.

An epic (ADR 0036) collects work across projects and takes its dates,
size and progress from the tasks it holds — it deliberately has no date
of its own. A cycle (ADR 0027) is a workspace-wide cadence, a window of
time with no opinion about which work belongs in it. A task has three
dates, but they describe one task, not a promise.

So "the demo is on the twelfth", "search goes GA on the fourth of
December", "billing is cut over before the quarter ends" live nowhere.
People wrote them into task titles and project descriptions.

We designed this twice before settling. The first attempt was a
**phase**: a span of one project, with a start and an end, state derived
from today against the window. It produced good screens — bands behind
the timeline bars, gaps between stages, overlap made visible — and it was
wrong anyway, for two reasons. It could not express a date several
projects aim at, which is the case that actually hurt; and a span is not
what the rest of the industry calls a milestone, so every tooltip,
countdown and chart would have had to invent its own vocabulary. GitHub,
GitLab, Jira, Linear and Asana all mean the same thing by the word: a
point.

## Decision

**A milestone is a point: one target date by which something must be
true.** Not a range. It has no start, no duration, and no notion of being
"in progress" — only of being reached or missed.

**It is scoped to one or more projects.** One project is a local
checkpoint that only that project's plan shows. Four projects is a shared
commitment that each of them shows, each seeing its own slice and the
total beside it. Scope is a property of the milestone, not a second kind
of entity — this is the shape GitLab reaches with project and group
milestones, with the two collapsed into one.

**Membership is explicit and lives on the task.** A task names at most
one milestone, and only one whose scope includes that task's project.
Nothing is inferred: a task due before the date is *not* thereby part of
the commitment, and deriving membership from dates would make every
count a guess. This is how every tool named above does it, and the reason
is not laziness — a commitment is chosen.

**An epic stores no milestone.** It derives the set its tasks sit in,
exactly as it already derives dates, size, cycle and progress from them.
An epic whose work spans three milestones is normal and must read as
normal. "Set the milestone for this epic's tasks" exists as a bulk action
that writes to the tasks; it is not a field on the epic.

**Counting follows the rule the product already states on screen:**
cancelled work is not counted at all, archived work that is done counts
as done, archived work that is not done drops out. That rule is written
once, as a queryset predicate, and epics and milestones both read it.
Two near-identical expressions is how `1/3` once became `0/2`.

**Risk is derived, and it has two sides.** A task is at risk when it is
open and its own due date falls after the milestone's date — the plan
contradicts itself. And once the date has passed, every still-open task
is at risk, whatever its due date says. Only the first half is obvious,
and a milestone with only the first half looks healthy the day after it
is missed.

**Closing is an action; everything else is derived.** A person closes a
milestone — the event either happened or it did not, and no amount of
finished work decides that. Around that one stored fact: *complete* when
all counted work is done, *overdue* past the date, *due today*, else
*open*. A milestone whose work is all done says so and offers to close.
Closing with work still open asks what happens to it — move it to another
milestone, or detach it — because that is the moment the feature either
feels careful or sloppy.

## Consequences

- `Milestone` belongs to a workspace and reaches projects through a
  many-to-many. Deleting a project narrows a milestone's scope; deleting
  the last one leaves a milestone no task can join, which the admin and
  the API refuse rather than allow silently.
- `Task.milestone` is nullable with `SET_NULL`: deleting a milestone must
  not delete work. Its tasks keep everything but the milestone.
- Narrowing scope on a milestone that already holds tasks from a dropped
  project is a destructive edit. It detaches those tasks, and it says how
  many before it does.
- The epic rule costs convenience at entry: a task added to an epic gets
  no milestone by itself. Three affordances pay it back — a prefill when
  the task's project has exactly one open milestone, a bulk action from
  the epic, and a reverse "add work" picker on the milestone itself.
- Two reports keep membership honest, both read off data we already hold:
  work in scope due before the date with no milestone, and work that
  blocks this milestone's tasks without being in it. The second is the
  valuable one — it finds a hole in the plan rather than a slip of the
  hand. Both are suggestions; neither ever joins a task to a milestone.
- We lose what the span gave us: no bands behind the timeline, no gaps,
  no overlap, no "days left in the stage". A milestone draws as a marker
  across the chart, the way MS Project, Linear and Asana draw it.

## Addendum, 2026-10-06 — the burndown has a home

The burndown lives on the milestone's own page and nowhere else, and it
is drawn from the activity log: ``task.milestone_changed`` gives the
scope line (when each task joined and left), ``task.status_changed``
gives the remaining line, exactly the replay the cycle burndown already
uses (ADR 0026) and with no snapshot table. Three lines and a
projection: remaining, scope, the straight line to zero on the date, and
today's pace carried forward — which is what the verdict sentence reads
out loud. A milestone with nothing attached draws no chart, because zero
of zero is not a hundred per cent.

## Still open

- Whether "cycle" survives alongside milestones. A task would otherwise
  carry three time-shaped fields — its own dates, a cycle, a milestone —
  and three is the point where people stop filling any of them in.
