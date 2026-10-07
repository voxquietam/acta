"""What the person in front of My Work should do next, and why.

My Work used to be the All Tasks list with the assignee pinned to the
viewer. A list answers "what is mine"; it does not answer "which of
these first", which is the only question someone opens the page with.

So the page leads with a ranking. Every open task of the viewer's is
scored against the handful of facts that make work urgent — the date
has passed, someone is waiting on it, the date is about to pass, it is
marked urgent, it runs past the milestone it was committed to, it has
sat in the same status for a fortnight — and each fact carries both a
weight and the sentence that explains it. The weight decides the order;
the sentence is what the row shows, because an order nobody can check
is an order nobody trusts.

A task with no such fact is not ranked at all. It is not urgent; it
belongs in the list below, not at the top of the page.
"""

import datetime

from django.db.models import Count, OuterRef, Prefetch, Q, Subquery
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from apps.activity.models import ActivityLog
from apps.milestones import forecast
from apps.tasks.models import Task, counted_q

#: How many ranked rows "Do first" shows. Six is what fits above the
#: fold; a seventh would be read as a backlog rather than as a shortlist.
DO_FIRST_LIMIT = 6

#: A date this many days out or nearer counts as pressing.
SOON_DAYS = 2

#: How long a task may sit in the same status before it is stale.
STALE_DAYS = 14

#: Statuses where sitting still is a problem. Work that is planned or
#: ready is meant to be waiting; work in progress is not.
MOVING_STATUSES = [
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]


def do_first(tasks, today=None) -> list[dict]:
    """Rank the viewer's open work, worst first.

    Args:
        tasks: The viewer's open tasks, from :func:`focus_tasks`.
        today: Reference date; defaults to the local current date.

    Returns:
        One row per task that has at least one reason, heaviest first.
        Each carries ``task``, ``reasons`` (heaviest first), ``weight``
        and the ``action`` label its button shows.
    """
    today = today or timezone.localdate()
    scored = []
    for task in tasks:
        reasons = _reasons(task, today)
        if not reasons:
            continue
        reasons.sort(key=lambda reason: -reason["weight"])
        scored.append(
            {
                "task": task,
                "reasons": reasons,
                "weight": sum(reason["weight"] for reason in reasons),
                "action": _action(task, reasons[0]),
                "due_label": due_label(task, today),
                "due_tone": due_tone(task, today),
            },
        )
    scored.sort(key=lambda row: (-row["weight"], row["task"].id))
    return scored


def due_label(task, today=None) -> str:
    """Return how a row says when the task is due.

    Near dates are said in words because that is how people hold them;
    anything further out is a date, because "in 23 days" is not.

    Args:
        task: The task.
        today: Reference date; defaults to the local current date.

    Returns:
        A translated phrase, or the empty string when there is no date.
    """
    if not task.due_date:
        return ""
    today = today or timezone.localdate()
    days = (task.due_date - today).days
    if days < 0:
        return ngettext("%(count)dd overdue", "%(count)dd overdue", -days) % {"count": -days}
    if days == 0:
        return str(_("today"))
    if days == 1:
        return str(_("tomorrow"))
    return f"{task.due_date:%b} {task.due_date.day}"


def due_tone(task, today=None) -> str:
    """Return the colour a row's due date is said in.

    Args:
        task: The task.
        today: Reference date; defaults to the local current date.

    Returns:
        A Tailwind text-colour utility.
    """
    if not task.due_date:
        return "text-placeholder-foreground"
    today = today or timezone.localdate()
    days = (task.due_date - today).days
    if days < 0:
        return "text-rose-400"
    if days <= SOON_DAYS:
        return "text-amber-400"
    return "text-subtle-foreground"


def _reasons(task, today) -> list[dict]:
    """Collect every reason this task is pressing.

    The weights are what order the page, so they are written out here
    rather than derived: overdue outranks everything, being in someone
    else's way outranks one's own deadline, and sitting still is the
    weakest reason of the six because it is the only one nobody else
    feels.

    Args:
        task: One of the viewer's open tasks, annotated by
            :func:`focus_tasks`.
        today: Reference date.

    Returns:
        Reason dicts with ``kind``, ``weight``, ``text`` and ``tone``.
    """
    reasons = []
    days = (task.due_date - today).days if task.due_date else None
    if days is not None and days < 0:
        over = -days
        reasons.append(
            {
                "kind": "overdue",
                "weight": 100 + min(30, over),
                "text": ngettext("overdue by %(count)d day", "overdue by %(count)d days", over) % {"count": over},
                "tone": "text-rose-400",
            },
        )
    if task.focus_blocks:
        names = []
        for blocked in task.focus_blocks:
            name = blocked.assignee.display_name if blocked.assignee else str(_("someone"))
            if name not in names:
                names.append(name)
        reasons.append(
            {
                "kind": "blocks",
                "weight": 60 + len(task.focus_blocks) * 10,
                "text": _("blocks %(who)s · %(what)s")
                % {
                    "who": ", ".join(names),
                    "what": ", ".join(blocked.slug for blocked in task.focus_blocks),
                },
                "tone": "text-rose-400",
            },
        )
    if days is not None and 0 <= days <= SOON_DAYS:
        reasons.append(
            {
                "kind": "soon",
                "weight": 45 - days * 5,
                "text": _("due today") if not days else _("due tomorrow") if days == 1 else _("due in 2 days"),
                "tone": "text-amber-400",
            },
        )
    if task.priority == Task.URGENT:
        reasons.append(
            {
                "kind": "urgent",
                "weight": 30,
                "text": _("urgent"),
                "tone": "text-rose-400",
            },
        )
    past = _days_past_milestone(task)
    if past:
        reasons.append(
            {
                "kind": "past-milestone",
                "weight": 20 + min(10, past),
                "text": _("%(count)dd past %(milestone)s")
                % {
                    "count": past,
                    "milestone": task.milestone.name,
                },
                "tone": "text-rose-400",
            },
        )
    sitting = _days_sitting(task, today)
    if sitting is not None and sitting > STALE_DAYS:
        reasons.append(
            {
                "kind": "sitting",
                "weight": 15,
                "text": _("%(status)s for %(count)d days")
                % {
                    "status": Task.STATUS_LABELS.get(task.status, task.status),
                    "count": sitting,
                },
                "tone": "text-amber-400",
            },
        )
    return reasons


