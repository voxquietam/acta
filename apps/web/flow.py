"""Why work is slow — the three blocks that sit under the Insights numbers.

The numbers above them answer "how much" and "how fast". These answer
"what is it waiting on", which is the question a team actually argues
about, and which nothing in Acta could answer before.

* **Wait vs work** — for the work that finished recently, how much of
  its life was spent being worked on and how much was spent waiting.
  The honest figure is usually between a tenth and a fifth, and seeing
  it once changes what people try to fix.
* **Blocked radar** — what is held up right now, for how long, and by
  whom. Dependencies were visible as a graph but never priced in days.
* **Bounces** — work moving backwards through the pipeline. Reopening
  out of done was already counted; ``in-review → in-progress`` is the
  one that actually says the review lane is the problem, and nothing
  counted it.

Everything is replayed from the activity log, which ADR 0026 already
made load-bearing for analytics. No new tables, no snapshots, no cron.

Both scopes run through here: one project, or a whole workspace. The
scope drives every query including the refusals, so a block can answer
for the workspace while refusing for a single project inside it — which
is the honest outcome, not a bug.

See docs/decisions/0026-scrumban-metrics.md.
"""

from collections import defaultdict
import datetime
from typing import Any

from django.db.models import Q
from django.utils import timezone

from apps.activity.models import ActivityLog
from apps.tasks.models import Task

#: The windows the page offers.
RANGE_WEEKS = [
    4,
    8,
    12,
]

#: Two months: long enough to hold a rhythm, short enough to forget a
#: quarter that has nothing to do with how the team works now.
DEFAULT_WEEKS = 8

#: Tasks that must have finished inside the window before the stage
#: split says anything. Below this it is three people's fortnight, not
#: a pattern.
NEED_CLOSED = 10

#: Status changes needed before a bounce rate is a rate rather than an
#: anecdote.
NEED_MOVES = 20

#: The stages a task waits in, in pipeline order. ``done`` is the exit,
#: not a stage, and ``cancelled`` is not on the path at all.
STAGES = [
    Task.STATUS_PLANNED,
    Task.STATUS_READY,
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]

#: The one stage that is work. Everything else in STAGES is a queue.
WORK_STAGE = Task.STATUS_IN_PROGRESS

#: Where each status sits on the path, for deciding what "backwards" is.
_PIPELINE_ORDER = {status: index for index, status in enumerate(STAGES + [Task.STATUS_DONE])}

#: Statuses that mean a task is no longer waiting on anything.
_CLOSED_STATUSES = [
    Task.STATUS_DONE,
    Task.STATUS_CANCELLED,
]

#: Reasons a status moved backwards that are not a bounce. Retiring the
#: Ready column rewrites every ready task to planned in one administrative
#: act (see ``workspaces.services.retire_ready_status``): real events, and
#: correctly logged, but counting them as work going backwards would say
#: a team rejected two hundred tasks on the day someone turned a column
#: off. The log marks them, so the reading can tell them apart.
_NOT_A_BOUNCE = [
    "ready_column_retired",
]

_SECONDS_PER_DAY = 86400.0


def resolve_weeks(raw) -> int:
    """Return a supported window, falling back to the default.

    Args:
        raw: Whatever arrived in the query string — ``"8"``, ``"8w"``,
            ``None`` or nonsense.

    Returns:
        One of :data:`RANGE_WEEKS`.
    """
    try:
        weeks = int(str(raw).rstrip("w"))
    except (TypeError, ValueError):
        return DEFAULT_WEEKS
    return weeks if weeks in RANGE_WEEKS else DEFAULT_WEEKS


def _log_scope(workspace, project) -> dict:
    """Return the filter that narrows activity rows to the scope.

    Args:
        workspace: The workspace being measured.
        project: One project inside it, or ``None`` for all of them.

    Returns:
        Keyword arguments for ``ActivityLog.objects.filter``.
    """
    return {"project": project} if project is not None else {"workspace": workspace}


