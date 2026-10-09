# Changelog

All notable changes to Acta are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are hand-written from the Conventional Commit history for now.
Automating this with `git-cliff` is deferred until `v1.0.0`.

## [Unreleased]

### Changed

- **Milestone forecasts replay the work arriving, not only the work
  closing.** The chart already drew the scope line so that work turning
  up late read as work turning up late, and the caption already counted
  the days it moved on — and then the projection extrapolated as though
  the bucket were never topped up again. Each day of the replay window
  now settles both sides: what closed comes off the remainder, what
  joined goes back on. The two are never sampled apart, because a day
  that closed seven and took in six is one observation about that team.
  Bands come out wider and later than before, which is the correction.
  Filling a milestone in the first place is not an arrival — that work
  is already the remainder, and counting its day again would tell every
  milestone younger than four weeks that it will never finish.
- **A milestone that takes in work faster than it closes it is told so.**
  `does not converge` is the sixth reading, beside likely / coin flip /
  unlikely / date passed / not enough history. It replaces the headline
  rather than colouring it, because `0% chance` reads as "late" and late
  is a milestone that still finishes, and it prints the two paces side
  by side — closing ≈2.1 a day against ≈2.6 arriving — so the verdict is
  checkable the way the chance was. No projection is drawn, there being
  no zero to draw a line to. See `docs/decisions/0038-forecasting.md`.

### Fixed

- **The forecast's 400-day horizon was being read back as a date.** A
  simulated run still unfinished at the horizon was recorded in the same
  pile as the honest finishes, so the percentiles could report "done in
  400 days" — a number produced by the loop guard and nothing else, the
  same defect as the `0.05`-a-day floor it replaced. Percentiles are now
  taken over every run, stalled ones included, and a percentile with no
  finish behind it reports no date at all.

## [0.7.0] — 2026-10-09

### Added