def _days_past_milestone(task) -> int:
    """Return how far a task's own date runs past the date it aims at.

    Args:
        task: The task, with ``milestone`` loaded.

    Returns:
        Days over, or ``0`` when the task makes its milestone, has no
        milestone, or the milestone is closed.
    """
    milestone = task.milestone
    if milestone is None or milestone.is_closed or not task.due_date:
        return 0
    if task.due_date <= milestone.target_date:
        return 0
    return (task.due_date - milestone.target_date).days


def _days_sitting(task, today) -> int | None:
    """Return how long a moving task has sat in its current status.

    Args:
        task: The task, carrying the ``status_since`` annotation.
        today: Reference date.

    Returns:
        Days since the status last changed, or ``None`` when the status
        is one where waiting is normal.
    """
    if task.status not in MOVING_STATUSES:
        return None
    since = task.status_since or task.created_at
    if since is None:
        return None
    if isinstance(since, datetime.datetime):
        since = timezone.localtime(since).date() if timezone.is_aware(since) else since.date()
    return (today - since).days


def _action(task, top) -> str:
    """Return the label for the row's button.

    The button says what the person does next, which is not always what
    the status implies: work that is in someone else's way is opened to
    be handed over, whatever state it is in.

    Args:
        task: The ranked task.
        top: Its heaviest reason.

    Returns:
        A translated verb.
    """
    if top["kind"] == "blocks":
        return _("Open")
    if task.status == Task.STATUS_IN_REVIEW:
        return _("Review")
    if task.status == Task.STATUS_IN_PROGRESS:
        return _("Continue")
    return _("Start")


def focus_tasks(user, workspace=None):
    """Return the viewer's open work, loaded for the whole focus strip.

    Everything the strip reads — the ranking, the counts, the tidy-up
    chips — is read off this one list rather than re-queried per block,
    so the page cost is the same whether a person holds five tasks or
    two hundred.

    Each task comes back carrying ``focus_blocks`` and
    ``focus_blocked_by``: the open work of OTHER people on either side
    of a blocking link. Links to one's own tasks are left out — a person
    waiting on themselves is not waiting.

    Args:
        user: The viewer.
        workspace: Narrow to this workspace, or ``None`` for every
            workspace the viewer belongs to.

    Returns:
        A list of :class:`~apps.tasks.models.Task`.
    """
    last_status_change = (
        ActivityLog.objects.filter(
            target_type=ActivityLog.TARGET_TASK,
            target_id=OuterRef("pk"),
            event_type="task.status_changed",
        )
        .order_by("-created_at", "-id")
        .values("created_at")[:1]
    )
    related = Task.objects.select_related("assignee", "project")

    queryset = (
        Task.objects.work()
        .filter(assignee=user, archived_at__isnull=True)
        .exclude(
            status__in=[
                Task.STATUS_DONE,
                Task.STATUS_CANCELLED,
            ],
        )
        # ``project__workspace`` because every row links to the task and
        # the canonical URL names the workspace — without the hop that is
        # one query per ranked row (``task_path`` says so in as many words).
        .select_related("project__workspace", "milestone", "epic")
        .prefetch_related(
            Prefetch("blocks", queryset=related),
            Prefetch("blocked_by", queryset=related),
        )
        .annotate(status_since=Subquery(last_status_change))
    )
    if workspace is not None:
        queryset = queryset.filter(project__workspace=workspace)
    tasks = list(queryset)
    for task in tasks:
        task.focus_blocks = [other for other in task.blocks.all() if _is_someone_elses_open_work(other, user)]
        task.focus_blocked_by = [other for other in task.blocked_by.all() if _is_someone_elses_open_work(other, user)]
    return tasks


