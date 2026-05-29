"""Workspace dashboard context builder.

One module so :class:`apps.web.views.DashboardView` stays thin. Every
aggregate is keyed off the active workspace's task set and respects the
``range`` window (7d / 14d / 30d / 90d). Queries are written to stay
O(1) in count regardless of task volume: a handful of ``aggregate`` /
``values().annotate()`` calls plus one bulk ``values()`` pull that is
bucketed per-member in Python (small-team workloads — a few hundred
tasks — so the in-Python pass is cheap and avoids a fragile annotate
chain per metric).

Notes / honest approximations:

* There is no ``status_changed_at`` column (status history lives in the
  activity log). "stuck in-review" / "urgent stale" / "idle member" use
  ``updated_at`` as a last-touched proxy — good enough for a nudge, not
  a forensic timestamp.
* Story points reuse the Fibonacci ``size`` field directly.
* The in-flight sparkline is reconstructed backwards from the current
  open count and the daily created/done deltas — a trend, not an exact
  historical snapshot.
"""

from __future__ import annotations

from datetime import timedelta
import statistics

from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractHour, ExtractIsoWeekDay, TruncDate, TruncWeek
from django.utils import timezone

from apps.activity.models import ActivityLog
from apps.tasks.models import Task

RANGE_DAYS = {
    "7d": 7,
    "14d": 14,
    "30d": 30,
    "90d": 90,
}
DEFAULT_RANGE = "14d"

# Active (non-terminal) statuses, in pipeline order.
PIPELINE_STATUSES = [
    Task.STATUS_PLANNED,
    Task.STATUS_READY,
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]
# Matrix columns mirror the design (ready folded into to-do visually is
# avoided — we surface the real ready bucket only inside "open").
MATRIX_STATUSES = [
    Task.STATUS_PLANNED,
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
    Task.STATUS_DONE,
]
OPEN_STATUSES = [
    Task.STATUS_PLANNED,
    Task.STATUS_READY,
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]
WIP_STATUSES = [
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]

STATUS_LABEL = {
    Task.STATUS_PLANNED: "planned",
    Task.STATUS_READY: "ready",
    Task.STATUS_TODO: "to-do",
    Task.STATUS_IN_PROGRESS: "in-progress",
    Task.STATUS_IN_REVIEW: "in-review",
    Task.STATUS_DONE: "done",
}
STATUS_COLOR = {
    Task.STATUS_PLANNED: "rgb(113, 113, 122)",
    Task.STATUS_READY: "rgb(100, 116, 139)",
    Task.STATUS_TODO: "rgb(59, 130, 246)",
    Task.STATUS_IN_PROGRESS: "rgb(139, 92, 246)",
    Task.STATUS_IN_REVIEW: "rgb(245, 158, 11)",
    Task.STATUS_DONE: "rgb(16, 185, 129)",
}
PRIORITY_META = [
    (Task.URGENT, "1 Urgent", "rgb(239, 68, 68)"),
    (Task.HIGH, "2 High", "rgb(251, 146, 60)"),
    (Task.MEDIUM, "3 Medium", "rgb(251, 191, 36)"),
    (Task.LOW, "4 Low", "rgb(165, 180, 217)"),
    (Task.NO_PRIORITY, "5 None", "rgb(113, 113, 122)"),
]
# Project ``icon_color`` token -> hex (Tailwind 500). Falls back to brand.
ICON_HEX = {
    "red": "#ef4444",
    "orange": "#f97316",
    "amber": "#f59e0b",
    "yellow": "#eab308",
    "lime": "#84cc16",
    "green": "#22c55e",
    "emerald": "#10b981",
    "teal": "#14b8a6",
    "cyan": "#06b6d4",
    "sky": "#0ea5e9",
    "blue": "#3b82f6",
    "indigo": "#6366f1",
    "violet": "#8b5cf6",
    "purple": "#a855f7",
    "fuchsia": "#d946ef",
    "pink": "#ec4899",
    "rose": "#f43f5e",
    "slate": "#64748b",
    "gray": "#6b7280",
    "zinc": "#71717a",
    "stone": "#78716c",
}
BRAND = "#425af5"