- **Epics — a task that collects other tasks.** An epic gathers work from
  across the whole workspace, which a parent cannot do (a subtask must
  share its parent's project), so it uses its own field and leaves the
  subtask rules alone: a subtask keeps its parent *and* can belong to an
  epic. Its progress, dates and status are read off the tasks inside it
  and never typed by hand.
  The Epic row sits beside Parent on a task and in the create dialog. Off in a
  workspace that turns `epics_enabled` off; on by default. The REST and
  MCP list endpoints leave epics out unless `kind` asks for them. See
  ADR 0036. The **Epics** tab is one row per epic — who carries it, how
  far along, over what span, and when anything inside it last closed,
  with the quiet ones first; an epic's own page is the app's kanban over
  its tasks, with blocked ones at the top of their column. The table
  gains an optional **Epic** column (Display menu) and the list a
  group-by-epic axis.
- **The create-task dialog is two columns now.** Content on the left,
  a property rail on the right — the same shape as the task page, so
  the two read as one interface. Every property is a row and an unset
  one says "Add" rather than hiding. The project moved into the header
  as a searchable combobox (name or slug). `Enter` in the title goes to
  the description, `⌘↵` creates, `⌘⇧↵` creates and opens a fresh dialog
  on the same project, and each row has a one-letter hotkey. See ADR
  0035.
- **A task can be filed with its relations already on it.** The dialog's
  rail carries **Parent**, **Links** (blocked by / blocks / related, all
  from one picker with the kind as a switch), **Repeat** and the
  **Meeting** it came out of — none of which the create form accepted
  before, so filing work no longer means "create it, open it, finish
  it". Repeat builds a recurring rule from the new task and adopts it as
  the first occurrence, exactly like "Make recurring…" does.
- **Filters are a floating dock now.** The 48px rail and the 512px panel
  are gone; the bar at the bottom shows only the filters that are on, as
  chips, and everything else is behind "Add" (hotkey `F`). Dates became
  presets, and the dock is dropped onto the same hidden form the rail
  wrote, so links to filtered views keep working unchanged. Backlog and
  archived moved out of the filters into their own **Display** menu —
  they decide what exists, not what is shown. See ADR 0019.
- **"This already exists" suggestions.** Typing a title in the create
  dialog shows the tasks that already say something close to it — by
  meaning and across languages, so a Russian title finds its Ukrainian
  twin. Needs `ACTA_EMBEDDING_URL` (an Ollama host) and a multilingual
  model; without it the feature is simply off. See ADR 0034 and
  `manage.py backfill_embeddings`.
- **MCP: `acta_tasks_find_similar`.** The same search as a tool, and
  `acta_task_create` now asks clients to run it before filing a task.
- **The link picker suggests what looks related.** Opening it with an
  empty search box now offers the tasks that read like this one instead
  of nothing at all.
- **"Usually does this" in the assignee picker**, and in the create
  dialog beside the similar-task list. The people who took the work the
  neighbours describe — shown only when the same person turns up on at
  least two close matches, and only on a task nobody owns yet.
- **"Similar tasks use" in the label picker**, on a task and in the
  create dialog. Labels that two close neighbours carry — or one
  near-identical task — and this one does not; they disappear as soon as
  they are attached.
- **Relationship graph: project stacks.** Tasks from a neighbouring
  project fold into one card carrying its badge, how many of its tasks
  this board touches and how many of them block work here. Opens in
  place; folds again from the project chip.
- **Link tasks from the graph.** Drag a row out of the Unlinked list
  onto a card and pick the kind — blocks, blocked by, related.
- **MCP can re-parent existing tasks.** `parent_slug` is now accepted by
  `acta_task_update` (and so by `acta_tasks_bulk_update`); `null`
  promotes a subtask back to top level. Previously a parent could only
  be set while creating the task.

- **Milestones — a date with a scope, and the work that aims at it.** A
  milestone is a point, not a span: a target date, the projects it
  covers, and whatever work has been committed to it. Progress, risk,
  the per-project and per-epic slices and the burndown are all read off
  that work rather than stored, so the pages, the MCP tools and the
  admin cannot drift into giving different answers about the same date.
  A task carries its milestone from the rail, the create dialog, the
  bulk endpoint and MCP; the list and the filters group by it; the board
  draws it as a window, a column set and a lane. See ADR 0037.
- **The Plan tab** cuts the work two levels deep and draws it as one
  gantt shared by both surfaces, with milestones as markers on the
  timeline. A date says which projects it belongs to, and the plan folds
  on both renders.
- **A milestone says its chance of making the date.** The old answer was
  an average — tasks closed over days elapsed, printed as a date, which
  read as a promise it could not keep. It now replays the team's own
  recent days two thousand times and reports the share of runs that land
  on or before the target, with the median and the 85th percentile
  behind it. Five readings: likely, coin flip, unlikely, date passed,
  not enough history. Below ten closes across three weeks it refuses to
  answer, which replaced a floor that once printed "865 days after the
  date". The same machine forecasts the cycle card in My Work, where the
  pace is the workspace's because a three-day cycle has no history of
  its own. See ADR 0038.
- **The forecast shows its working.** The pace it assumed is printed
  next to the percentage, and under the chart a strip of the twenty-eight
  days it replayed — because a steady fortnight and one afternoon of
  clearing the backlog average out the same and look nothing alike.
- **It counts points where the estimates allow it.** Counting tasks
  assumes they are all the same work, and the easy ones went first. When
  both the closes and the remainder clear a coverage bar the replay runs
  on `size` instead, imputing the median where an estimate is missing.
  The page says which unit it counted in.
- **An estimate now records who made it.** `Task.size_source` is set at
  every door a size can arrive through. The channel answers it
  everywhere but MCP, where a number the person dictated and one the
  agent invented arrive identically — so the tool declares it with
  `size_from_user`, and silence is read as the agent. The MCP tools are
  told to always leave an estimate; an agent's guess carries a mark on
  the task so a person can overrule it.
- **My Work is a page about what to do next, not a list.** Four numbers,
  a ranked shortlist, what each date is owed, what is free to start, and
  a column of people, days, calls and what others did. The grouped list
  it used to be is still there, folded behind **Everything else**.
- **The project Overview opens on the dates it aims at.**
- **A workspace can retire the Ready column.** A replenishment buffer
  nobody grooms into splits the backlog in two and makes every picker a
  row longer. Turning it off moves every ready task to planned — one
  logged event per task, credited to the admin who flipped the switch —
  and the settings page names the count before the switch is flipped.
  Turning it back on does not undo the move.
- **Six themes on a two-by-three grid**: brightness down the rows, tint
  across. `light` · `paper` · `ash` and `dark` · `dusk` · `midnight`,
  picked from tiles that each paint themselves in their own theme rather
  than from a list of names.

### Changed

- **The graph's filters now remove rather than dim.** "Only matching" is
  on by default; switching it off brings the filtered-out cards back
  dimmed, for when the chain matters more than the filter.
- **Graph layout is packed per chain.** Unrelated chains are laid out
  separately and packed towards the shape of a screen, instead of dagre
  lining them all up in one row — a busy project fitted at 12% before.
- Stepping the zoom from the toolbar keeps the middle of the board in
  place instead of growing out of its top-left corner.

### Fixed

- **The dev server could serve a stale stylesheet and bundle forever.**
  The service worker cached everything under `/static/` cache-first, on
  the assumption that the filenames carry a content hash — true in
  production, false in development, where `main.bundle.css` and
  `acta.min.js` keep their names. The worker pinned whatever it saw
  first, so a soft reload kept showing it and only a hard reload got the
  new one. It now caches only where the filenames are actually hashed.
- **The export icon sat on top of the Archive tab** once the window got
  narrow enough. The topbar's right-hand cluster was allowed to shrink
  below its own content, and because its icons refuse to shrink they
  spilled leftwards over the view tabs. It keeps its width now. (The
  topbar still runs out of room below ~1500px with the sidebar open —
  the right-hand icons go past the edge rather than over the tabs.)
- **Two dock icons were rendering as nothing.** The sprite is built by
  scanning templates, so `list-filter` and `copy-check` — named only in
  Python — never made it in. The build script now scans `apps/web` for
  icon names too.

- The **Blocked** badge on a task's topbar was dark-theme only — pale
  pink text on pale pink in the light themes. Same for the error banner
  on the sign-in and sign-up pages.

- **A milestone drawn around work that had been running for months was
  told it had no history.** The forecast measured history from the
  milestone row's age rather than from the span its closes cover, so a
  container opened this morning over two months of work waited three
  weeks to be told something its own activity log already knew.
- **My Work's grouped list was drawn at zero height, past the end of the
  scroll.** The page pinned itself to the viewport so the list could own
  an inner scroll region, which worked while the list was the whole
  page; with the focus strip above it the strip ate the column. The rows
  were in the DOM and unreachable on screen.
- **Every theme but two was tuned for a ground it does not have.** The
  `-300`/`-400` accent shades are for dark surfaces and fall to about
  2:1 on light ones, so counts and badges read as decoration. Light
  themes take the `-700`/`-800` ramp, Paper and Ash one step further,
  the lifted darks one step lighter. Surface tokens were re-audited with
  them — the dark placeholder sat at 2.6:1, and a card border on
  Midnight at 1.19:1 made a card read as a smudge.
- **Two theme icons rendered as nothing.** The sprite is built by
  scanning templates for literal names, and the theme icons now arrive
  through a loop variable. The build script reads the theme list from
  Python too.
- 174 entries in the Ukrainian catalogue were `#, fuzzy` — msgmerge
  guesses that `compilemessages` discards, so the catalogue looked
  translated while the interface was English, and some guesses were
  simply wrong (the `Covers` column was labelled "overdue").

### Performance

- **One bulk operation redrew the whole board.** A bulk broadcasts an
  event per task, and each one re-walked every kanban column to rebuild
  the substatus row — fifteen tasks meant fifteen full board walks
  interleaved with fifteen card swaps. Events sharing a `bulk_id` now
  land in one pass: 980 DOM mutations over 301 ms became 565 over 93 ms.

- **Panning the graph lagged on a big board.** Every frame rewrote the
  geometry and state of every visible card, though a pan moves the stage
  and not the cards, and recomputed which cards the viewport covers. A
  pan frame is now one transform; the rest waits for the gesture to
  pause, so the cost no longer grows with the number of tasks.
- **A mouse wheel zoomed the graph 2.7x per notch.** The step was tuned
  for a trackpad pinch, where a browser reports deltas of single pixels;
  one notch of a wheel reports a hundred. Both now move about 1.2x per
  notch, and Firefox's line-based deltas are converted instead of taken
  as pixels (which had made panning crawl).

### Infrastructure

- **Redis**, for two things that assumed a single web process and said
  so nowhere. `send_event` only reached listeners held by the process
  that wrote the event, so every qcluster job was invisible to an open
  browser until it reconnected; and `django.core.cache` with no `CACHES`
  is per-process memory, which the MCP rate limiter counted in — giving
  each worker, and the stdio server, a ceiling of its own. Both are one
  shared thing now. It holds nothing durable, runs with persistence off
  and is not in the backup story. Opt-in on `REDIS_URL`; empty keeps the
  old behaviour.
- **The SSE event table is pruned nightly.** Every broadcast is persisted
  so a reconnecting browser can replay what it missed; nothing read them
  after that window and nothing deleted them. A week — generous because
  pruning past a client's last id answers `stream-reset`, which the
  browser does not handle.

### ⚠ Breaking

- Migrations: the `milestones` app, `Task.milestone`, `Task.size_source`,
  `Workspace.ready_enabled`, `Workspace.epics_enabled`, `TaskEmbedding`,
  and the epic fields. All additive.
- `requirements/base.txt` gains `redis`, so **both** images must be
  rebuilt — `web` and `qcluster` build separately, and rebuilding only
  `web` leaves the cluster crash-looping on an `ImportError`.
  `make deploy` covers it.
- Midnight's palette moved off near-black, so anyone on it sees a
  different app. Retired theme keys (`mist`, `sage`, `lilac`) migrate to
  `ash`.
- MCP `acta_task_create` / `acta_task_update` accept `size_from_user`,
  and refuse an argument they do not declare.

## [0.6.0] — 2026-10-02

Four months of work on `dev`: Claude Desktop connects to Acta over OAuth
without a bridge, the workspace moved into the URL, meetings and
recurring tasks became first-class, and a production outage drove a
round of SSE and database-connection fixes. 112 commits.

### ⚠ Breaking / migrations

Run migrations after deploying. Existing data is preserved.

- New apps: `mcp` (OAuth client / code / refresh-token tables),
  `meetings`, `recurring`. Plus `accounts.0009`, `activity.0004`,
  `comments.0004`, `notifications.0005`, `tasks.0013`,
  `workspaces.0009` — `migrate` applies them in dependency order.
- **Every URL is now workspace-scoped** (`/ksu24/projects/ST/5/`).
  Legacy paths answer `301` to the canonical form, so existing links and
  bookmarks keep working, but anything that *constructs* Acta URLs
  outside the app needs updating.
- **Workspace slugs are frozen after creation** and a set of root
  section names is reserved, so a workspace can no longer shadow an app
  route.
- MCP tool responses carry an **absolute task URL**; clients that built
  links from the slug should read the field instead.

### Added

- **MCP over OAuth 2.1** — Claude Desktop connects by pasting
  `https://actaspace.com/mcp/`: discovery, dynamic client registration,
  a consent screen and PKCE, with no Node bridge and no token to copy.
  Access tokens live an hour and refresh in place, so one connection is
  one revocable line in settings. Claude Code keeps its hand-issued
  `Token` header — both schemes resolve to the same `ApiToken`. See
  `docs/decisions/` and `docs/mcp.md`.
- **Consent screen names the account** it is about to grant for, with a
  "Not you?" switch that signs out and returns to the same request —
  needed once one person runs two Acta accounts in one Desktop.
- **`me` resolves to the caller** in every MCP tool that takes a
  username, so "assign it to me" stops picking the workspace owner.
- **MCP coverage widened** — project create, label groups, project and
  member reads, status updates, comment writes.
- **Workspace in the URL path** with a disambiguation page when a
  project key exists in two workspaces.
- **Meetings / calls** — a Meeting model with its own list, detail and
  editor, comments as a third comment target, participant notifications
  in-app and over Telegram, and a My Work strip. ADR 0030.
- **Recurring tasks** — a rule entity with a daily materializer, the
  `/recurring/` page with filters and a schedule editor, pause / run /
  delete, a "make recurring" action from a task, and assignee
  notification on each occurrence. ADR 0028.
- **Installable PWA** — manifest, service worker and icons. ADR 0029.
- **Faceted filters** — project and assignee facets with live counts and
  hide-zeros, served by one facet endpoint; the assignee strip is
  single-pick by default (Cmd / Ctrl / Shift to add).
- **Multi-select** — checkboxes in the list view across My Work, All
  Tasks and project detail, and multi-select in the task link picker.
- **Create-task modal** gained size, attachments and a cycle picker.
- **Inline project rename** on the Overview tab, for workspace admins
  and the project lead.
- **Linked tasks and subtasks open in the modal** instead of a full
  navigation.
- **Telegram deploy heads-up** — a "system update" notice to linked
  chats ahead of a deploy, with a per-user mute.
- **`.ipynb` uploads** are allowed as attachments.
- **Ukrainian translations** for the recurring UI, filters, the
  workspace chooser, calls and the OAuth consent screen.

### Changed

- The Status filter is hidden on kanban (the columns *are* the statuses)
  and cleared when switching into that view.
- The task modal closes on × or Esc only, no longer on a backdrop click.
- Task detail content column widened; "show my projects" now defaults to
  off, matching the server.

### Fixed

- **Kanban cards would not drag** after a filter change or a live
  update: the panel refresh swaps HTML without HTMX, so nothing re-bound
  Sortable until some other swap happened. The same gap froze the task
  table's virtual window, which read as "half my tasks are missing".
- **A peer's new task appeared on a filtered board** — the client filter
  pass skipped every row when the chip querystring and the form
  disagreed, waiting on a refetch that an SSE push never triggers.
- **A 500 response was swapped into the panel**, wiping the board and
  with it the columns Sortable was bound to.
- **Two projects sharing a slug prefix in different workspaces** 500'd;
  both the web UI and MCP now disambiguate instead.
- **Kanban drag in Safari**, stuck table label popovers, the emoji
  picker flipping off-screen, duplicate command-palette keys, and
  meeting comment threads colliding by id.
- **Link-picker search** now matches partially typed task numbers, title
  words, slug prefixes and assignees, and stops hiding its own results.
- **Task delete from the context menu** posts reliably, and delete is
  available in the detail modal and page.
- **Telegram DMs to deactivated users** are no longer attempted.
- Long unbreakable URLs wrap in comment and inbox bodies; the
  "copy link" action in the modal copies the task's URL, not the page's.
- A long tail of filter-toggle, cold-load and cross-view SSE sync fixes
  across My Work, All Tasks and project detail.

### Performance

- **Task table virtualisation** — hovering went quadratic with the size
  of the render tree, not the row count; rows now leave layout outside a
  quantised window with spacers holding the scrollbar. ADR 0032.
- **Row checkboxes reveal with `visibility`**, not an animated opacity
  that Safari was layer-promoting per row: 12 fps → 60.
- **One SSE stream per tab** instead of two, halving the connection cost
  of an open tab.
- **On-demand panel loading** — only the active view's panel is fetched,
  and chip toggles refresh it instead of prefetching everything cold.
- Delegated modal-open and row-filter handlers drop roughly seven Alpine
  bindings per row; bulk-table partials inlined; `task.recurrence`
  preloaded on detail.

### Infrastructure

- **`CONN_MAX_AGE = 0`** — persistent connections under ASGI pinned one
  database connection per open SSE stream until Postgres refused new
  ones and every request answered 500. `max_connections` raised to 300
  for headroom.
- **Request errors log to stderr.** Production had no `LOGGING` config
  at all, so every traceback went nowhere.
- Telegram DMs are delivered through django-q, so a write never blocks
  on `api.telegram.org`.
- Built JS and CSS bundles are copied into the runtime image.

## [0.5.1] — 2026-05-31

Hot-fix to unbreak the production image build.

### Fixed

- **`tailwind.prose.config.js` missing from the Docker build context** —
  `npm run build:css` chains two tailwindcss invocations (`main` +
  `prose`), but the Dockerfile only `COPY`-ed `tailwind.config.js`.
  Local `make build-css` worked because it bind-mounts the whole repo;
  the in-image build failed with *"Specified config file
  /build/tailwind.prose.config.js does not exist"*. Added the missing
  `COPY tailwind.prose.config.js ./` line.

## [0.5.0] — 2026-05-31

The headline release after `0.4.0`: a real workspace dashboard, archive
+ delete for projects and workspaces, Kaneo data import in prod, mobile
viewport support, a Wave-1/2/3/4 audit-driven perf push, a pre-deploy
backup pipeline, and a long tail of follow-ups around task-list filters,
project membership, and notifications.

### ⚠ Breaking / migrations

Run migrations after deploying — this release adds rows across many
apps. Existing data is preserved.

- `notifications` (new app) + `reactions` (new app) + `cycles.0002`,
  `tasks.0008..0012`, `projects.0004..0007`, `workspaces.0006..0008`,
  `labels.0003..0004`, `accounts.0008`, `telegram.0001..0005` —
  Django's `migrate` applies them all in dependency order.
- `Task.end_date` / `Task.completed_at` (already in `0.4.0` migration
  set, repeated here because the timeline + completion-date filters in
  this release rely on the backfill having run).

### Added

- **Workspace dashboard** at `/` (was a stub): live KPI tiles (created /
  done / in-flight / active people with sparklines + "why" hints),
  attention alerts, cross-project status pipeline, an 8-week cumulative
  flow chart, per-project velocity with forecast, distribution panels,
  a workload matrix + sortable leaderboard, hygiene cards, and a
  7×24 activity heatmap. Range switch (7/14/30/90d) swaps the body via
  HTMX. Realises ADR 0016 from the design-system mock. Charts via
  Chart.js; data from a single N+1-safe context builder
  (`apps/web/dashboard.py`).
- **Archive / delete a project** from the project Overview header (owner /
  admin only). Archive is a soft hide — the project drops out of the
  sidebar and the active project list (revealed via "Show archived" on the
  Projects page, with an Unarchive control on its overview). Delete is a
  hard cascade behind a typed-slug confirmation modal; irreversible.
- **Archive view tab** on every task list (Table / List / Timeline /
  Kanban / Backlog) with a locked archived-only filter and muted row
  marker. Switches scope without leaving the page; archived rows stay
  out of the default lists. See ADR 0004 follow-up.
- **Task-list filter shortcuts** (also power the dashboard links):
  `due=overdue` / `due=soon` / `due=none`, `label=none`, `desc=none`.
  See ADR 0019.
- **Favourite tasks** — toggle a star on any task; favourites appear
  nested in the sidebar (or as a dedicated "Issues" list when there
  are many). Per-user state, syncs over SSE.
- **Telegram quiet-hours digest** — per-user quiet-hours window;
  notifications produced during the window queue silently and flush
  as a single digest when the window ends. Per-kind muting too.
- **Project Updates polish** — inbox preview gets edit + delete
  actions, opt-in project-stats snapshot embedded in the update, with
  deep links into the matching filtered task list.
- **Project membership** wired through the UI: assignee picker ranks
  members first, sidebar "Show my projects" toggle, per-project
  `notify_members_only` flag, workspace `auto_add_to_all_projects`
  for invitees.
- **Promote chip** — quick "advance status" pill in list view +
  inline status-axis promotion; gated promotes (missing required
  fields) open the task modal with the relevant picker focused.
- **Required-on-transition** workspace policy — block forward
  transitions out of `to-do` unless assignee / priority / description
  are filled; surfaced uniformly on kanban DnD, promote chip, and
  DRF PATCH. Companion `member_defaults` and members CSV export.
- **Mobile viewport support** (Wave 4) — slide-in nav drawer below
  `md:`, full-screen modals below `sm:`, bottom-sheet filter drawer,
  full-screen inbox detail, compact list rows on mobile.

### Changed

- **UI font** swapped from the OS stack to **Exo 2** (variable, full
  Cyrillic); slugs / code stay on JetBrains Mono.
- **Dashboard** moved to the top of the sidebar nav (above Inbox).
- **Only the assignee may change a task's start / end dates** (an
  unassigned task stays open to anyone; the hard `due_date` is
  unrestricted). Enforced on every surface — rail date cells, DRF, MCP,
  and bulk — and the rail cells render read-only for non-assignees. The
  timeline only ever drags the deadline, so it's unaffected.
