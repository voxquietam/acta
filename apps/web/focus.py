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

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from apps.tasks.models import Task

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
    from django.db.models import OuterRef, Prefetch, Subquery

    from apps.activity.models import ActivityLog

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
        .select_related("project__workspace", "milestone")
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