def _icon_hex(token: str) -> str:
    """Map a project ``icon_color`` token to a hex string."""
    return ICON_HEX.get(token or "", BRAND)


def _daily(qs, field, days, end_date):
    """Return per-day counts (length ``days``) ending on ``end_date``."""
    rows = qs.annotate(d=TruncDate(field)).values("d").annotate(n=Count("id"))
    bucket = {r["d"]: r["n"] for r in rows if r["d"] is not None}
    return [bucket.get(end_date - timedelta(days=days - 1 - i), 0) for i in range(days)]


def _delta(curr, prev):
    """Return (abs, pct, css_class) describing curr vs prev."""
    diff = curr - prev
    pct = round((diff / prev) * 100) if prev else (100 if curr else 0)
    cls = "up" if diff > 0 else "down" if diff < 0 else "flat"
    return diff, pct, cls


def _hint(kind, value, pct):
    """Threshold-rule "why" copy for a KPI tile."""
    if kind == "created":
        if pct >= 15:
            return "warn", "Demand is rising fast — make sure capacity scales or close older tickets first."
        if pct <= -15:
            return "good", "Intake is cooling — a good moment to drain the backlog."
        return "mute", "Steady intake."
    if kind == "done":
        if pct >= 10:
            return "good", "Throughput climbing — keep the review lane clear to sustain it."
        if pct <= -10:
            return "warn", "Throughput slipping — check what's stuck before pulling new work."
        return "mute", "Throughput holding steady."
    if kind == "inflight":
        return "warn", "Open work above a sustainable level pulls focus — clear in-review first."
    return "mute", ""