- **Workspace settings page rebuilt** on the design-system mock: a single
  sectioned page — General (rename, auto-archive horizon, member-announcement
  toggle + read-only slug/owner/created/members), People & policy (members +
  invites merged with custom role dropdowns, WIP limits, cycles), and a
  **Danger zone** at the bottom. WIP-limit and cycle saves swap in place via
  HTMX (no reload). Ukrainian translations completed.
- **Transfer ownership / delete workspace** (owner only, ADR 0010) in the
  Danger zone. Transfer hands the workspace to another member and demotes the
  old owner to admin; delete is a hard cascade behind a typed-slug confirm.
- **Filter sidebar** redesigned with icon rail + 2-column layout,
  client-side toggle persistence (cookies), badge counts via Alpine,
  pinned full-row height.
- **Kanban board** — header recount + substatus updates now arrive via
  peer SSE (no manual refetch); Alpine `x-show` drift fixed by
  swapping `Set` for a plain object selection store.
- **List view** — in-place row swap on task mutation; backlog panel
  refetches whole-panel on `task.status_changed` / `task.updated`.
- **Label grouping** rolled out — Type / Area / Layer groups with
  seeding + management UI; Cmd+K palette over labels and tasks.

### Fixed

- **Notify the assignee when a task is created already assigned to them**
  — task creation only emitted `task.created`, which isn't a watched diff
  kind, so an assignee set in the create modal got no inbox/Telegram
  notification (only re-assignment did). Wired into every create path
  (web, DRF, MCP).