def _is_someone_elses_open_work(task, user) -> bool:
    """Whether a linked task is live work belonging to another person.

    Args:
        task: The task on the other end of a blocking link.
        user: The viewer.

    Returns:
        ``True`` when it is unfinished, unarchived and not the viewer's.
    """
    return (
        task.status not in (Task.STATUS_DONE, Task.STATUS_CANCELLED)
        and task.archived_at is None
        and task.assignee_id != user.id
    )


def summary(tasks, today=None) -> list[dict]:
    """Return the one-line reading of the viewer's day.

    Five counts, each a different kind of pressure, and the ones that
    are zero are left out rather than shown as zero: a row of noughts
    reads as a dashboard, and this line is meant to read as a sentence.

    Args:
        tasks: The viewer's open tasks, from :func:`focus_tasks`.
        today: Reference date; defaults to the local current date.

    Returns:
        Row dicts with ``kind``, ``count``, ``label``, ``icon`` and
        ``tone``, leaving out every zero.
    """
    today = today or timezone.localdate()
    overdue = sum(1 for task in tasks if task.due_date and task.due_date < today)
    soon = sum(1 for task in tasks if task.due_date and 0 <= (task.due_date - today).days <= SOON_DAYS)
    waiting = sum(1 for task in tasks if task.focus_blocks)
    blocked = sum(1 for task in tasks if task.focus_blocked_by)
    at_risk = len(milestones_at_risk(tasks, today))
    rows = [
        ("overdue", overdue, _("overdue"), "calendar-x", "text-rose-400"),
        ("waiting", waiting, _("waiting on you"), "at-sign", "text-rose-400"),
        ("soon", soon, _("due in 2 days"), "clock", "text-amber-400"),
        ("blocked", blocked, _("blocked"), "hourglass", "text-amber-400"),
        (
            "at-risk",
            at_risk,
            ngettext("milestone at risk", "milestones at risk", at_risk),
            "diamond",
            "text-rose-400",
        ),
    ]
    return [
        {
            "kind": kind,
            "count": count,
            "label": label,
            "icon": icon,
            "tone": tone,
        }
        for kind, count, label, icon, tone in rows
        if count
    ]


def milestones_at_risk(tasks, today=None) -> list:
    """Return the viewer's milestones their own work is endangering.

    Read off the viewer's slice alone: a date is at risk here when it
    has passed with work of theirs still open, or when work of theirs is
    due after it. What the rest of the team owes the same date is the
    milestone page's question, not this page's.

    Args:
        tasks: The viewer's open tasks, from :func:`focus_tasks`.
        today: Reference date; defaults to the local current date.

    Returns:
        The distinct milestones, soonest date first.
    """
    today = today or timezone.localdate()
    at_risk = {}
    for task in tasks:
        milestone = task.milestone
        if milestone is None or milestone.is_closed:
            continue
        if milestone.target_date < today or (task.due_date and task.due_date > milestone.target_date):
            at_risk[milestone.id] = milestone
    return sorted(at_risk.values(), key=lambda milestone: (milestone.target_date, milestone.id))


def tidy_up(tasks, today=None) -> list[dict]:
    """Return the housekeeping the viewer's own list is asking for.

    Not urgent and deliberately not ranked — these are the three ways a
    list rots quietly, and each chip is a count rather than a warning.

    Args:
        tasks: The viewer's open tasks, from :func:`focus_tasks`.
        today: Reference date; defaults to the local current date.

    Returns:
        Row dicts with ``kind``, ``count``, ``label`` and ``icon``,
        leaving out every zero.
    """
    today = today or timezone.localdate()
    sitting = 0
    for task in tasks:
        days = _days_sitting(task, today)
        if days is not None and days > STALE_DAYS:
            sitting += 1
    rows = [
        ("sitting", sitting, _("In progress 14+ days"), "clock-alert"),
        ("no-milestone", sum(1 for task in tasks if task.milestone_id is None), _("No milestone"), "circle-dashed"),
        (
            "no-priority",
            sum(1 for task in tasks if task.priority == Task.NO_PRIORITY),
            _("No priority"),
            "minus",
        ),
    ]
    return [
        {
            "kind": kind,
            "count": count,
            "label": label,
            "icon": icon,
        }
        for kind, count, label, icon in rows
        if count
    ]


#: How many rows "Pick next" offers. Five is a choice; a longer list is
#: the backlog again, which is the thing this block exists to replace.
PICK_NEXT_LIMIT = 5

#: Statuses something can be picked up from.
PICKABLE_STATUSES = [
    Task.STATUS_READY,
    Task.STATUS_TODO,
    Task.STATUS_PLANNED,
]


