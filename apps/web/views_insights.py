"""Insights — one page, two scopes.

``/insights/`` measures a whole workspace; ``/projects/<prefix>/insights/``
measures one project. Same view, same template: the scope arrives in the
URL and drives every query on the page, refusals included.

This replaces the project-only insights page and absorbs what was going
to be a separate Flow page. The two would have overlapped badly — "avg
time in status" and "wait vs work" are the same replay, and "reopen rate"
is one kind of bounce — and two pages of similar words leave a reader
guessing which one to open. What the merge adds that neither had: a
scope switch, so the numbers can be read across the whole workspace, and
the three blocks in ``apps/web/flow.py``.
"""

import json

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.projects.models import Project
from apps.tasks.metrics import compute_cfd, compute_flow_metrics
from apps.tasks.models import Task
from apps.web import flow
from apps.web.nav import resolve_active_workspace
from apps.web.views import _cycle_histogram, _get_user_project_or_404, _is_htmx_partial

#: Tasks that must have closed before a median is a median. Below this
#: the page shows the dash rather than a number built from three samples.
NEED_CLOSED = 5

#: Days of history before the cumulative-flow bands mean anything. A
#: three-day-old project draws a chart that looks like a cliff.
NEED_HISTORY_DAYS = 14

#: Band colours, oldest stage at the bottom — the status palette, so the
#: chart and the board agree about what in-review looks like.
_CFD_COLORS = {
    "planned": "rgb(113 113 122 / 0.55)",
    "ready": "rgb(6 182 212 / 0.55)",
    "to-do": "rgb(59 130 246 / 0.55)",
    "in-progress": "rgb(139 92 246 / 0.55)",
    "in-review": "rgb(245 158 11 / 0.55)",
    "done": "rgb(16 185 129 / 0.55)",
}


def _duration(hours) -> str:
    """Return a duration in the unit a reader can hold in their head.

    Args:
        hours: A number of hours, or ``None``.

    Returns:
        ``18h`` under a day, ``2.4d`` beyond it, an em-dash for nothing.
    """
    if hours is None:
        return "—"
    if hours < 24:
        return f"{hours:.0f}h"
    return f"{hours / 24:.1f}d"


def _project_groups(user, workspace) -> list[tuple]:
    """Return the projects the scope picker offers, starred first.

    A workspace here runs to twenty-odd projects, so the picker is a
    searchable list rather than a row of buttons, and the ones someone
    starred are the ones they open.

    Args:
        user: The viewer, for their stars.
        workspace: The workspace being measured.

    Returns:
        ``[(group_label, [projects]), ...]``, empty groups dropped.
    """
    projects = list(Project.objects.filter(workspace=workspace, archived=False).order_by("name"))
    starred = set(
        Project.objects.filter(workspace=workspace, favourited_by=user).values_list("id", flat=True),
    )
    groups = [
        (_("Starred"), [project for project in projects if project.id in starred]),
        (_("All projects"), [project for project in projects if project.id not in starred]),
    ]
    return [(label, items) for label, items in groups if items]


def _refusals(context: dict) -> dict:
    """Return the line each unanswered block says instead of a chart.

    Written here rather than in the templates because they quote the
    shortfall: "needs ten, has four" is a sentence someone can act on,
    where "not enough data" is one they can only shrug at.

    Nothing blocked is not a refusal at all — it is the good outcome,
    and it gets a tick rather than an hourglass.

    Args:
        context: The page context, with the three flow blocks in it.

    Returns:
        The refusal strings and icons the block templates expect.
    """
    wait_work, blocked, bounces = context["wait_work"], context["blocked"], context["bounces"]
    out = {}
    if not wait_work["ok"]:
        out["wait_work_refusal"] = _("Needs %(need)d tasks closed in the window — this one has %(have)d.") % {
            "need": wait_work["need"],
            "have": wait_work["counted"],
        }
        out["wait_work_hint"] = _("Splitting a life into work and waiting takes a sample, not an anecdote.")
    if not blocked["ok"]:
        out["blocked_icon"] = "circle-check"
        out["blocked_refusal"] = (
            _("Nothing is blocked right now.") if blocked["any_links"] else _("No blocking links here yet.")
        )
        out["blocked_hint"] = (
            _("Every blocking link in scope has an end that is already finished.")
            if blocked["any_links"]
            else _("Link one task as blocking another and the wait it causes shows up here.")
        )
    if not context["cfd_ok"]:
        # Two different shortfalls, and the fix differs: wait, or attach
        # some work. Saying "not enough data" for both would send half
        # the readers to do the wrong thing.
        if context["task_count"] < context["need_closed"]:
            out["cfd_refusal"] = _("Needs work to band — this scope has %(count)d tasks.") % {
                "count": context["task_count"],
            }
            out["cfd_hint"] = _("Attach or create some work and the bands appear on their own.")
        else:
            out["cfd_refusal"] = _("Needs %(need)d days of history — this scope has %(have)d.") % {
                "need": context["need_history_days"],
                "have": context["history_days"],
            }
            out["cfd_hint"] = _("A chart over three days of history is a cliff, not a trend.")
    if not bounces["ok"]:
        out["bounces_refusal"] = _("Needs %(need)d status changes in the window — this one has %(have)d.") % {
            "need": bounces["need"],
            "have": bounces["moves"],
        }
        out["bounces_hint"] = _("A rate over a handful of moves is an anecdote with a percent sign.")
    return out