- **Record `start_date` changes in the activity log** — only `end_date`
  / `due_date` emitted an event; a start-date edit left no trace. Added a
  `task.start_changed` event (feed render + label + uk translation).
- **Modal tooltips flip below their trigger** — an upward tooltip on a
  control just under the modal header was clipped by the panel's top
  edge; modal tooltips now render downward.
- **Kanban flutter** on task creation — HTMX OOB insertion + ID-based
  selectors + view-aware create paths (kanban / table / list + SSE).
- **Sidebar nav jitter** — Alpine race on hydration; logo drift; live
  filter counts.

### Performance

- **List payload** −80 % (3.74 MB → 0.746 MB on 260 tasks) via panel
  splitting + per-axis lazy fetch (Wave 1 audit).
- **Self-hosted htmx / sse** and Tailwind / Lucide rebuilds: Performance
  score +41, Total Blocking Time −1080 ms, fonts +32 %, `acta.js`
  148 → 54 KB, sprite −129 KB.
- **TipTap lazy-load** — −169 KB gzip on non-editor pages.
- **SSE sync fix** — `GZipSkipSseMiddleware` stops `GZipMiddleware`
  buffering tiny SSE events in gzip's 32 KB window.

### Infrastructure

- **Pre-deploy backup pipeline** — `make deploy` now runs
  `make backup-prerelease` before any `git fetch` / `reset`.
  `BRANCH=master` snapshots DB + media (retention 14);
  `BRANCH=dev` snapshots DB only (retention 7). Bundles include a
  manifest (`git_sha`, `target_sha`, applied migrations, sizes) and
  are written under `$ACTA_BACKUP_DIR` (default `/var/backups/acta`).
  `make backup-daily` is cron-ready for a daily DB-only snapshot.
  `make restore FROM=<bundle>` restores a bundle, taking a safety
  snapshot first. See `docs/operations.md` "Backups" and
  `docs/deployment.md` "Rolling back".