def pick_next(tasks, ranked, today=None) -> list[dict]:
    """Return work that is free to start, once the urgent is dealt with.

    The complement of :func:`do_first`, and deliberately not a second
    ranking: nothing here is pressing, so the order is the plain one a
    person would use themselves — priority first, then the nearer date.

    Anything ranked at all is left out, not merely what fits on the
    shortlist: a task that missed the cut is still pressing, and
    offering it here as something to pick up reads as a bug. So is
    anything waiting on an unfinished blocker, whoever owns it —
    "ready" that cannot be started is not ready.

    Args:
        tasks: The viewer's open tasks, from :func:`focus_tasks`.
        ranked: The rows :func:`do_first` returned.
        today: Reference date; defaults to the local current date.

    Returns:
        Row dicts with ``task``, ``due_label`` and ``due_tone``.
    """
    today = today or timezone.localdate()
    urgent = {row["task"].id for row in ranked}
    free = [
        task
        for task in tasks
        if task.status in PICKABLE_STATUSES and task.id not in urgent and not _has_open_blocker(task)
    ]
    free.sort(key=_pick_order)
    return [
        {
            "task": task,
            "due_label": due_label(task, today),
            "due_tone": due_tone(task, today),
        }
        for task in free[:PICK_NEXT_LIMIT]
    ]


def _has_open_blocker(task) -> bool:
    """Whether anything unfinished stands in this task's way.

    Unlike ``focus_blocked_by`` this counts the viewer's own tasks too:
    for "waiting on you" the owner matters, for "can I start this" it
    does not.

    Args:
        task: The task, with ``blocked_by`` prefetched.

    Returns:
        ``True`` when a blocker is still open.
    """
    return any(
        blocker.status not in (Task.STATUS_DONE, Task.STATUS_CANCELLED) and blocker.archived_at is None
        for blocker in task.blocked_by.all()
    )


def _pick_order(task):
    """Sort key for Pick next: priority, then the nearer date.

    ``NO_PRIORITY`` is stored as ``0`` but means "least", so it sorts
    last rather than first. A task with no date sorts after every dated
    one instead of ahead of them.

    Args:
        task: The task being ordered.

    Returns:
        A tuple ``(priority, due_date, id)``.
    """
    return (
        task.priority if task.priority != Task.NO_PRIORITY else Task.LOW + 1,
        task.due_date or datetime.date.max,
        task.id,
    )


#: How many of the viewer's tasks each epic group lists before it stops.
COMMITMENT_ROWS = 4


def commitments(user, tasks, today=None) -> dict:
    """Return the dates the viewer has promised, hottest first.

    Two numbers per date, and keeping them apart is the point: what the
    viewer owes it, and what the whole scope owes it. A person who has
    finished their three tasks has not delivered a milestone, and a bar
    that showed only their share would say they had.

    Under each date the viewer's own open work, grouped by epic, so the
    answer to "what do I still owe this date" is on the same card as
    the date itself.

    Args:
        user: The viewer.
        tasks: Their open tasks, from :func:`focus_tasks`.
        today: Reference date; defaults to the local current date.

    Returns:
        ``hot`` (the dates in trouble) and ``steady`` (the rest, which
        the page reduces to a strip of chips).
    """
    today = today or timezone.localdate()
    by_milestone = {}
    for task in tasks:
        if task.milestone_id and not task.milestone.is_closed:
            by_milestone.setdefault(task.milestone_id, []).append(task)
    if not by_milestone:
        return {
            "hot": [],
            "steady": [],
        }
    ids = list(by_milestone)
    team = _counts_by_epic(Task.objects.work().filter(counted_q(), milestone_id__in=ids))
    ours = _counts_by_epic(Task.objects.work().filter(counted_q(), milestone_id__in=ids, assignee=user))
    rows = [_commitment(by_milestone[milestone_id], team, ours, today) for milestone_id in ids]
    rows.sort(key=lambda row: (row["milestone"].target_date, row["milestone"].id))
    return {
        "hot": [row for row in rows if row["hot"]],
        "steady": [row for row in rows if not row["hot"]],
    }


def _counts_by_epic(queryset) -> dict:
    """Return ``{(milestone_id, epic_id): (done, total)}`` in one query.

    Args:
        queryset: Counted work of the milestones in view.

    Returns:
        Done and total per milestone-and-epic cell.
    """
    rows = queryset.values("milestone_id", "epic_id").annotate(
        total=Count("id"),
        done=Count("id", filter=Q(status=Task.STATUS_DONE)),
    )
    return {(row["milestone_id"], row["epic_id"]): (row["done"], row["total"]) for row in rows}


def _commitment(open_tasks, team, ours, today) -> dict:
    """Build one commitment card from the viewer's open work under a date.

    Args:
        open_tasks: The viewer's open tasks attached to this milestone.
        team: Per-epic counts across the whole scope.
        ours: Per-epic counts of the viewer's own work.
        today: Reference date.

    Returns:
        The card dict the template renders.
    """
    milestone = open_tasks[0].milestone
    passed = milestone.target_date < today
    late = [task for task in open_tasks if task.due_date and task.due_date > milestone.target_date]
    overdue = [task for task in open_tasks if task.due_date and task.due_date < today]
    mine_done, mine_total = _total(ours, milestone.id)
    team_done, team_total = _total(team, milestone.id)
    return {
        "milestone": milestone,
        "date_label": _date_label(milestone.target_date),
        "countdown": _countdown(milestone, passed, today),
        "passed": passed,
        "hot": bool(passed or late or overdue),
        "line": _commitment_line(open_tasks, late, overdue, passed),
        "mine_done": mine_done,
        "mine_total": mine_total,
        "mine_percent": round(mine_done / mine_total * 100) if mine_total else 0,
        "team_done": team_done,
        "team_total": team_total,
        "team_percent": round(team_done / team_total * 100) if team_total else 0,
        "groups": _commitment_groups(open_tasks, team, milestone, today),
    }