def build_dashboard_context(workspace, user, range_key=DEFAULT_RANGE):
    """Build the full dashboard context for ``workspace``.

    Args:
        workspace: The active :class:`Workspace`.
        user: The request user (reserved for future per-user scoping).
        range_key: One of ``7d`` / ``14d`` / ``30d`` / ``90d``.

    Returns:
        A context dict consumed by ``templates/web/dashboard.html``.
    """
    range_key = range_key if range_key in RANGE_DAYS else DEFAULT_RANGE
    days = RANGE_DAYS[range_key]
    now = timezone.now()
    today = timezone.localdate()
    since = now - timedelta(days=days)
    prev_since = now - timedelta(days=days * 2)

    tasks = Task.objects.filter(project__workspace=workspace)
    active = tasks.filter(archived_at__isnull=True)

    # ---- KPI tiles -------------------------------------------------------
    # 5 ``COUNT`` calls collapsed into one ``aggregate`` — same numbers, one
    # DB round-trip instead of five. ``in_flight`` adds an archived-aware
    # filter that mirrors the ``active.exclude(...)`` shape from before.
    inflight_excl = [Task.STATUS_PLANNED, Task.STATUS_DONE, Task.STATUS_CANCELLED]
    kpi_counts = tasks.aggregate(
        created=Count("id", filter=Q(created_at__gte=since)),
        created_prev=Count(
            "id",
            filter=Q(created_at__gte=prev_since, created_at__lt=since),
        ),
        done=Count(
            "id",
            filter=Q(status=Task.STATUS_DONE, completed_at__gte=since),
        ),
        done_prev=Count(
            "id",
            filter=Q(
                status=Task.STATUS_DONE,
                completed_at__gte=prev_since,
                completed_at__lt=since,
            ),
        ),
        in_flight=Count(
            "id",
            filter=Q(archived_at__isnull=True) & ~Q(status__in=inflight_excl),
        ),
    )
    created = kpi_counts["created"]
    created_prev = kpi_counts["created_prev"]
    done = kpi_counts["done"]
    done_prev = kpi_counts["done_prev"]
    in_flight = kpi_counts["in_flight"]

    member_users = list(workspace.memberships.select_related("user").all())
    member_count = len(member_users)

    created_spark = _daily(tasks.filter(created_at__gte=since), "created_at", days, today)
    done_spark = _daily(
        tasks.filter(status=Task.STATUS_DONE, completed_at__gte=since),
        "completed_at",
        days,
        today,
    )
    # In-flight reconstructed backwards from the current open count.
    inflight_spark = [0] * days
    inflight_spark[-1] = in_flight
    for i in range(days - 2, -1, -1):
        inflight_spark[i] = max(0, inflight_spark[i + 1] - (created_spark[i + 1] - done_spark[i + 1]))

    # Active people: distinct assignees who closed >= 1 task in the window.
    closed_pairs = list(
        tasks.filter(status=Task.STATUS_DONE, completed_at__gte=since, assignee__isnull=False)
        .annotate(d=TruncDate("completed_at"))
        .values_list("d", "assignee_id")
    )
    active_people = len({a for _, a in closed_pairs})
    per_day_people = {}
    for d, a in closed_pairs:
        per_day_people.setdefault(d, set()).add(a)
    active_spark = [len(per_day_people.get(today - timedelta(days=days - 1 - i), ())) for i in range(days)]

    c_diff, c_pct, c_cls = _delta(created, created_prev)
    d_diff, d_pct, d_cls = _delta(done, done_prev)
    c_hint_cls, c_hint = _hint("created", created, c_pct)
    d_hint_cls, d_hint = _hint("done", done, d_pct)
    if_hint_cls, if_hint = _hint("inflight", in_flight, 0)

    kpis = [
        {
            "key": "created",
            "label": f"Created · last {range_key}",
            "icon": "square-plus",
            "accent": "acc-brand",
            "value": created,
            "delta": f"{'+' if c_diff >= 0 else ''}{c_pct}%",
            "delta_cls": c_cls,
            "spark": created_spark,
            "hint_cls": c_hint_cls,
            "hint": c_hint,
        },
        {
            "key": "done",
            "label": f"Done · last {range_key}",
            "icon": "circle-check-big",
            "accent": "acc-emerald",
            "value": done,
            "delta": f"{'+' if d_diff >= 0 else ''}{d_pct}%",
            "delta_cls": d_cls,
            "spark": done_spark,
            "spark_opts": '{"stroke":"rgb(110,231,183)","fill":"rgba(16,185,129,0.18)"}',
            "hint_cls": d_hint_cls,
            "hint": d_hint,
        },
        {
            "key": "inflight",
            "label": "In-flight · now",
            "icon": "circle-dot",
            "accent": "acc-violet",
            "value": in_flight,
            "delta": "open",
            "delta_cls": "flat",
            "spark": inflight_spark,
            "spark_opts": '{"stroke":"rgb(196,181,253)","fill":"rgba(139,92,246,0.18)"}',
            "hint_cls": if_hint_cls,
            "hint": if_hint,
        },
        {
            "key": "active",
            "label": f"Active people · {range_key}",
            "icon": "users",
            "accent": "acc-amber",
            "value": active_people,
            "delta": f"of {member_count}",
            "delta_cls": "flat",
            "spark": active_spark,
            "spark_opts": '{"stroke":"rgb(252,211,77)","fill":"rgba(245,158,11,0.18)"}',
            "hint_cls": "mute",
            "hint": f"{active_people} of {member_count} members closed work this window.",
        },
    ]

    # ---- Attention alerts ------------------------------------------------
    # 5 ``COUNT`` calls collapsed into one ``aggregate``. Same numbers, one
    # DB round-trip.
    not_done = active.exclude(status__in=[Task.STATUS_DONE, Task.STATUS_CANCELLED])
    stale_before = now - timedelta(days=3)
    review_before = now - timedelta(days=7)
    due_soon_cutoff = today + timedelta(days=3)
    alert_counts = not_done.aggregate(
        overdue=Count("id", filter=Q(due_date__lte=today)),
        due_soon=Count(
            "id",
            filter=Q(due_date__gt=today, due_date__lte=due_soon_cutoff),
        ),
        due_soon_urgent=Count(
            "id",
            filter=Q(
                due_date__gt=today,
                due_date__lte=due_soon_cutoff,
                priority__in=[Task.URGENT, Task.HIGH],
            ),
        ),
        urgent_stale=Count(
            "id",
            filter=Q(priority=Task.URGENT, updated_at__lt=stale_before),
        ),
        stuck_review=Count(
            "id",
            filter=Q(status=Task.STATUS_IN_REVIEW, updated_at__lt=review_before),
        ),
    )
    overdue_n = alert_counts["overdue"]
    due_soon_n = alert_counts["due_soon"]
    due_soon_urgent = alert_counts["due_soon_urgent"]
    urgent_stale_n = alert_counts["urgent_stale"]
    stuck_review_n = alert_counts["stuck_review"]

    alerts = [
        {
            "n": overdue_n,
            "label": "overdue today",
            "hint": "Past due and still open",
            "accent": "rgb(244, 63, 94)",
            "q": "due=overdue",
        },
        {
            "n": due_soon_n,
            "label": "due in 1–3d",
            "hint": f"{due_soon_urgent} are urgent/high",
            "accent": "rgb(251, 146, 60)",
            "q": "due=soon",
        },
        {
            "n": urgent_stale_n,
            "label": "urgent · stale 3d+",
            "hint": "No update in 3+ days",
            "accent": "rgb(239, 68, 68)",
            "q": "priority=1&xstatus=done&xstatus=cancelled",
        },
        {
            "n": stuck_review_n,
            "label": "stuck in-review 7d+",
            "hint": "Untouched in review for a week",
            "accent": "rgb(245, 158, 11)",
            "q": "status=in-review",
        },
    ]

    # ---- Status pipeline -------------------------------------------------
    status_rows = active.exclude(status=Task.STATUS_CANCELLED).values("status").annotate(n=Count("id"))
    status_counts = {r["status"]: r["n"] for r in status_rows}
    open_total = sum(status_counts.get(s, 0) for s in PIPELINE_STATUSES)
    grand_total = open_total + status_counts.get(Task.STATUS_DONE, 0)
    pipe_order = PIPELINE_STATUSES + [Task.STATUS_DONE]
    pipeline = []
    for s in pipe_order:
        n = status_counts.get(s, 0)
        pct = round((n / grand_total) * 100) if grand_total else 0
        pipeline.append(
            {
                "status": s,
                "label": STATUS_LABEL[s],
                "n": n,
                "pct": pct,
                "width": round((n / grand_total) * 100, 1) if grand_total else 0,
                "color": STATUS_COLOR[s],
            }
        )

    # ---- CFD lite (8 ISO weeks, created vs done; count + points) ---------
    cfd = _build_cfd(tasks, now)

    # ---- Project velocity ------------------------------------------------
    velocity = _build_velocity(workspace, tasks, now)

    # ---- Distribution panels --------------------------------------------
    in_flight_qs = active.filter(status__in=WIP_STATUSES)
    dist_project = [
        {
            "id": r["project_id"],
            "nm": r["project__slug_prefix"],
            "color": _icon_hex(r["project__icon_color"]),
            "n": r["n"],
        }
        for r in in_flight_qs.values("project_id", "project__slug_prefix", "project__icon_color")
        .annotate(n=Count("id"))
        .order_by("-n")
    ]
    prio_counts = {
        r["priority"]: r["n"]
        for r in active.exclude(status=Task.STATUS_CANCELLED).values("priority").annotate(n=Count("id"))
    }
    dist_prio = [{"nm": label, "color": color, "n": prio_counts.get(p, 0)} for p, label, color in PRIORITY_META]
    dist_label = [
        {"nm": r["labels__name"], "color": r["labels__color"], "n": r["n"]}
        for r in in_flight_qs.filter(labels__isnull=False)
        .values("labels__name", "labels__color")
        .annotate(n=Count("id"))
        .order_by("-n")[:6]
    ]

    # ---- People: matrix + leaderboard + overloaded + idle ---------------
    members, overloaded, idle = _build_people(workspace, member_users, tasks, now, today)

    # ---- Hygiene ---------------------------------------------------------
    # 4 single-table counts collapsed into one ``aggregate``; the M2M
    # ``labels__isnull`` count stays standalone because mixing it into the
    # aggregate would force a LEFT JOIN onto ``task_labels`` for every
    # filtered count and require ``distinct=True`` to dedupe row inflation.
    # 5 queries → 2.
    hyg_base = active.filter(status__in=WIP_STATUSES)
    hyg_counts = hyg_base.aggregate(
        no_assignee=Count("id", filter=Q(assignee__isnull=True)),
        no_priority=Count("id", filter=Q(priority=Task.NO_PRIORITY)),
        no_due_date=Count("id", filter=Q(due_date__isnull=True)),
        no_description=Count("id", filter=Q(description="")),
    )
    no_labels_n = hyg_base.filter(labels__isnull=True).count()
    hygiene = [
        {
            "key": "no_assignee",
            "label": "no assignee",
            "icon": "user-x",
            "n": hyg_counts["no_assignee"],
            "q": "assignee=unassigned",
        },
        {
            "key": "no_priority",
            "label": "no priority",
            "icon": "circle-dashed",
            "n": hyg_counts["no_priority"],
            "q": "priority=0",
        },
        {
            "key": "no_labels",
            "label": "no labels",
            "icon": "tag",
            "n": no_labels_n,
            "q": "label=none",
        },
        {
            "key": "no_due_date",
            "label": "no due date",
            "icon": "calendar-x",
            "n": hyg_counts["no_due_date"],
            "q": "due=none",
        },
        {
            "key": "no_description",
            "label": "no description",
            "icon": "file-text",
            "n": hyg_counts["no_description"],
            "q": "desc=none",
        },
    ]

    # ---- Activity heatmap (7d x 24h, UTC) --------------------------------
    heatmap = _build_heatmap(workspace, now)

    _wip_mode, _wip_limits = workspace.wip_config()
    wip_inprogress = _wip_limits.get(Task.STATUS_IN_PROGRESS) or 2

    return {
        "dash_range": range_key,
        "dash_wip_inprogress": wip_inprogress,
        "dash_ranges": list(RANGE_DAYS.keys()),
        "dash_member_count": member_count,
        # ``dash_project_count`` counts EVERY project that has at least one
        # task (including done / cancelled / archived) — used for the
        # workspace meta line. ``dist_project`` below counts only projects
        # with in-flight tasks. Distinct semantics; do NOT merge.
        "dash_project_count": tasks.values("project_id").distinct().count(),
        "dash_open_total": open_total,
        "dash_done_total": status_counts.get(Task.STATUS_DONE, 0),
        "kpis": kpis,
        "alerts": alerts,
        "pipeline": pipeline,
        "pipeline_open": open_total,
        "cfd": cfd,
        "velocity": velocity,
        "dist_project": dist_project,
        "dist_prio": dist_prio,
        "dist_label": dist_label,
        "members": members,
        "overloaded": overloaded,
        "idle": idle,
        "hygiene": hygiene,
        "heatmap": heatmap,
        "status_order": MATRIX_STATUSES,
        "status_label_map": STATUS_LABEL,
        "status_color_map": {s: STATUS_COLOR[s] for s in MATRIX_STATUSES},
    }