- **Telegram message templates auto-seed on deploy** — a new
  `seed_telegram_templates` management command (run from the entrypoint)
  fills the default body for any notification kind missing a row, so a
  fresh DB no longer starts with an empty admin template list. Idempotent;
  `--overwrite` resets edited rows. See `docs/operations.md` §5.
- **Kaneo import** applied to prod 2026-05-29 (18 projects / 260 tasks
  / 315 labels / 16 users into the `ksu24` workspace).

## [0.4.0] — 2026-05-27

Backlog grooming, create-task-from-text, JSON export, a three-date task
model, account-settings polish, and a security hardening of password
rules — on top of a redesigned workspace-settings page.

### ⚠ Breaking / migrations

Run migrations after deploying. Existing accounts with weak passwords
keep working; only new passwords are validated.

- `Task.end_date` added — the timeline bar-end is now separate from the
  deadline (`due_date`).
- `Task.completed_at` added (+ backfill) to support date-range filtering
  on a selectable field.
- `AUTH_PASSWORD_VALIDATORS` enabled (standard Django set, min length 8).
  Weak passwords such as `123` are now rejected at signup and on
  password set/change.

### Added

- **Backlog grooming** — a dedicated Backlog tab with inline promote and
  a show-backlog toggle.
- **Create task from text** — spin a new task off a comment (auto-linked
  as related) or off any selected text in a comment or description; lands
  in a prefilled create modal.