def _total(cells: dict, milestone_id: int) -> tuple[int, int]:
    """Sum a per-epic count map down to one milestone's totals.

    Args:
        cells: ``{(milestone_id, epic_id): (done, total)}``.
        milestone_id: The milestone to sum.

    Returns:
        A ``(done, total)`` pair.
    """
    done = total = 0
    for (other, _epic_id), (cell_done, cell_total) in cells.items():
        if other == milestone_id:
            done += cell_done
            total += cell_total
    return done, total


def _commitment_line(open_tasks, late, overdue, passed) -> dict:
    """Return the one line that says how the viewer stands against a date.

    Args:
        open_tasks: Their open work under it.
        late: The part of it due after the date.
        overdue: The part of it already overdue.
        passed: Whether the date itself has gone.

    Returns:
        ``text`` and ``tone``.
    """
    if passed:
        text = ngettext(
            "date passed — %(count)d of yours still open",
            "date passed — %(count)d of yours still open",
            len(open_tasks),
        ) % {"count": len(open_tasks)}
    elif late:
        text = ngettext(
            "%(count)d of yours ends after this date",
            "%(count)d of yours end after this date",
            len(late),
        ) % {"count": len(late)}
    elif overdue:
        text = ngettext(
            "%(count)d of yours overdue inside it",
            "%(count)d of yours overdue inside it",
            len(overdue),
        ) % {"count": len(overdue)}
    else:
        return {
            "text": _("your part fits the date"),
            "tone": "text-emerald-400",
        }
    return {
        "text": text,
        "tone": "text-rose-400",
    }


def _commitment_groups(open_tasks, team, milestone, today) -> list[dict]:
    """Group the viewer's open work under a date by the epic it belongs to.

    Args:
        open_tasks: Their open work under the milestone.
        team: Per-epic counts across the whole scope.
        milestone: The milestone.
        today: Reference date.

    Returns:
        Group dicts, named epics first and the unattached remainder last.
    """
    buckets: dict = {}
    for task in open_tasks:
        buckets.setdefault(task.epic_id, []).append(task)
    groups = []
    for epic_id, rows in buckets.items():
        rows.sort(key=lambda task: (task.due_date or datetime.date.max, task.id))
        behind = [
            task for task in rows if task.due_date and (task.due_date > milestone.target_date or task.due_date < today)
        ]
        team_done, team_total = team.get((milestone.id, epic_id), (0, 0))
        groups.append(
            {
                "epic": rows[0].epic if epic_id else None,
                "open": len(rows),
                "late": len(behind),
                "team_done": team_done,
                "team_total": team_total,
                "rows": [
                    {
                        "task": task,
                        "due_label": due_label(task, today),
                        "due_tone": due_tone(task, today),
                        "behind": task in behind,
                    }
                    for task in rows[:COMMITMENT_ROWS]
                ],
            },
        )
    groups.sort(key=lambda group: (group["epic"] is None, group["epic"].title if group["epic"] else ""))
    return groups


def _date_label(value) -> str:
    """Format a date the short way every milestone surface uses.

    Args:
        value: A ``date``.

    Returns:
        The date as ``Mon D``.
    """
    return f"{value:%b} {value.day}"


def _countdown(milestone, passed, today) -> str:
    """Say where a date stands, in the words the milestone pages use.

    Args:
        milestone: The milestone.
        passed: Whether its date has gone.
        today: Reference date.

    Returns:
        A translated one-liner.
    """
    days = abs((milestone.target_date - today).days)
    if passed:
        return ngettext("overdue by %(count)d day", "overdue by %(count)d days", days) % {"count": days}
    if not days:
        return str(_("due today"))
    return ngettext("due in %(count)d day", "due in %(count)d days", days) % {"count": days}


#: WIP limit used when the workspace has not set a personal one. Three
#: is the number the design settled on and the one most people can hold.
DEFAULT_WIP_LIMIT = 3

#: How far back "closed this week" reaches, and the window it is
#: compared against.
WEEK_DAYS = 7

#: Weekdays "This week" lays out, and how far ahead it may look to find
#: them — a Friday afternoon should still show the next working week.
WEEK_COLUMNS = 5
WEEK_LOOKAHEAD = 9

#: How far ahead the Calls card looks.
CALLS_DAYS = 3


def kpi(user, workspace, tasks, today=None) -> list[dict]:
    """Return the four numbers the page opens with.

    Load, throughput, reliability, commitment — in that order, because
    that is the order someone asks them in: how much am I carrying, how
    much did I finish, did it land when I said, and where does the cycle
    stand.

    Args:
        user: The viewer.
        workspace: The active workspace, or ``None``.
        tasks: Their open tasks, from :func:`focus_tasks`.
        today: Reference date; defaults to the local current date.

    Returns:
        Card dicts with ``label``, ``value``, ``sub`` and ``tone``.
    """
    today = today or timezone.localdate()
    closed_rows = _closed_recently(user, workspace, today)
    cards = [_wip_card(workspace, tasks)]
    closed, on_time = _closed_cards(closed_rows, today)
    cards.append(closed)
    cards.append(on_time)
    cycle = _cycle_card(user, workspace, closed_rows, today)
    if cycle:
        cards.append(cycle)
    return cards