def _build_cfd(tasks, now):
    """Return created/done weekly series for the last 8 ISO weeks."""
    weeks = 8
    start = now - timedelta(weeks=weeks)
    week_keys = []
    cur = (now - timedelta(days=now.weekday())).date()
    for i in range(weeks - 1, -1, -1):
        week_keys.append(cur - timedelta(weeks=i))
    labels = [f"w{wk.isocalendar().week:02d}" for wk in week_keys]
    idx = {wk: i for i, wk in enumerate(week_keys)}

    # ``Count("id") + Sum("size")`` in one annotate — one query per direction
    # instead of two. ``Sum`` returns ``None`` on an empty group; coalesce in
    # Python rather than wrapping every ``Sum`` in ``Coalesce(Sum(...), 0)``.
    created_count = [0] * weeks
    created_pts = [0] * weeks
    for r in (
        tasks.filter(created_at__gte=start)
        .annotate(w=TruncWeek("created_at"))
        .values("w")
        .annotate(n=Count("id"), pts=Sum("size"))
    ):
        wk = r["w"].date() if hasattr(r["w"], "date") else r["w"]
        if wk in idx:
            created_count[idx[wk]] = r["n"]
            created_pts[idx[wk]] = r["pts"] or 0

    done_count = [0] * weeks
    done_pts = [0] * weeks
    for r in (
        tasks.filter(status=Task.STATUS_DONE, completed_at__gte=start)
        .annotate(w=TruncWeek("completed_at"))
        .values("w")
        .annotate(n=Count("id"), pts=Sum("size"))
    ):
        wk = r["w"].date() if hasattr(r["w"], "date") else r["w"]
        if wk in idx:
            done_count[idx[wk]] = r["n"]
            done_pts[idx[wk]] = r["pts"] or 0

    return {
        "weeks": labels,
        "count": {"created": created_count, "done": done_count},
        "points": {"created": created_pts, "done": done_pts},
    }