- **Export filtered views as JSON** — All Tasks, My Work, project task
  lists, and the project overview export the currently filtered view
  (reactions + replies included in the overview shape).
- **Password set/change** in a modal, alongside a redesigned account
  settings page.

### Changed

- **Workspace settings** reworked into a two-column layout with
  scrollable member and invite lists.
- Backlog / archived toggles now resolve entirely client-side for instant
  feedback; non-active view panels load lazily.
- The Telegram webhook auto-registers on deploy from
  `ACTA_PUBLIC_BASE_URL`.

### Fixed

- Google signup now works when matched to a pending invite by email, and
  the blocked-signup page explains a missing invite.
- `serve_avatar` returns 404 (not 500) on a missing file.
- Timeline: stamp `start_date` for tasks created in-progress; refresh the
  bar + hover tooltip after a drag-resize; don't push the URL when
  opening a task modal from the timeline.
- Backlog tab stays populated regardless of the show-backlog toggle, and
  the toggle is respected in the tasks JSON export.
- Open a single SSE stream for the active workspace only.

### Performance

- Cache-bust avatar URLs with `?v=<version>` everywhere.
- Dropped the modal backdrop-blur that caused cursor lag on weak GPUs.

### Tests

- Integration coverage for every DRF viewset through `APIClient` (tasks,
  projects, project updates, comments, labels, label groups, workspaces,
  workspace members, activity log).

## [0.3.0] — 2026-05-24

Large feature release on top of v0.2.x: Telegram notifications, file
attachments + avatars everywhere, workspace cycles, scrumban (WIP limits,
aging, insights), an admin-managed job scheduler, single-active-workspace
scoping, a unified context menu, broadcast announcements, and Google login.

### ⚠ Breaking / migrations

Run migrations and rebuild the image (the scheduler adds a new compose
service; uploads need the `acta-media` volume + Pillow).