def _wip_card(workspace, tasks) -> dict:
    """Return the "in progress against the limit" card.

    The limit is the workspace's own personal WIP limit where it has set
    one, because a number the team agreed on reads very differently from
    a number this page invented.

    Args:
        workspace: The active workspace, or ``None``.
        tasks: The viewer's open tasks.

    Returns:
        A card dict.
    """
    limit = DEFAULT_WIP_LIMIT
    if workspace is not None:
        mode, limits = workspace.wip_config()
        if mode == workspace.WIP_PERSONAL and limits.get(Task.STATUS_IN_PROGRESS):
            limit = limits[Task.STATUS_IN_PROGRESS]
    moving = sum(1 for task in tasks if task.status == Task.STATUS_IN_PROGRESS)
    over = moving - limit
    return {
        "label": _("In progress"),
        "value": f"{moving}/{limit}",
        "tone": "text-rose-400" if over > 0 else "",
        "sub": (
            _("%(count)d over — finish one before starting another") % {"count": over}
            if over > 0
            else ngettext("room for %(count)d more", "room for %(count)d more", -over) % {"count": -over}
        ),
    }


def _closed_recently(user, workspace, today) -> list[dict]:
    """Return the viewer's work closed inside the replay window.

    One read serves three readings — how much was closed this week, how
    much of it landed on time, and the throughput the cycle forecast
    replays — so the window is the widest of the three.

    The closing day comes from the activity log rather than from
    ``updated_at``: an edit after the fact would otherwise move the day a
    task was finished, and "on time" would drift with it.

    Args:
        user: The viewer.
        workspace: The active workspace, or ``None``.
        today: Reference date.

    Returns:
        Dicts of ``closed`` (date), ``due_date`` and ``cycle_id``.
    """
    last_status_change = (
        ActivityLog.objects.filter(
            target_type=ActivityLog.TARGET_TASK,
            target_id=OuterRef("pk"),
            event_type="task.status_changed",
        )
        .order_by("-created_at", "-id")
        .values("created_at")[:1]
    )
    queryset = Task.objects.work().filter(assignee=user, status=Task.STATUS_DONE)
    if workspace is not None:
        queryset = queryset.filter(project__workspace=workspace)
    rows = []
    cutoff = today - datetime.timedelta(days=forecast.WINDOW_DAYS)
    for row in queryset.annotate(closed_at=Subquery(last_status_change)).values(
        "closed_at",
        "updated_at",
        "due_date",
        "cycle_id",
    ):
        moment = row["closed_at"] or row["updated_at"]
        if moment is None:
            continue
        closed = timezone.localtime(moment).date() if timezone.is_aware(moment) else moment.date()
        if closed < cutoff or closed > today:
            continue
        rows.append(
            {
                "closed": closed,
                "due_date": row["due_date"],
                "cycle_id": row["cycle_id"],
            },
        )
    return rows


def _closed_cards(closed_rows, today) -> tuple[dict, dict]:
    """Return the throughput and reliability cards.

    Args:
        closed_rows: What :func:`_closed_recently` returned.
        today: Reference date.

    Returns:
        A ``(closed, on_time)`` pair of card dicts.
    """
    this_week = before = on_time = 0
    for row in closed_rows:
        age = (today - row["closed"]).days
        if age <= WEEK_DAYS:
            this_week += 1
            if row["due_date"] and row["closed"] <= row["due_date"]:
                on_time += 1
        elif age <= WEEK_DAYS * 2:
            before += 1
    share = round(on_time / this_week * 100) if this_week else None
    return (
        {
            "label": _("Closed, 7 days"),
            "value": str(this_week),
            "tone": "",
            "sub": ngettext(
                "%(count)d the week before",
                "%(count)d the week before",
                before,
            )
            % {"count": before},
        },
        {
            "label": _("On time"),
            "value": f"{share}%" if share is not None else "—",
            # Nothing closed is nothing to judge, so the dash stays
            # neutral: a green em-dash reads as a pass nobody earned.
            "tone": "" if share is None else "text-rose-400" if share < 60 else "text-emerald-400",
            "sub": _("%(on_time)d of %(closed)d closed") % {"on_time": on_time, "closed": this_week},
        },
    )