@login_required
def insights(request, slug_prefix=None):
    """Render Insights for one project, or for the active workspace.

    Args:
        request: The request; ``?weeks=`` picks the window.
        slug_prefix: A project's prefix, or ``None`` for the workspace.

    Returns:
        The full page, or the inner block on an HTMX swap.
    """
    if slug_prefix:
        project = _get_user_project_or_404(request.user, slug_prefix)
        workspace = project.workspace
    else:
        project = None
        workspace = resolve_active_workspace(request)
        if workspace is None:
            return redirect("web:dashboard")

    weeks = flow.resolve_weeks(request.GET.get("weeks"))
    scope = {"project": project} if project else {"workspace": workspace}
    metrics = compute_flow_metrics(**scope, weeks=weeks)
    closed = metrics["completed_count"]
    enough = closed >= NEED_CLOSED

    tasks = Task.objects.work().filter(**({"project": project} if project else {"project__workspace": workspace}))
    oldest = tasks.order_by("created_at").values_list("created_at", flat=True).first()
    history_days = (timezone.now() - oldest).days if oldest else 0
    task_count = tasks.count()
    # Two gates, because they fail for different reasons: a young
    # container has no history to band, and an empty one has nothing to
    # band. Saying which is which is the difference between "wait a
    # fortnight" and "attach some work".
    cfd_ok = history_days >= NEED_HISTORY_DAYS and task_count >= NEED_CLOSED

    open_tasks = tasks.filter(archived_at__isnull=True).exclude(
        status__in=[
            Task.STATUS_DONE,
            Task.STATUS_CANCELLED,
        ],
    )
    per_status = dict(open_tasks.order_by().values_list("status").annotate(count=Count("id")))

    context = {
        "project": project,
        "workspace": workspace,
        "whole_workspace": project is None,
        "scope_groups": _project_groups(request.user, workspace),
        "weeks": weeks,
        "week_ranges": flow.RANGE_WEEKS,
        "metrics": metrics,
        "task_count": task_count,
        "need_closed": NEED_CLOSED,
        "closed_enough": enough,
        "cycle_median": _duration(metrics["cycle_median"]) if enough else "—",
        "cycle_p85": _duration(metrics["cycle_p85"]) if enough else "—",
        "lead_median": _duration(metrics["lead_median"]) if enough else "—",
        "throughput_avg": round(closed / weeks, 1) if enough else "—",
        "cycle_sample": len(metrics["cycle_times"]),
        "cfd_ok": cfd_ok,
        "history_days": history_days,
        "need_history_days": NEED_HISTORY_DAYS,
        "wip_rows": [
            (status, Task.STATUS_LABELS[status], per_status.get(status, 0))
            for status in Task.KANBAN_STATUS_VALUES
            if status != Task.STATUS_DONE
        ],
        "wip_total": sum(per_status.values()),
        **flow.build_flow_context(workspace, project, weeks=weeks),
    }
    context.update(_refusals(context))
    if cfd_ok:
        cfd = compute_cfd(**scope, weeks=weeks)
        context["cfd_labels_json"] = json.dumps(cfd["labels"])
        context["cfd_datasets_json"] = json.dumps(
            [
                {
                    "label": str(Task.STATUS_LABELS[status]),
                    "data": cfd["series"][status],
                    "color": _CFD_COLORS.get(status, "rgb(113 113 122 / 0.5)"),
                }
                for status in cfd["statuses"]
            ],
        )
    if enough:
        context["throughput_labels_json"] = json.dumps([point["label"] for point in metrics["throughput"]])
        context["throughput_data_json"] = json.dumps([point["count"] for point in metrics["throughput"]])
        context["cycle_hist_json"] = json.dumps(_cycle_histogram(metrics["cycle_times"]))

    if request.GET.get("partial") or _is_htmx_partial(request):
        return render(request, "web/_insights_inner.html", context)
    return render(request, "web/insights.html", context)