- New apps / models: `apps.attachments` (Attachment, content-addressed
  dedup, ref-counted delete), `apps.telegram` (account linking, per-kind
  message templates + delivery prefs), `apps.cycles` (workspace
  auto-rolling cycles); avatars on `User`; `User.active_workspace`.
- New task statuses: `ready` (replenishment buffer) and `cancelled`
  (terminal); tasks can move between projects.
- WIP limits on projects + workspaces (personal + column).
- Infra: recurring jobs run via a django-q2 `qcluster` service (no host
  cron). New `acta-media` volume.
- Auth: Google login — verified-email links existing accounts; social
  signup gated on a matching invite. New allauth settings.

### Added

- **Telegram notifications**: account-linking + `notify()` fan-out,
  admin-editable per-kind message templates, per-kind delivery prefs,
  localized bot replies, rich placeholders, markdown-cleaned previews.
- **File attachments**: task + comment attachments (upload / serve /
  delete), inline image paste/drop everywhere (description editor,
  comments, project updates + their comments, create-task modal),
  content-addressed dedup, lightbox gallery (arrow keys),
  alt-from-filename, orphan GC.
- **Avatars**: upload + square-crop + serve, in-browser downscale, shown
  across comments, members, overview, topbar, tables, lists, filters,
  kanban, timeline, project list, link search.
- **Cycles**: workspace-level auto-rolling cycles + assignment +
  dashboard, auto-rollover of unfinished tasks, start / approaching-end
  notifications.
- **Scrumban**: project + workspace WIP limits (personal + column), aging
  WIP, `ready` status, project insights (cycle / lead time + throughput),
  cumulative-flow + bottleneck dashboard.
- **Single active workspace** + sidebar switcher.
- **Unified context menu**: right-click + bulk task actions.
- **Admin-managed scheduler**: django-q2 schedules editable in `/admin/`.
- **Announcements**: broadcast to the workspace inbox (force-delivered).
- **Google login**: "Continue with Google" on login + signup.
- Cancel-task status, move task between projects, editable Size cell,
  size filter, project favourite-star, create-modal project prefill,
  flash-message toasts, email invites in Members.

### Fixed

- 29 fixes across kanban drag-and-drop, filters, timeline, notification
  previews, query counts, and UI polish. See `git log v0.2.1..v0.3.0`.

### Performance

- In-browser avatar downscale before upload; batched cycle dashboard
  summaries; draft-decode avatar processing.

## [0.2.1] — 2026-05-21

Patch release — sidebar version-link polish on top of v0.2.0.

### Fixed

- Sidebar version no longer wraps to its own line: the changelog link
  is split out of the dashboard link (nested `<a>` is invalid HTML and
  the browser broke it onto a new line).
- Dropped the version-link tooltip that clipped off the top of the
  window; the link keeps an `aria-label` for accessibility.
- The `[0.2.0]` changelog heading now links to its release tag.

## [0.2.0] — 2026-05-21

Second release. Adds collaboration (reactions, threaded comments,
mentions, notifications), an MCP server + API tokens for automation,
invite-based signup, a timeline/Gantt view, and a full design-system
rebrand on top of the v0.1.0 MVP.

### ⚠ Breaking / migrations

Deployers should run migrations and review the items below — each
changes a model, a public surface, or prod settings:

- New models / fields: `Reaction` (polymorphic), `Task.start_date`,
  task links, persistent notifications/inbox, comments on project
  updates, API tokens, workspace invites.
- API: comments became polymorphic (DRF comment API stays task-only);
  new reaction toggle, task link, and MCP endpoints.
- Signup is now invite-only (workspace tokens, expiring + one-use).
- Prod compose closes the Postgres host port and pins container names
  (multi-stage build with a node stage).
- Brand palette swapped from lavender to indigo-blue.

### Added

- **MCP server** — Model Context Protocol integration over stdio and
  HTTP (`/mcp/`): read tools (task/activity/comments/links), write
  tools (create / update / archive / comment / link), label CRUD with
  auto-create, bulk + delete, rate limiting, and N+1 guards.
- **API tokens** — token auth for non-browser clients with a settings
  management UI (create / revoke / delete) and an MCP setup snippet.
- **Invite-based signup** — expiring, one-use workspace invite tokens,
  invite UI on settings, SMTP email delivery, and a custom signup page
  that prefills + locks the invited email.
- **Emoji reactions** — reaction bars on tasks, comments, and project
  updates (generic `Reaction` model + toggle endpoint, vendored
  emoji-picker).
- **Comments** — edit / delete / in-place edit, relative timestamps,
  one-level replies on task comments, plus comments and replies on
  project updates.
- **Project updates** — compose from the overview with health chips,
  edit, and delete.
- **Notifications & Inbox** — persistent `/inbox/` with server-side
  fan-out, live SSE arrival on a per-user channel, mention fan-out, and
  notifications on `project_update.created`.
- **Mentions** — `@`-picker in the TipTap editor with chips, hover
  cards, and task-modal open; backend mention search + fan-out.
- **Task links** — link / unlink with autocomplete and live
  blocked / blocking badges.
- **Timeline (Gantt) view** — third project tab with `start_date`
  scheduling, drag-to-reschedule, day / week / month zoom, open-ended
  bars for partially-dated tasks, client-side filters, lazy-loaded.