def _task_scope(workspace, project) -> dict:
    """Return the filter that narrows tasks to the scope.

    Args:
        workspace: The workspace being measured.
        project: One project inside it, or ``None`` for all of them.

    Returns:
        Keyword arguments for ``Task.objects.filter``.
    """
    return {"project": project} if project is not None else {"project__workspace": workspace}


def _status_events(workspace, project=None, *, since=None, task_ids=None) -> dict:
    """Replay status changes, oldest first, in one query.

    Args:
        workspace: The workspace being measured.
        project: One project inside it, or ``None``.
        since: Only read changes at or after this moment.
        task_ids: Only read these tasks. The workspace scope is the hot
            case, so a caller that already knows which tasks it cares
            about must never pull the whole history to find them.

    Returns:
        ``{task_id: [(when, from_status, to_status, reason), ...]}``,
        where ``reason`` is whatever the writer stamped on the change —
        the marker that tells an administrative rewrite from a decision.
    """
    rows = ActivityLog.objects.filter(
        target_type=ActivityLog.TARGET_TASK,
        event_type="task.status_changed",
        **_log_scope(workspace, project),
    )
    if since is not None:
        rows = rows.filter(created_at__gte=since)
    if task_ids is not None:
        rows = rows.filter(target_id__in=list(task_ids))
    history = defaultdict(list)
    for task_id, payload, when in rows.order_by("created_at", "id").values_list(
        "target_id",
        "payload",
        "created_at",
    ):
        payload = payload or {}
        history[task_id].append((when, payload.get("from"), payload.get("to"), payload.get("reason")))
    return history


def wait_vs_work(workspace, project=None, *, weeks: int, now=None) -> dict[str, Any]:
    """Return where the time went for work that finished in the window.

    A task counts when its last move into done falls inside the window
    and it is still done. Its life is replayed from ``created_at``, and
    the stage it started in is the ``from`` of its first logged change.
    Time spent sitting in done before a reopen belongs to no stage — it
    was neither work nor a queue.

    The read is bounded before it begins: one indexed query finds the
    tasks that closed inside the window, and only their history is
    replayed. Reading every status change in a workspace to find the
    forty that mattered is how an insights page becomes a page nobody
    opens.

    Args:
        workspace: The workspace being measured.
        project: One project inside it, or ``None``.
        weeks: Width of the trailing window.
        now: Reference moment; defaults to now.

    Returns:
        ``{"ok": False, ...}`` with the shortfall when too little
        finished, otherwise ``ok`` plus ``efficiency``, the per-stage
        ``segments`` and the longest queue.
    """
    now = now or timezone.now()
    since = now - datetime.timedelta(weeks=weeks)
    closed_ids = set(
        ActivityLog.objects.filter(
            target_type=ActivityLog.TARGET_TASK,
            event_type="task.status_changed",
            payload__to=Task.STATUS_DONE,
            created_at__gte=since,
            **_log_scope(workspace, project),
        ).values_list("target_id", flat=True),
    )
    created_days = dict(
        Task.objects.work()
        .filter(
            id__in=closed_ids,
            status=Task.STATUS_DONE,
            **_task_scope(workspace, project),
        )
        .values_list("id", "created_at"),
    )
    history = _status_events(workspace, project, task_ids=created_days.keys()) if created_days else {}

    totals = dict.fromkeys(STAGES, 0.0)
    counted = 0
    for task_id, created in created_days.items():
        events = history.get(task_id)
        if not events:
            continue
        last_done = max((index for index, event in enumerate(events) if event[2] == Task.STATUS_DONE), default=None)
        if last_done is None or events[last_done][0] < since:
            continue
        counted += 1
        cursor, stage = created, events[0][1]
        for when, _from_status, to_status, _reason in events[: last_done + 1]:
            if stage in totals:
                totals[stage] += max(0.0, (when - cursor).total_seconds()) / _SECONDS_PER_DAY
            cursor, stage = when, to_status

    window = {
        "weeks": weeks,
        "since": since,
        "until": now,
    }
    if counted < NEED_CLOSED:
        return {
            "ok": False,
            "counted": counted,
            "need": NEED_CLOSED,
            **window,
        }
    average = {stage: totals[stage] / counted for stage in STAGES}
    lifetime = sum(average.values())
    working = average[WORK_STAGE]
    segments = [
        {
            "status": stage,
            "label": Task.STATUS_LABELS[stage],
            "days": round(average[stage], 1),
            "share": round(average[stage] / lifetime * 100) if lifetime else 0,
            # A stage that took almost no time still needs a sliver, or
            # the bar reads as though the stage does not exist.
            "weight": max(average[stage], 0.15),
            "is_work": stage == WORK_STAGE,
        }
        for stage in STAGES
    ]
    longest = max((segment for segment in segments if not segment["is_work"]), key=lambda segment: segment["days"])
    return {
        "ok": True,
        "counted": counted,
        "efficiency": round(working / lifetime * 100) if lifetime else 0,
        "work_days": round(working, 1),
        "life_days": round(lifetime, 1),
        "segments": segments,
        "longest": longest,
        "longest_vs_work": round(longest["days"] / working, 1) if working else None,
        **window,
    }


