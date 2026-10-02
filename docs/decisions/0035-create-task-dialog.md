# 0035 — The create-task dialog is the task page, early

**Status:** accepted · 2026-10-02

## Context

The dialog that files a task was a stack of labelled fields: project
`<select>`, title, description, then status / priority / size on one
row, due date and assignee on another, cycle, labels, attachments. It
worked, and it carried less than half of what a task can hold. Parent,
links, repeat and the meeting a task came out of all existed on the task
page and nowhere in the dialog, so the common shape of filing work was
"create it, open it, finish it".

It also read nothing like the page it creates. The task page puts
content on the left and properties in a rail on the right; the dialog
put everything in one column, in a different order, with different
controls for the same values.

## Decision

The dialog is two columns — content left, a 280px property rail right
(design "Create Task Rethink", artboard 1a). Every property is a row;
one that is unset says "Add" rather than hiding, so the rail is also the
answer to "what can a task carry".

**It stays a projection over the same form.** `actaCreateTask()` holds
the state and writes hidden inputs under the names the POST handler
already parses — `status`, `priority`, `size`, `due_date`, `assignee`,
`cycle`, `labels`. `_create_task_post` was not touched and does not know
the selects are gone. This is the filter dock's arrangement (ADR 0019),
for the same reason: the parsing, the validation and their tests are the
part worth not rewriting.

**The rail ships as JSON, not as markup.** `build_create_task_data`
emits one `json_script` and Alpine draws the row that is open. A
workspace with two hundred labels and forty members would otherwise
render all of them into every dialog open, nearly all never looked at.

**Project moved into the header, as a combobox.** It is the one answer
the rest of the dialog depends on — members, labels and cycles all come
from its workspace — and a `<select>` of every project in every
workspace could only be scrolled, where a combobox searches name and
slug. Picking one still re-renders the whole dialog server-side, with
every other field riding along through `hx-include` and coming back as
`pre_*`, exactly as the `<select>`'s `change` did.

**The keyboard is the fast path.** `Enter` in the title moves to the
description rather than submitting — a half-written task is the common
case for a stray `Enter`. `⌘↵` creates. `⌘⇧↵` creates and reopens a
fresh dialog on the same project, which is how a batch of tasks gets
filed. Each row has a one-letter hotkey, live while focus is not in a
text field.

**Popovers are teleported and placed from the trigger rect.** The left
column scrolls and the rail scrolls, and either would clip a dropdown
drawn inside it.

## Consequences

- The pre-fill tests moved from asserting on `selected` attributes to
  asserting on the payload. That is the better assertion anyway: the
  payload is what the person ends up seeing, and markup for eleven rows
  of options no longer exists to assert on.
- A translated string can no longer be interpolated into an `x-text`
  expression — an apostrophe in Ukrainian would end the HTML attribute.
  The three strings Alpine writes itself ("Add", the backlog note, the
  project placeholder) travel in the payload instead.
- `"Tomorrow"` and `"Next week"` already existed in the catalogue as a
  cycle caption and a pager label, so the deadline presets carry a
  `msgctxt`. A shared msgid across unrelated surfaces is a translation
  bug waiting for the second language.
- Icon names that reach the page through JSON are invisible unless the
  sprite knows them, and the sprite is built by scanning templates for
  `{% lucide "…" %}`. The build script now also scans `apps/web` for
  `"icon": "…"` literals. The filter dock had been relying on its icons
  happening to appear in some template too; `list-filter` and
  `copy-check` did not, and had been rendering as nothing.
- The cycle row states the rule instead of offering a pick when the
  status is a backlog one, and submits nothing — `apply_cycle_policy`
  would clear it regardless, and a control that silently ignores you is
  worse than no control.

## The four rows that needed new server work

Seven of the eleven rows are values the create POST already accepted.
The other four did not exist anywhere in the create path:

- **Parent** and **Links** share one picker and one endpoint,
  `create_task_search`. `task_link_search` could not serve them: it is
  keyed on an existing task, and the one being filed does not exist yet,
  so scope comes from the picked project instead — the whole workspace
  for a link, the project alone for a parent, since `Task.clean` requires
  a subtask and its parent to share one and caps the depth at one level.
  An empty search box answers with the tasks that read like the title
  being typed, the same semantic lookup the similar-task hint makes.
- The link **kind** is a switch above the search rather than three rows
  in the rail. "Blocked by" and "related" are two answers to one
  question, and a rail with three link rows would be mostly empty on
  every task. Each picked task is submitted under the name of its kind,
  so the POST reads `blocked_by` / `blocks` / `related` directly. One
  task may hold only one kind — two at once is a contradiction to read
  off the graph, and the POST refuses it.
- **Repeat** is not a field on a task at all: a `RecurringTask` is a rule
  that spawns tasks. Picking a cadence creates that rule from the task
  that was just filed and adopts the task as occurrence one, the same
  adoption "Make recurring…" performs from the task page, so nothing is
  duplicated. The dialog offers four presets — daily, weekly, every two
  weeks, monthly — anchored on the task's deadline when it has one, so a
  task due Friday repeats on Fridays. The full schedule (weekday sets,
  end conditions, lead time) stays on the Recurring page, which owns it.
- **Meeting** attaches the new task to an existing `Meeting`. Its options
  ship in the payload rather than behind an endpoint: a workspace logs a
  handful of calls a week and the dialog only offers recent ones, because
  a task filed out of a call is filed right after it.

Each of these is checked server-side against what the picker could have
offered. The picker is a convenience; the rules belong to the POST.

## Still owed

The deadline row offers presets and the native date input rather than
the design's calendar with the cycle band underneath it and the "inside
cycle C14 · 4 days before it ends" footer. That calendar is a component
of its own and would be reused by the task page, so it is not part of
this dialog's change.