def _cycle_card(user, workspace, closed_rows, today) -> dict | None:
    """Return the running cycle's card, or ``None`` when none is running.

    The pace is forecast the same way a milestone's is (ADR 0038): replay
    the viewer's own recent days against what the cycle still owes them.

    The throughput sampled is their work across the workspace, not only
    the work inside this cycle — a cycle three days old has no history of
    its own, while the person running it has a fortnight of it, and
    refusing to answer because the container is new would be answering
    the wrong question.

    Args:
        user: The viewer.
        workspace: The active workspace, or ``None``.
        closed_rows: What :func:`_closed_recently` returned.
        today: Reference date.

    Returns:
        A card dict, or ``None``.
    """
    from apps.cycles.models import Cycle

    if workspace is None:
        return None
    cycle = (
        Cycle.objects.filter(workspace=workspace, start_date__lte=today, end_date__gte=today)
        .order_by("start_date")
        .first()
    )
    if cycle is None:
        return None
    counts = (
        Task.objects.work()
        .filter(counted_q(), assignee=user, cycle=cycle)
        .aggregate(
            total=Count("id"),
            done=Count("id", filter=Q(status=Task.STATUS_DONE)),
        )
    )
    total, done = counts["total"], counts["done"]
    left = max(0, (cycle.end_date - today).days)
    days_left = ngettext("%(count)d day left", "%(count)d days left", left) % {"count": left}
    return {
        "label": cycle.display_name,
        "value": f"{done}/{total}",
        "tone": "",
        "sub": f"{days_left} · {_cycle_pace(closed_rows, total, total - done, cycle, today)}",
    }


def _cycle_pace(closed_rows, total: int, remaining: int, cycle, today) -> str:
    """Say whether the viewer's share of the cycle lands inside it.

    Args:
        closed_rows: What :func:`_closed_recently` returned.
        total: The viewer's counted work in the cycle, finished or not.
        remaining: The part of it still unfinished.
        cycle: The running cycle.
        today: Reference date.

    Returns:
        A translated fragment for the card's second line.
    """
    if not total:
        # No work of theirs in it is not the same as having finished it,
        # and "all yours done" over 0/0 is the kind of green that teaches
        # people to stop reading the card.
        return str(_("nothing of yours in it"))
    if remaining <= 0:
        return str(_("all yours done"))
    days = {row["closed"] for row in closed_rows}
    outlook = forecast.forecast(
        closes=forecast.daily_closes({index: row["closed"] for index, row in enumerate(closed_rows)}, today),
        remaining=remaining,
        history_days=(today - min(days)).days if days else 0,
        today=today,
        target=cycle.end_date,
        seed=cycle.id,
    )
    if outlook["state"] != "ready":
        return str(_("too little history to forecast"))
    return _("%(chance)d%% to finish yours") % {"chance": outlook["chance"]}


#: How many unread notifications one person's card lists before it stops.
PERSON_ROWS = 3

#: How many entries "Since yesterday" shows.
FEED_LIMIT = 8


def people(user, workspace, tasks) -> list[dict]:
    """Return the people this work actually runs through.

    Not a team list — only the people something is pending with, in the
    order of how much is pending: someone held up by the viewer first,
    then someone the viewer is held up by, then someone who wrote and
    has not been answered.

    Args:
        user: The viewer.
        workspace: The active workspace, or ``None``.
        tasks: Their open tasks, from :func:`focus_tasks`.

    Returns:
        Row dicts with ``person``, ``rows``, ``chips``, ``weight``.
    """
    holding: dict = {}
    for task in tasks:
        for blocked in task.focus_blocks:
            _add_person(holding, blocked.assignee, "waits", task, blocked)
        for blocker in task.focus_blocked_by:
            _add_person(holding, blocker.assignee, "waited", task, blocker)
    for notification in _unread(user, workspace):
        _add_person(holding, notification.actor, "wrote", None, notification)
    rows = [row for row in holding.values() if row["rows"]]
    for row in rows:
        row["weight"] = row["waits"] * 3 + row["waited"] * 2 + row["wrote"]
        row["chips"] = _person_chips(row)
        row["rows"] = row["rows"][: PERSON_ROWS * 2]
    rows.sort(key=lambda row: (-row["weight"], row["person"].display_name))
    return rows


def _unread(user, workspace):
    """Return the viewer's unread notifications that name a person.

    Args:
        user: The viewer.
        workspace: The active workspace, or ``None``.

    Returns:
        A list of :class:`~apps.notifications.models.Notification`.
    """
    from apps.notifications.models import Notification

    queryset = Notification.objects.filter(
        recipient=user,
        is_read=False,
        archived_at__isnull=True,
        actor__isnull=False,
    ).select_related("actor", "task__project")
    if workspace is not None:
        queryset = queryset.filter(workspace=workspace)
    return list(queryset.order_by("-created_at")[:20])


def _add_person(holding: dict, person, kind: str, task, other) -> None:
    """File one pending thing under the person it is pending with.

    Args:
        holding: The accumulator, keyed by user id.
        person: The other person, or ``None`` for unassigned work.
        kind: ``waits`` / ``waited`` / ``wrote``.
        task: The viewer's task, where there is one.
        other: The task or notification on the other side.
    """
    if person is None:
        return
    row = holding.setdefault(
        person.id,
        {
            "person": person,
            "rows": [],
            "waits": 0,
            "waited": 0,
            "wrote": 0,
        },
    )
    row[kind] += 1
    row["rows"].append(
        {
            "kind": kind,
            "task": task,
            "other": other,
        },
    )