def _blocked_since(workspace, pairs: set, slug_to_id: dict, task_ids) -> dict:
    """Return when each block began, as far as the log can say.

    The ``blocks`` relation carries no timestamp of its own, but adding
    it was logged. ``kind`` says which way round the pair was written:
    ``blocks`` means the acting task holds the target, ``blocked_by``
    the reverse. Re-adding a link after removing it moves the clock
    forward, which is right — the wait started again.

    Args:
        workspace: The workspace being measured.
        pairs: ``{(blocker_id, blocked_id)}`` to date.
        slug_to_id: Slugs of the tasks involved, mapped to their ids.
        task_ids: The tasks whose link events to read.

    Returns:
        ``{(blocker_id, blocked_id): when}`` for the pairs the log knows.
    """
    started: dict[tuple[int, int], datetime.datetime] = {}
    rows = ActivityLog.objects.filter(
        workspace=workspace,
        event_type="task.link_added",
        target_type=ActivityLog.TARGET_TASK,
        payload__kind__in=[
            "blocks",
            "blocked_by",
        ],
        target_id__in=list(task_ids),
    ).values_list("target_id", "payload", "created_at")
    for acting_id, payload, when in rows:
        payload = payload or {}
        other_id = slug_to_id.get(payload.get("target_slug"))
        if other_id is None:
            continue
        pair = (acting_id, other_id) if payload.get("kind") == "blocks" else (other_id, acting_id)
        if pair in pairs and when > started.get(pair, when - datetime.timedelta(seconds=1)):
            started[pair] = when
    return started