- **My Activity** — my-comments + activity tabs grouped by task, with
  diffs, links, previews, and load-more.
- **Activity** — filters + search, load-more counter, comment clamp.
- **Workspaces** — create-workspace / create-project / settings flows.
- **i18n** — Ukrainian translations for the create flows and the
  timeline view.

### Changed

- **Rebrand** — indigo-blue brand palette, a `midnight` theme variant,
  a custom logo mark + favicons (replacing the text "A"), and
  Inter + JetBrains Mono webfonts.
- **Design system** — redesigned task cards / rows, kanban headers +
  substatus row, table (merged slug + priority, sticky header, label
  popover), project cards (progress bar, breakdown chips), bulk bar
  (glass surface, brand glow), create-task modal, segmented view tabs,
  task-detail topbar, modal scrim, priority-picker shortcuts, and
  extracted button / input primitives.
- **Navigation** — sidebar reordered (inbox first, dashboard in the
  footer) with an active stripe and version kicker; boosted-nav fixes
  remove full-page reloads.
- **Performance** — project overview collapsed to a single aggregate
  query (21 → ≤15), and the timeline panel is lazy-loaded.
- **Deploy** — `make deploy BRANCH=…`, `make ci-check`, deployment
  docs, and a multi-stage Docker build.

### Fixed

- HTMX history navigation restores the full page — Back no longer
  drops the shell (timeline no longer renders full-screen).
- Reaction tooltip stays inside `overflow-hidden` comment cards.
- Timeline: scroll bounds + overscroll, full-width rows, uniform week
  columns, today-line layered behind bars, theme-reactive header, and
  a full-title hover card.
- Live per-section filter counts; dropped redundant chip tooltips.
- Comment timestamp moved to the top-right; DRF comment API kept
  task-only under the polymorphic model.
- SSE applies MCP-driven events even when the actor is the current
  user.
- Misc: `EMAIL_*` env fallback, admin invite nav, focus-outline reset,
  deduped label hover tooltips, and activity diff em-dash cleanup.

## [0.1.0] — 2026-05-18 — Initial MVP

First production release. Self-hosted on Debian behind a Traefik edge
proxy at `actaspace.com`.

### Added

- **Workspaces, projects, tasks, subtasks** with human-readable slug
  prefixes (`ABC-123`) and per-project task number allocation.
- **HTMX-driven UI**: server-rendered Django templates + Alpine.js for
  local state, no client-side framework. Table view, Kanban board, and
  grouped list with stackable axes.
- **Rich text descriptions** via TipTap (bundled with esbuild). Inline
  toolbar appears on focus; supports lists, code, highlight, task
  lists, links.
- **Task detail in modal** (click) or full page (Ctrl+click / direct
  URL) with a unified comments + activity timeline.
- **Activity log** with per-field diff capture on every watched
  attribute; written exclusively by `apps.activity.services.log_event`
  from viewset `perform_*` hooks (ADR 0011).
- **Bulk operations** via a single universal endpoint
  `PATCH /api/v1/tasks/bulk/` with all-or-nothing transactionality
  (ADR 0012). Confirmation modal for bulk archive.
- **Real-time updates** over Server-Sent Events using
  `django-eventstream`; ASGI-only with Uvicorn (ADR 0015).
- **Filters**: status / priority / labels / assignee / project, both
  client-side and server-side; sidebar state persisted.
- **Labels** with hex colour, workspace-scoped, pills shared across
  filter sidebar, task row, table cell, task detail, create-task
  modal.
- **Project icons + colour picker** from a curated Lucide subset and
  21-colour Tailwind palette; live updates across sidebar / list /
  detail surfaces.
- **Favourite projects** — star toggle on the project list; the
  sidebar nav lists only starred projects.
- **User settings** page: first/last name, email (read-only), language.
- **Internationalization** — English (source) and Ukrainian, 255 / 255
  strings translated. `.po` committed, `.mo` built at deploy.
- **Dark / light theme** driven by CSS variables; pre-paint script in
  `<head>` prevents FOUC.
- **Custom login + closed signup** — Tailwind-branded
  `templates/account/login.html`, `is_open_for_signup` returns `False`
  for both the password and social adapters. Admins create accounts via
  Django admin.
- **Global HTMX error toast** with auto-dismiss.
- **Deployment plumbing**: production `Dockerfile`, `docker-entrypoint.sh`
  that runs `migrate` + `compilemessages` + `collectstatic` before
  starting Uvicorn, `CSRF_TRUSTED_ORIGINS` read from the environment,
  HSTS / secure cookies / `SECURE_PROXY_SSL_HEADER` for Traefik.

### Architecture

Decisions captured in `docs/decisions/0001-0019`. Headline:

- ASGI + Uvicorn (no WSGI Gunicorn — SSE on sync workers fails).
- Server-rendered templates + HTMX + Alpine + Chart.js + sortable.js;
  no React, no build step except TipTap and Tailwind.
- `request.user` is the only source of truth for activity-log actor.
- `master` is the deployable branch, `dev` is integration, history is
  linear (rebase, no merge commits).

[0.2.1]: https://github.com/voxquietam/acta/releases/tag/v0.2.1
[0.2.0]: https://github.com/voxquietam/acta/releases/tag/v0.2.0
[0.1.0]: https://github.com/voxquietam/acta/releases/tag/v0.1.0