def _person_chips(row) -> list[dict]:
    """Return the chips summarising what is pending with one person.

    Args:
        row: The accumulated person row.

    Returns:
        Chip dicts with ``text`` and ``tone``, zeros left out.
    """
    chips = []
    if row["waits"]:
        chips.append(
            {
                "text": _("you block %(count)d") % {"count": row["waits"]},
                "tone": "bg-rose-500/15 text-rose-300",
            },
        )
    if row["waited"]:
        chips.append(
            {
                "text": _("blocks you %(count)d") % {"count": row["waited"]},
                "tone": "bg-amber-500/10 text-amber-300",
            },
        )
    if row["wrote"]:
        chips.append(
            {
                "text": ngettext("%(count)d unread", "%(count)d unread", row["wrote"]) % {"count": row["wrote"]},
                "tone": "bg-brand-500/15 text-brand-300",
            },
        )
    return chips


def week(tasks, today=None) -> list[dict]:
    """Return the next five working days and what lands on each.

    Weekends are skipped rather than drawn empty: a column that is
    always blank teaches the reader to skip the row it is in.

    Args:
        tasks: The viewer's open tasks, from :func:`focus_tasks`.
        today: Reference date; defaults to the local current date.

    Returns:
        Day dicts with ``date``, ``is_today``, ``tasks`` and
        ``milestones``.
    """
    today = today or timezone.localdate()
    days = []
    for offset in range(WEEK_LOOKAHEAD):
        if len(days) == WEEK_COLUMNS:
            break
        day = today + datetime.timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        landing = [task for task in tasks if task.due_date == day]
        milestones = {
            task.milestone.id: task.milestone
            for task in tasks
            if task.milestone and not task.milestone.is_closed and task.milestone.target_date == day
        }
        days.append(
            {
                "date": day,
                "offset": offset,
                "is_today": offset == 0,
                "label": _day_label(day, offset),
                "tasks": landing,
                "milestones": sorted(milestones.values(), key=lambda milestone: milestone.name),
                "heavy": len(landing) >= 3,
            },
        )
    return days


def _day_label(day, offset) -> str:
    """Name a day the way a person would say it.

    Args:
        day: The date.
        offset: Days from today.

    Returns:
        ``Today`` / ``Tomorrow`` / ``Mon 14``.
    """
    if not offset:
        return str(_("Today"))
    if offset == 1:
        return str(_("Tomorrow"))
    return f"{day:%a} {day.day}"


def calls(meetings, tasks, today=None) -> list[dict]:
    """Return the viewer's next calls, each with what to bring to it.

    A call whose title names a milestone the viewer owes work to is the
    one they most need to prepare for, so the card says how much of that
    work is still open rather than making them go and look.

    Args:
        meetings: Their upcoming meetings, soonest first.
        tasks: Their open tasks, from :func:`focus_tasks`.
        today: Reference date; defaults to the local current date.

    Returns:
        Row dicts with ``meeting``, ``label`` and ``prep``.
    """
    today = today or timezone.localdate()
    rows = []
    for meeting in meetings:
        day = timezone.localtime(meeting.happened_at).date()
        offset = (day - today).days
        if offset < 0 or offset > CALLS_DAYS - 1:
            continue
        rows.append(
            {
                "meeting": meeting,
                "offset": offset,
                "label": _day_label(day, offset),
                "prep": _call_prep(meeting, tasks, today),
            },
        )
    return rows


def _call_prep(meeting, tasks, today) -> dict:
    """Return what the viewer owes the date this call is about.

    Args:
        meeting: The call.
        tasks: The viewer's open tasks.
        today: Reference date.

    Returns:
        ``text`` and ``tone``.
    """
    title = meeting.title.lower()
    for task in tasks:
        milestone = task.milestone
        if milestone is None or milestone.is_closed or milestone.name.lower() not in title:
            continue
        theirs = [other for other in tasks if other.milestone_id == milestone.id]
        behind = [
            other
            for other in theirs
            if other.due_date and (other.due_date > milestone.target_date or other.due_date < today)
        ]
        text = ngettext(
            "%(name)s · %(count)d of yours open",
            "%(name)s · %(count)d of yours open",
            len(theirs),
        ) % {"name": milestone.name, "count": len(theirs)}
        if behind:
            text += str(_(", %(count)d late") % {"count": len(behind)})
        return {
            "text": text,
            "tone": "text-rose-400" if behind else "text-placeholder-foreground",
        }
    return {
        "text": _("no milestone linked"),
        "tone": "text-placeholder-foreground",
    }


def feed(user, workspace):
    """Return what other people did on the viewer's work lately.

    Args:
        user: The viewer.
        workspace: The active workspace, or ``None``.

    Returns:
        A list of :class:`~apps.notifications.models.Notification`,
        newest first.
    """
    from apps.notifications.models import Notification

    queryset = Notification.objects.filter(
        recipient=user,
        archived_at__isnull=True,
        actor__isnull=False,
    ).select_related("actor", "task__project")
    if workspace is not None:
        queryset = queryset.filter(workspace=workspace)
    return list(queryset.order_by("-created_at")[:FEED_LIMIT])