def _build_velocity(workspace, tasks, now):
    """Per-project done-velocity tiles with a 12-week sparkline + forecast."""
    from apps.projects.models import Project

    weeks = 12
    start = now - timedelta(weeks=weeks)
    cur = (now - timedelta(days=now.weekday())).date()
    week_keys = [cur - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    idx = {wk: i for i, wk in enumerate(week_keys)}

    per_project = {}
    for r in (
        tasks.filter(status=Task.STATUS_DONE, completed_at__gte=start)
        .annotate(w=TruncWeek("completed_at"))
        .values("project_id", "w")
        .annotate(n=Count("id"))
    ):
        wk = r["w"].date() if hasattr(r["w"], "date") else r["w"]
        if wk in idx:
            spark = per_project.setdefault(r["project_id"], [0] * weeks)
            spark[idx[wk]] = r["n"]

    out = []
    projects = Project.objects.filter(workspace=workspace, archived=False)
    for p in projects:
        spark = per_project.get(p.id, [0] * weeks)
        done4w = sum(spark[-4:])
        prev4w = sum(spark[-8:-4])
        if any(spark):
            recent = statistics.mean(spark[-4:])
            band = round(statistics.pstdev(spark[-8:])) if len(spark) >= 2 else 0
            forecast = f"{round(recent * 4)} ± {band}"
        else:
            forecast = "0"
        vdelta = done4w - prev4w
        out.append(
            {
                "slug": p.slug_prefix,
                "name": p.name,
                "icon": _icon_hex(p.icon_color),
                "done4w": done4w,
                "prev4w": prev4w,
                "delta_str": f"{'+' if vdelta >= 0 else ''}{vdelta} vs prior",
                "delta_cls": "up" if vdelta > 0 else "down" if vdelta < 0 else "flat",
                "spark": spark,
                "forecast": forecast,
            }
        )
    out.sort(key=lambda t: t["done4w"], reverse=True)
    return out


def _build_people(workspace, member_users, tasks, now, today):
    """Bucket tasks per member for the matrix, leaderboard, and panels."""
    rows = list(
        tasks.values(
            "assignee_id",
            "status",
            "priority",
            "created_at",
            "completed_at",
            "due_date",
            "updated_at",
            "archived_at",
        )
    )
    by_user = {}
    for r in rows:
        if r["assignee_id"] is None:
            continue
        by_user.setdefault(r["assignee_id"], []).append(r)

    members = []
    overloaded = []
    idle = []
    cutoff7 = now - timedelta(days=7)
    cutoff30 = now - timedelta(days=30)

    for m in member_users:
        u = m.user
        urs = by_user.get(u.id, [])
        wip = {s: 0 for s in MATRIX_STATUSES}
        done30 = done7 = overdue = urgent = created7 = created30 = 0
        done_ages = []
        active_ages = []
        last_touch = None
        for r in urs:
            st = r["status"]
            archived = r["archived_at"] is not None
            if (
                st in (Task.STATUS_PLANNED, Task.STATUS_TODO, Task.STATUS_IN_PROGRESS, Task.STATUS_IN_REVIEW)
                and not archived
            ):
                wip[st] += 1
            if st == Task.STATUS_DONE and r["completed_at"] and r["completed_at"] >= cutoff30:
                wip[Task.STATUS_DONE] += 1
                done30 += 1
                done_ages.append((r["completed_at"] - r["created_at"]).total_seconds() / 86400)
                if r["completed_at"] >= cutoff7:
                    done7 += 1
            if st not in (Task.STATUS_DONE, Task.STATUS_CANCELLED) and not archived:
                if r["due_date"] and r["due_date"] <= today:
                    overdue += 1
                if r["priority"] in (Task.URGENT, Task.HIGH):
                    urgent += 1
                active_ages.append((now - r["created_at"]).total_seconds() / 86400)
            if r["created_at"] >= cutoff30:
                created30 += 1
                if r["created_at"] >= cutoff7:
                    created7 += 1
            if r["updated_at"] and (last_touch is None or r["updated_at"] > last_touch):
                last_touch = r["updated_at"]

        open_count = (
            wip[Task.STATUS_PLANNED] + wip[Task.STATUS_TODO] + wip[Task.STATUS_IN_PROGRESS] + wip[Task.STATUS_IN_REVIEW]
        )
        still_open_overdue = overdue
        done_rate = round(done30 / (done30 + still_open_overdue), 2) if (done30 + still_open_overdue) else 0.0
        med_age = f"{statistics.median(done_ages):.1f}d" if done_ages else "—"
        max_age = f"{max(active_ages):.1f}d" if active_ages else "—"

        members.append(
            {
                "id": u.id,
                "nm": u.display_name,
                "color": u.avatar_color,
                "wip": {STATUS_LABEL[s]: wip[s] for s in MATRIX_STATUSES},
                "done7": done7,
                "done30": done30,
                "doneRate": done_rate,
                "overdue": overdue,
                "urgent": urgent,
                "created7": created7,
                "created30": created30,
                "medAge": med_age,
                "maxAge": max_age,
                "open": open_count,
            }
        )
        if open_count:
            overloaded.append({"nm": u.display_name, "color": u.avatar_color, "n": open_count})
        if open_count and last_touch and last_touch < cutoff7:
            days_idle = round((now - last_touch).total_seconds() / 86400)
            idle.append({"nm": u.display_name, "color": u.avatar_color, "days": days_idle, "open": open_count})

    overloaded.sort(key=lambda x: x["n"], reverse=True)
    idle.sort(key=lambda x: x["days"], reverse=True)
    return members, overloaded[:4], idle[:3]


def _build_heatmap(workspace, now):
    """Return a 7x24 activity grid (rows Mon→Sun) for the last 7 days."""
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    grid = [[0] * 24 for _ in range(7)]
    for r in (
        ActivityLog.objects.filter(workspace=workspace, created_at__gte=now - timedelta(days=7))
        .annotate(dow=ExtractIsoWeekDay("created_at"), hour=ExtractHour("created_at"))
        .values("dow", "hour")
        .annotate(n=Count("id"))
    ):
        dow = (r["dow"] or 1) - 1  # ISO 1..7 (Mon..Sun) -> 0..6
        hour = r["hour"] or 0
        if 0 <= dow < 7 and 0 <= hour < 24:
            grid[dow][hour] = r["n"]
    return [{"day": days[i], "counts": grid[i]} for i in range(7)]