def blocked_radar(workspace, project=None, *, now=None, top: int = 6) -> dict[str, Any]:
    """Return what is held up right now, and what it costs in days.

    Unlike the other two blocks this is a photograph, not a window: a
    block that was cleared last week is not what anyone needs to hear
    about. Only pairs where both ends are open count — a blocker that
    finished is not holding anything, whatever the link still says.

    In a project scope a link counts when EITHER end sits in the
    project. Being held up by another team's work is still this
    project's wait, and the version that only looked inward would stay
    silent about exactly the blocks nobody here can clear alone.

    Args:
        workspace: The workspace being measured.
        project: One project inside it, or ``None``.
        now: Reference moment; defaults to now.
        top: How many blockers to list.

    Returns:
        ``{"ok": False, "any_links": bool}`` when nothing is blocked,
        otherwise the counts, the ranked blockers and the longest chain.
    """
    now = now or timezone.now()
    links = Task.blocks.through.objects.filter(from_task__project__workspace=workspace)
    if project is not None:
        links = links.filter(Q(from_task__project=project) | Q(to_task__project=project))
    pairs = list(links.values_list("from_task_id", "to_task_id"))
    open_tasks = {
        task.id: task
        for task in Task.objects.work()
        .filter(id__in={task_id for pair in pairs for task_id in pair}, archived_at__isnull=True)
        .exclude(status__in=_CLOSED_STATUSES)
        .select_related("project", "assignee")
    }
    live = [(blocker, blocked) for blocker, blocked in pairs if blocker in open_tasks and blocked in open_tasks]
    if not live:
        return {
            "ok": False,
            "any_links": bool(pairs),
        }

    started = _blocked_since(
        workspace,
        set(live),
        {task.slug: task.id for task in open_tasks.values()},
        open_tasks.keys(),
    )
    estimated = 0
    held_days = {}
    for pair in live:
        when = started.get(pair)
        if when is None:
            # The link predates logging. It cannot have started before
            # both tasks existed, which is the latest honest guess.
            estimated += 1
            when = max(open_tasks[pair[0]].created_at, open_tasks[pair[1]].created_at)
        held_days[pair] = max(0, int((now - when).total_seconds() // _SECONDS_PER_DAY))

    waiting = defaultdict(list)
    holding = defaultdict(list)
    for (blocker, blocked), days in held_days.items():
        waiting[blocked].append(days)
        holding[blocker].append((blocked, days))
    rows = sorted(
        (
            {
                "task": open_tasks[blocker],
                "holds": len(held),
                "held_slugs": ", ".join(open_tasks[blocked].slug for blocked, _days in held),
                # Summed, not maxed: one task holding five others for a
                # week each is a worse problem than one holding a single
                # task for a month, and sorting by count alone hides it.
                "days": sum(days for _blocked, days in held),
            }
            for blocker, held in holding.items()
        ),
        key=lambda row: (-row["days"], -row["holds"]),
    )
    peak = rows[0]["days"] or 1
    for row in rows:
        row["pct"] = round(row["days"] / peak * 100)
    return {
        "ok": True,
        "blocked_now": len(waiting),
        "blockers": len(holding),
        # A task waits from the oldest block on it, not from the sum of
        # them: it is one task, standing still, since one moment.
        "standing_days": sum(max(days) for days in waiting.values()),
        "rows": rows[:top],
        "chain": _longest_chain(live, open_tasks),
        "estimated_links": estimated,
        "links": len(live),
    }


def _longest_chain(pairs: list, tasks: dict) -> list:
    """Return the longest run of blocks, A holds B holds C.

    Args:
        pairs: ``[(blocker_id, blocked_id), ...]`` of live blocks.
        tasks: The tasks involved, by id.

    Returns:
        The tasks along the longest chain, blocker first.
    """
    onwards = defaultdict(list)
    for blocker, blocked in pairs:
        onwards[blocker].append(blocked)
    seen: dict[int, list] = {}

    def walk(task_id, visiting):
        """Return the longest chain starting at one task."""
        if task_id in seen:
            return seen[task_id]
        best = [task_id]
        for nxt in onwards.get(task_id, []):
            # Defensive only: the write path rejects a direct cycle, but
            # a longer one would hang this walk rather than fail it.
            if nxt in visiting:
                continue
            candidate = [task_id] + walk(nxt, visiting | {nxt})
            if len(candidate) > len(best):
                best = candidate
        seen[task_id] = best
        return best

    longest = max((walk(task_id, {task_id}) for task_id in onwards), key=len)
    return [tasks[task_id] for task_id in longest]


def bounces(workspace, project=None, *, weeks: int, now=None, top: int = 3) -> dict[str, Any]:
    """Return the work that moved backwards through the pipeline.

    Reopening — leaving done — was already reported on its own, and it
    keeps exactly its old meaning here so a number people recognise does
    not quietly change under them. What is new is everything else: a
    task sent back from review to in-progress never counted anywhere,
    and it is the one that says where the queue really is.

    Args:
        workspace: The workspace being measured.
        project: One project inside it, or ``None``.
        weeks: Width of the trailing window.
        now: Reference moment; defaults to now.
        top: How many repeat offenders to name.

    Returns:
        ``{"ok": False, ...}`` below :data:`NEED_MOVES`, otherwise the
        totals, the ranked transitions and the reopen line.
    """
    now = now or timezone.now()
    since = now - datetime.timedelta(weeks=weeks)
    history = _status_events(workspace, project, since=since)
    moves = sum(len(events) for events in history.values())
    window = {
        "weeks": weeks,
        "since": since,
        "until": now,
        "moves": moves,
        "tasks": len(history),
    }
    if moves < NEED_MOVES:
        return {
            "ok": False,
            "need": NEED_MOVES,
            **window,
        }

    transitions = defaultdict(int)
    per_task = defaultdict(lambda: defaultdict(int))
    for task_id, events in history.items():
        for _when, from_status, to_status, reason in events:
            if reason in _NOT_A_BOUNCE:
                continue
            if from_status in _PIPELINE_ORDER and to_status in _PIPELINE_ORDER:
                if _PIPELINE_ORDER[to_status] < _PIPELINE_ORDER[from_status]:
                    transitions[(from_status, to_status)] += 1
                    per_task[task_id][(from_status, to_status)] += 1

    completed, reopened = set(), set()
    for task_id, events in history.items():
        finished = False
        for _when, from_status, to_status, _reason in events:
            if to_status == Task.STATUS_DONE:
                completed.add(task_id)
                finished = True
            elif from_status == Task.STATUS_DONE and finished:
                reopened.add(task_id)

    # Leaving done is reported on the reopen line, so it does not also
    # appear as the tallest bar and get counted twice by the reader.
    bars = {pair: count for pair, count in transitions.items() if pair[0] != Task.STATUS_DONE}
    ranked = sorted(bars.items(), key=lambda item: -item[1])
    shown, rest = ranked[:3], sum(count for _pair, count in ranked[3:])
    peak = shown[0][1] if shown else 1
    total = sum(transitions.values())
    repeat_ids = sorted(per_task, key=lambda task_id: -sum(per_task[task_id].values()))[:top]
    repeat_tasks = {task.id: task for task in Task.objects.filter(id__in=repeat_ids).select_related("project")}
    return {
        "ok": True,
        "total": total,
        "rate": round(total / moves * 100, 1) if moves else 0,
        "kinds": [
            {
                "from_label": Task.STATUS_LABELS[from_status],
                "to_label": Task.STATUS_LABELS[to_status],
                "count": count,
                "pct": round(count / peak * 100),
                "share": round(count / total * 100) if total else 0,
            }
            for (from_status, to_status), count in shown
        ],
        "other": rest,
        "review_back": transitions.get((Task.STATUS_IN_REVIEW, Task.STATUS_IN_PROGRESS), 0),
        "reopen": {
            "completed": len(completed),
            "reopened": len(reopened),
            "rate": round(len(reopened) / len(completed) * 100, 1) if completed else None,
        },
        "repeat": [
            {
                "task": repeat_tasks[task_id],
                "count": sum(per_task[task_id].values()),
                "path": "%s → %s"
                % (
                    Task.STATUS_LABELS[max(per_task[task_id], key=per_task[task_id].get)[0]],
                    Task.STATUS_LABELS[max(per_task[task_id], key=per_task[task_id].get)[1]],
                ),
                "mixed": len(per_task[task_id]) > 1,
            }
            for task_id in repeat_ids
            if task_id in repeat_tasks
        ],
        **window,
    }


def build_flow_context(workspace, project=None, *, weeks: int) -> dict[str, Any]:
    """Return the three flow blocks for one scope and window.

    Args:
        workspace: The workspace being measured.
        project: One project inside it, or ``None``.
        weeks: Width of the trailing window.

    Returns:
        ``{"wait_work", "blocked", "bounces"}``, each either answered or
        carrying its own refusal.
    """
    now = timezone.now()
    return {
        "wait_work": wait_vs_work(workspace, project, weeks=weeks, now=now),
        "blocked": blocked_radar(workspace, project, now=now),
        "bounces": bounces(workspace, project, weeks=weeks, now=now),
    }
