"""Server-rendered page views.

Per docs/decisions/0014-frontend-architecture.md, page views return
rendered Django templates; HTMX handles inline updates from the same
endpoints (or from `/api/v1/...` for JSON-only consumers).
"""

import datetime
from functools import cached_property
import json
import re
from urllib.parse import parse_qs, quote, urlencode, urlparse

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Exists, F, Max, OuterRef, Prefetch, Q, Subquery
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView, TemplateView

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.attachments.models import Attachment
from apps.attachments.services import categorize, create_comment_attachment, create_inline_image, create_task_attachment
from apps.attachments.serving import serve_attachment_response
from apps.comments.models import Comment
from apps.cycles.models import Cycle
from apps.cycles.services import (
    apply_cycle_policy,
    compute_cycle_burndown,
    compute_velocity,
    current_cycle,
    cycle_summaries,
    cycle_summary,
    ensure_cycles,
)
from apps.labels.models import Label, LabelGroup
from apps.labels.palette import LABEL_COLORS, is_curated_label_color
from apps.labels.services import add_labels_to_tasks, grouped_labels, trim_exclusive_conflicts
from apps.notifications.models import Notification
from apps.notifications.services import (
    notify_announcement,
    notify_comment_created,
    notify_project_update_created,
    notify_task_created,
)
from apps.projects.models import Project, ProjectUpdate
from apps.reactions.services import TARGET_TYPES, attach_reactions, summarize_reactions, toggle_reaction
from apps.tasks.events import broadcast_link_change, broadcast_task_events, emit_task_diff_events, snapshot_task
from apps.tasks.metrics import compute_bottlenecks, compute_cfd, compute_flow_metrics
from apps.tasks.models import Task
from apps.web.dashboard import DEFAULT_RANGE, build_dashboard_context
from apps.web.exports import serialize_project_overview, serialize_tasks
from apps.web.filters import (
    SORTABLE_COLUMNS,
    apply_task_filters,
    apply_task_ordering,
    filter_sidebar_context,
    resolve_show_archived,
    resolve_show_backlog,
)
from apps.web.grouping import LIST_AXES, group_tasks
from apps.web.nav import resolve_active_workspace, set_active_workspace
from apps.workspaces.models import Workspace, WorkspaceMember

User = get_user_model()

_OPEN_STATUSES = [
    Task.STATUS_PLANNED,
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]

# My Work surfaces active work only — the not-started backlog
# (planned / ready) is groomed on the Backlog tab, not here, so it
# doesn't drown the "what should I do now" list.
_MY_WORK_ACTIVE_STATUSES = [
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]


_VIEW_MODES = {"overview", "kanban", "table", "list", "timeline", "backlog"}


def _is_htmx_partial(request):
    """True when the response should be the *inner* partial.

    HTMX sends ``HX-Request: true`` on every AJAX swap. We narrow that
    down: when ``HX-Boosted: true`` is also set, the request comes from
    an ``hx-boost``-driven anchor (sidebar navigation), and we need the
    **full** template so HTMX can ``hx-select`` the ``#app-content``
    fragment from a shell-aware response. Only un-boosted HTMX requests
    (filter form submit, kanban sort, panel refresh on ``acta:*``
    events) want the inner-only partial.

    History restores also carry ``HX-Request: true`` but set
    ``HX-History-Restore-Request: true``: HTMX is rebuilding the whole
    history element (``<body>``) from a cache miss, so it needs the
    **full** page. Serving the partial there drops the body to just the
    swapped fragment — the page renders as the bare panel with no shell
    (the "timeline goes fullscreen, sidebar disappears on Back" bug).
    """
    if request.headers.get("HX-Request") != "true":
        return False
    if request.headers.get("HX-Boosted") == "true":
        return False
    if request.headers.get("HX-History-Restore-Request") == "true":
        return False
    return True


def _timeline_context(table_tasks, today):
    """Build the timeline (Gantt) context from an already-filtered list.

    Shared by ``AllTasksView`` and ``ProjectDetailView`` so both derive
    the row order and chart window identically. Tasks sort by
    ``start_date`` (nulls last), then ``due_date`` (nulls last). The
    chart window spans the earliest..latest dated task padded by a week
    each side, at least 90 days wide; it falls back to ``today`` when no
    task carries a date.

    Args:
        table_tasks: The filtered task list (reused, not re-queried).
        today: ``date`` used as the chart anchor / fallback window.

    Returns:
        A context dict with ``timeline_tasks`` + ``chart_start_iso`` /
        ``chart_end_iso`` / ``today_iso``.
    """
    timeline_tasks = sorted(
        table_tasks,
        key=lambda t: (
            t.start_date is None,
            t.start_date or datetime.date.max,
            t.end_date is None,
            t.end_date or datetime.date.max,
            t.due_date is None,
            t.due_date or datetime.date.max,
        ),
    )
    all_dates = [d for t in timeline_tasks for d in (t.start_date, t.end_date, t.due_date) if d]
    raw_min = min(all_dates) if all_dates else today
    raw_max = max(all_dates) if all_dates else today
    chart_start = raw_min - datetime.timedelta(days=7)
    chart_end = max(raw_max + datetime.timedelta(days=14), chart_start + datetime.timedelta(days=90))
    return {
        "timeline_tasks": timeline_tasks,
        "chart_start_iso": chart_start.isoformat(),
        "chart_end_iso": chart_end.isoformat(),
        "today_iso": today.isoformat(),
    }


def _resolve_list_axis(request, *, default, options):
    """Resolve the List view group axis: querystring → cookie → default.

    ``options`` is the set of axis keys valid for the current page
    (e.g. My Work disallows ``assignee``, project pages disallow
    ``project``). The cookie name is ``acta_list_axis_<page>`` so
    each page remembers its own choice.
    """
    raw = request.GET.get("axis")
    if raw in options:
        return raw
    cookie_pref = request.COOKIES.get("acta_list_axis")
    return cookie_pref if cookie_pref in options else default


_LIST_AXIS_LABELS = {
    "deadline": "Deadline",
    "status": "Status",
    "priority": "Priority",
    "assignee": "Assignee",
    "project": "Project",
    "cycle": "Cycle",
}


def _cycle_banner(request):
    """Resolve the active-cycle header context, or ``None``.

    Renders only when the view is filtered to **exactly one** concrete
    cycle — ``?cycle=active`` or a single ``?cycle=<id>``. Backlog,
    multi-cycle, and no-cycle views get no banner. The returned dict is
    the cycle plus its :func:`apps.cycles.services.cycle_summary`
    counters, ready to flatten into the template context.

    Args:
        request: The active ``HttpRequest``.

    Returns:
        A context dict (``cycle`` + summary keys), or ``None``.
    """
    values = request.GET.getlist("cycle")
    if len(values) != 1 or values[0] == "backlog":
        return None
    workspace = resolve_active_workspace(request)
    if workspace is None or not workspace.cycle_config()["enabled"]:
        return None
    value = values[0]
    if value == "active":
        cycle = current_cycle(workspace)
    else:
        try:
            cycle = workspace.cycles.filter(pk=int(value)).first()
        except (TypeError, ValueError):
            cycle = None
    if cycle is None:
        return None
    return {"cycle": cycle, **cycle_summary(cycle)}


_BACKLOG_STALE_DAYS = 90


def _backlog_context(tasks, *, today):
    """Build the Backlog grooming context: planned + ready tasks.

    The backlog is the pre-cycle zone — ``ready`` (groomed, pullable) on
    top, ``planned`` (raw) below. Reuses the page's already-filtered task
    list (no extra query) and narrows it to those two statuses, ordered
    priority-first then oldest-first. Each task is decorated with
    ``backlog_age_days`` and ``is_stale`` (a planned task untouched for
    90+ days). Staleness keys off ``status_since`` when the queryset
    carries it (project detail), else ``created_at`` (All Tasks).

    Args:
        tasks: The page's filtered task list (any statuses; filtered here).
        today: ``date`` anchor for age / staleness.

    Returns:
        A context dict with ``backlog_sections`` (Ready, then Planned) +
        ``backlog_total`` and the label dicts ``_task_row.html`` needs.
    """
    cutoff = today - datetime.timedelta(days=_BACKLOG_STALE_DAYS)
    backlog = sorted(
        (t for t in tasks if t.status in (Task.STATUS_PLANNED, Task.STATUS_READY)),
        key=lambda t: (-(t.priority or 0), t.created_at),
    )
    for task in backlog:
        since = getattr(task, "status_since", None)
        anchor = since.date() if since else task.created_at.date()
        task.backlog_age_days = (today - anchor).days
        task.is_stale = task.status == Task.STATUS_PLANNED and anchor <= cutoff
    sections = group_tasks(backlog, "status")
    # Ready first (pullable), Planned below (raw backlog) — Vox's order.
    sections.sort(key=lambda s: 0 if s["key"] == Task.STATUS_READY else 1)
    return {
        "backlog_sections": sections,
        "backlog_total": len(backlog),
        # ``_task_row.html`` needs these; the lazy ``?panel=backlog`` path
        # returns before the full context sets them.
        "status_labels": Task.STATUS_LABELS,
        "priority_labels": dict(Task.PRIORITY_CHOICES),
        "today": today,
    }


def _with_cycle_axis(base_keys, workspace):
    """Append the ``cycle`` group-by axis when the workspace runs cadence.

    Keeps the cycle axis out of the List-view picker entirely for
    workspaces with cycles disabled, so it only appears where it has
    meaning. ``base_keys`` is returned unchanged otherwise.

    Args:
        base_keys: The page's default axis-key tuple.
        workspace: The active / project :class:`Workspace`, or ``None``.

    Returns:
        A tuple of axis keys, with ``"cycle"`` appended iff cadence is on.
    """
    if workspace is not None and workspace.cycle_config()["enabled"]:
        return (*base_keys, "cycle")
    return base_keys


def _list_axis_options(option_keys, active_key):
    """Render-ready axis tabs for the List view picker.

    Returns a list of ``{"key", "label", "active"}`` dicts in the
    requested order so the template can render them as a tab group.
    """
    return [{"key": key, "label": _LIST_AXIS_LABELS[key], "active": key == active_key} for key in option_keys]


def _resolve_view_mode(request, *, default, allow_overview=False, allow_backlog=False):
    """Resolve view_mode in the canonical order.

    Order: ``?view=`` querystring → ``acta_view_mode`` cookie → page
    default. Anything that doesn't validate falls back to ``default``.
    Used by AllTasksView (default ``table``) and ProjectDetailView
    (default ``kanban``) so both share the same persistence flow.

    Args:
        request: The active ``HttpRequest``.
        default: Fallback mode for the page when neither querystring
            nor cookie carries a valid value.
        allow_overview: When True ``"overview"`` is a valid value
            (project detail). All Tasks rejects it since there's no
            single project to show an overview of.
        allow_backlog: When True ``"backlog"`` is a valid value
            (project detail's grooming tab). All Tasks rejects it.

    Returns:
        One of ``"overview"`` / ``"kanban"`` / ``"table"`` / ``"list"`` /
        ``"timeline"`` / ``"backlog"``.
    """
    allowed = {"kanban", "table", "list", "timeline"}
    if allow_overview:
        allowed.add("overview")
    if allow_backlog:
        allowed.add("backlog")
    view_mode = request.GET.get("view")
    if view_mode in allowed:
        return view_mode
    cookie_pref = request.COOKIES.get("acta_view_mode")
    return cookie_pref if cookie_pref in allowed else default


def _params_with_archive_cookie(request):
    """Return a mutable copy of ``request.GET`` with the persisted
    ``show_archived`` cookie merged in.

    Querystring still wins — the user can flip the toggle in either
    direction for one request and the cookie tracks it. Views call
    this before handing params to ``apply_task_filters`` /
    ``filter_sidebar_context`` so the cookie default doesn't have to
    be re-implemented in either.
    """
    params = request.GET.copy()
    if "show_archived" not in params:
        params["show_archived"] = resolve_show_archived(request)
    return params


def _persist_archive_cookie(response, params):
    """Stamp ``acta_show_archived`` on ``response`` to remember the
    current toggle state for the next request.

    Long max-age (1 year) so the toggle outlives sessions. Same-site
    Lax because filter submits are first-party GETs.
    """
    value = "1" if "1" in params.getlist("show_archived") else "0"
    response.set_cookie(
        "acta_show_archived",
        value,
        max_age=60 * 60 * 24 * 365,
        samesite="Lax",
    )
    return response


def _user_task_qs(user):
    """Return the base queryset of tasks the request user can access.

    All web endpoints scoped to a single task funnel through this so the
    membership check and select/prefetch are consistent.

    Only fields needed by the *common* surfaces (table rows, kanban
    cards, list view, inline edits, activity log) are eager-loaded:
    ``project__workspace``, ``assignee``, labels. ``reporter`` and
    ``parent`` are deliberately NOT joined here — they only appear on
    the task-detail page (the rail's "reporter" line and the
    "subtask of …" breadcrumb), which adds its own ``select_related``
    after this base queryset. Skipping them shaves a join on the
    high-traffic table render path.

    Args:
        user: The acting :class:`User`.

    Returns:
        A queryset filtered to the user's workspaces, eager-loading
        project/workspace, assignee, and labels (via prefetch).
    """
    return (
        Task.objects.filter(project__workspace__memberships__user=user).select_related(
            "project__workspace",
            "assignee",
            "cycle",
        )
        # ``Prefetch("labels", queryset=...select_related("group"))`` rather
        # than the bare ``"labels"`` string: the task-detail rail's chip
        # trigger groups labels by ``label.group`` (the ``labels_grouped``
        # filter), so without the join each chip row would re-query the
        # group FK. One JOIN in the labels prefetch query costs nothing on
        # the high-traffic list / kanban surfaces and saves the N+1 for
        # task detail / modal.
        .prefetch_related(
            Prefetch("labels", queryset=Label.objects.select_related("group")),
            "blocks",
            "blocked_by",
        )
    )


def _get_user_task_or_404(user, slug_prefix, number):
    """Look up a task by slug+number, 404 when foreign or missing.

    Args:
        user: Acting :class:`User`.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        The :class:`Task` instance.
    """
    return get_object_or_404(
        _user_task_qs(user),
        project__slug_prefix=slug_prefix,
        number=number,
    )


def _my_work_tasks(user, params, workspace):
    """Resolve the My Work task queryset for ``user``.

    Scoped to ``workspace`` (the user's active workspace); ``None`` means
    the user belongs to no workspace, so the list is empty. Querystring
    filters (``params``) narrow the base queryset — except the assignee
    filter, which is implicit (``me``). Done tasks reach the queryset via
    the page-specific ``Q(status=DONE, updated_at>=cutoff)`` clause so the
    "Recently done" bucket stays populated without showing ancient done
    rows. If the user picks specific statuses in the sidebar, those
    override the open/recently-done split (``apply_task_filters`` honours
    the selection). Grouping into sections is delegated to
    :func:`apps.web.grouping.group_tasks`.
    """
    if workspace is None:
        return []
    done_cutoff = timezone.now() - datetime.timedelta(days=7)
    base = (
        Task.objects.filter(assignee=user, project__workspace=workspace)
        .filter(
            Q(status__in=_MY_WORK_ACTIVE_STATUSES) | Q(status=Task.STATUS_DONE, updated_at__gte=done_cutoff),
        )
        .select_related("project__workspace", "assignee", "reporter", "parent__project")
        .prefetch_related("labels", "blocks", "blocked_by")
    )
    base = apply_task_filters(base, params, request_user=user)
    return list(
        base.order_by(
            F("due_date").asc(nulls_last=True),
            "-priority",
            "-updated_at",
        ),
    )


class AllTasksView(LoginRequiredMixin, ListView):
    """Workspace-wide task index at ``/tasks/``.

    Lists every task across every workspace the user belongs to,
    with querystring-driven filters (status, priority, project,
    workspace, label, assignee, search). The full filtered set
    renders into a single scrollable block — the team is small
    enough that hundreds of rows render fine, and pagination
    actively gets in the way of the filter-and-scan workflow.
    HTMX requests get the inner partial only — page chrome stays
    cached.
    """

    context_object_name = "tasks"

    def get_template_names(self):
        """Full page on cold load, inner fragment for HTMX filter swaps.

        ``HX-Target=task-table-root`` short-circuits to just the
        ``_table.html`` partial — used by the column-sort handler so
        re-sorting doesn't pay the cost of rebuilding kanban + the
        five list-axis grouping passes.
        """
        if self.request.headers.get("HX-Target") == "task-table-root":
            return ["web/projects/_table.html"]
        if self.request.GET.get("panel") == "table":
            return ["web/projects/_table.html"]
        if self.request.GET.get("panel") == "kanban":
            return ["web/projects/_kanban.html"]
        if self.request.GET.get("panel") == "list":
            return ["web/projects/_list_panel.html"]
        if self.request.GET.get("panel") == "timeline":
            return ["web/projects/_timeline.html"]
        if self.request.GET.get("panel") == "backlog":
            return ["web/projects/_backlog_panel.html"]
        if _is_htmx_partial(self.request):
            return ["web/_all_tasks_inner.html"]
        return ["web/all_tasks.html"]

    def get_queryset(self):
        """Filter the user's accessible tasks by querystring params.

        Returned in table order (``?order=`` querystring) — kanban
        ordering is computed in :meth:`get_context_data` from the same
        filtered set since both bodies render simultaneously.
        """
        qs = _user_task_qs(self.request.user)
        active = resolve_active_workspace(self.request)
        qs = qs.filter(project__workspace=active) if active else qs.none()
        params = _params_with_archive_cookie(self.request)
        # The not-started backlog (planned / ready) is ALWAYS rendered into the
        # DOM; the "Show backlog" toggle hides/shows it client-side (instant),
        # like "Show archived" — see acta.js ``rowMatches`` + kanban column
        # hiding. So no server-side backlog filtering here.
        qs = apply_task_filters(qs, params, request_user=self.request.user)
        return apply_task_ordering(qs, params)

    def _backlog_tasks(self):
        """Planned + ready for the Backlog tab — independent of the
        ``show_backlog`` toggle (which only hides them from the other views)."""
        active = resolve_active_workspace(self.request)
        if active is None:
            return []
        qs = _user_task_qs(self.request.user).filter(project__workspace=active)
        params = _params_with_archive_cookie(self.request)
        return list(apply_task_filters(qs, params, request_user=self.request.user))

    def render_to_response(self, context, **response_kwargs):
        """Persist ``view_mode`` + ``show_archived`` + ``list_axis`` cookies."""
        response = super().render_to_response(context, **response_kwargs)
        # Preference cookies (view / list axis / show_archived / show_backlog)
        # are persisted ONLY on real navigations — never on a lazy ``?panel=``
        # fetch. A panel fetch reads them but must not write them back:
        #   * the view would bounce — a lazy tab switch fires the panel fetch
        #     while the URL still carries the previous ``?view=`` (pushState
        #     runs after), so writing it resets the cookie and ``syncFromCookie``
        #     yanks the user back;
        #   * show_backlog / show_archived are set on the client by the
        #     structural-filter toggles, so a slow background panel response
        #     carrying a stale value would race and overwrite the latest choice
        #     (toggling off "didn't stick" because a late ON-era panel response
        #     stamped the cookie back to 1).
        if not self.request.GET.get("panel"):
            response.set_cookie(
                "acta_view_mode",
                context.get("view_mode", "kanban"),
                max_age=60 * 60 * 24 * 365,
                samesite="Lax",
            )
            if context.get("list_axis"):
                response.set_cookie(
                    "acta_list_axis",
                    context["list_axis"],
                    max_age=60 * 60 * 24 * 365,
                    samesite="Lax",
                )
            _persist_archive_cookie(response, _params_with_archive_cookie(self.request))
            response.set_cookie(
                "acta_show_backlog",
                resolve_show_backlog(self.request),
                max_age=60 * 60 * 24 * 365,
                samesite="Lax",
            )
        return response

    def _kanban_columns_ctx(self, table_tasks):
        """Kanban column context for the kanban body (inline or ``?panel=kanban``).

        Args:
            table_tasks: The already-filtered task list.

        Returns:
            A context dict with ``wip_mode`` + ``columns``.
        """
        kanban_tasks = sorted(
            table_tasks,
            key=lambda t: (
                Task.STATUS_VALUES.index(t.status) if t.status in Task.STATUS_VALUES else 99,
                -(t.priority or 0),
                -t.updated_at.timestamp(),
            ),
        )
        wip_mode, wip_limits, wip_over = _wip_context(resolve_active_workspace(self.request))
        # All columns (incl. planned / ready) are always built; the kanban
        # hides the planned / ready columns client-side when the backlog
        # toggle is off (acta.js), so the toggle is instant.
        return {
            "wip_mode": wip_mode,
            "columns": _build_kanban_columns(
                kanban_tasks,
                wip_mode=wip_mode,
                wip_limits=wip_limits,
                over_by_status=wip_over,
            ),
        }

    def _list_axes_ctx(self, table_tasks):
        """List-view grouping context (inline list body or ``?panel=list``).

        Args:
            table_tasks: The already-filtered task list.

        Returns:
            A context dict with ``list_axis`` + ``list_axis_options`` +
            ``list_sections_by_axis`` (one ``group_tasks`` pass per axis).
        """
        list_axis_keys = _with_cycle_axis(
            ("deadline", "status", "priority", "assignee", "project"),
            resolve_active_workspace(self.request),
        )
        list_axis = _resolve_list_axis(self.request, default="project", options=list_axis_keys)
        return {
            "list_axis": list_axis,
            "list_axis_options": _list_axis_options(list_axis_keys, list_axis),
            "list_sections_by_axis": {
                key: group_tasks(table_tasks, key, request_user=self.request.user) for key in list_axis_keys
            },
        }

    def get_context_data(self, **kwargs):
        """Attach filter sidebar context + kanban columns when needed.

        Assignee lives in the top strip, not in the sidebar.
        """
        ctx = super().get_context_data(**kwargs)
        view_mode = _resolve_view_mode(self.request, default="table", allow_backlog=True)
        ctx["view_mode"] = view_mode
        ctx["view_panel_target"] = "#task-list-wrapper"
        ctx["show_project"] = True
        ctx["show_labels"] = True
        # All Tasks renders only the *active* view body inline and lazy-loads
        # the rest via ``?panel=`` (see _view_panel.html). Keeps the
        # workspace-wide page — and every structural-filter round-trip — light
        # instead of rendering table + kanban + list together.
        ctx["lazy_view_panels"] = True
        # Always populate the per-task display dicts — ``_task_row.html``
        # uses them via ``status_labels|get_item:task.status`` etc., and
        # the partial may render on either the full page path or the
        # lazy ``?panel=list`` path.
        ctx["status_labels"] = Task.STATUS_LABELS
        ctx["priority_labels"] = dict(Task.PRIORITY_CHOICES)
        ctx["today"] = timezone.localdate()
        # Default sort order, exposed to the client so a "clear sort"
        # click can re-apply it without a server round-trip. Mirrors
        # the ``default_ordering`` argument passed to
        # ``apply_task_ordering`` (or the Task.Meta.ordering fallback
        # when none is set). Format: comma-separated keys with ``-``
        # prefix meaning descending — same shape as ``?order=`` itself.
        ctx["default_order"] = "-updated"
        # Both bodies render in the DOM so the Alpine ``viewMode`` store
        # can toggle visibility with no round-trip. ``tasks`` (from
        # ``get_queryset``, ``?order=``-aware) feeds the table; columns
        # group a kanban-ordered copy by status.
        table_tasks = list(ctx["tasks"])
        ctx["table_tasks"] = table_tasks
        ctx["tasks"] = table_tasks

        # Timeline context — shared derivation with ProjectDetailView.
        ctx.update(_timeline_context(table_tasks, ctx["today"]))

        # ``?panel=timeline`` is the lazy-load fetch for just the Gantt
        # body — return now with only the timeline context, skipping the
        # kanban sort + five list-axis groupings + filter sidebar build.
        if self.request.GET.get("panel") == "timeline":
            return ctx

        # ``?panel=backlog`` — lazy fetch of just the grooming body.
        if self.request.GET.get("panel") == "backlog":
            ctx.update(_backlog_context(self._backlog_tasks(), today=ctx["today"]))
            return ctx

        # ``?panel=table`` — lazy fetch of just the table body. ``table_tasks``
        # + ``show_labels`` are already in ``ctx`` above; nothing else needed.
        if self.request.GET.get("panel") == "table":
            return ctx

        # ``?panel=kanban`` — lazy fetch of just the kanban body.
        if self.request.GET.get("panel") == "kanban":
            ctx.update(self._kanban_columns_ctx(table_tasks))
            return ctx

        # ``?panel=list`` — lazy fetch of just the list body.
        if self.request.GET.get("panel") == "list":
            ctx.update(self._list_axes_ctx(table_tasks))
            return ctx

        # Table-only HTMX swap (column sort header) renders just ``_table.html``
        # — table_tasks is already set, so nothing more to build.
        if self.request.headers.get("HX-Target") == "task-table-root":
            return ctx

        # Full inner render. With lazy panels only the *active* view body is
        # rendered inline (the rest are empty ``?panel=`` slots), so build
        # context for the active view alone — not table + kanban + list at once.
        if view_mode == "kanban":
            ctx.update(self._kanban_columns_ctx(table_tasks))
        elif view_mode == "list":
            ctx.update(self._list_axes_ctx(table_tasks))
        elif view_mode == "backlog":
            ctx.update(_backlog_context(self._backlog_tasks(), today=ctx["today"]))
        ctx["cycle_banner"] = _cycle_banner(self.request)
        sidebar_params = _params_with_archive_cookie(self.request)
        sidebar_params["show_backlog"] = resolve_show_backlog(self.request)
        ctx.update(
            filter_sidebar_context(
                self.request,
                hide_assignee=True,
                show_backlog_toggle=True,
                extra_preserved={"view": view_mode},
                effective_params=sidebar_params,
            )
        )
        return ctx


class MyWorkView(LoginRequiredMixin, TemplateView):
    """The user's personal task inbox at ``/my-work/``.

    Lists every task assigned to the user across workspaces, grouped
    by a deadline-aware bucket. Filterable via the shared filter
    sidebar (assignee filter hidden — always implicitly the user).
    """

    def get_template_names(self):
        """Full page on cold load, inner fragment for HTMX filter swaps."""
        if _is_htmx_partial(self.request):
            return ["web/_my_work_inner.html"]
        return ["web/my_work.html"]

    def render_to_response(self, context, **response_kwargs):
        """Persist the ``show_archived`` + ``list_axis`` toggles."""
        response = super().render_to_response(context, **response_kwargs)
        _persist_archive_cookie(response, _params_with_archive_cookie(self.request))
        if context.get("list_axis"):
            response.set_cookie(
                "acta_list_axis",
                context["list_axis"],
                max_age=60 * 60 * 24 * 365,
                samesite="Lax",
            )
        return response

    def get_context_data(self, **kwargs):
        """Build the list panel + filter sidebar context.

        My Work has the same axis picker as All Tasks / project detail
        but excludes the ``assignee`` axis (already implicitly the
        current user). Default axis is ``deadline``.
        """
        ctx = super().get_context_data(**kwargs)
        params = _params_with_archive_cookie(self.request)
        active = resolve_active_workspace(self.request)
        tasks = _my_work_tasks(self.request.user, params, active)
        ctx["has_any_tasks"] = bool(tasks)
        list_axis_keys = _with_cycle_axis(("deadline", "status", "priority", "project"), active)
        list_axis = _resolve_list_axis(self.request, default="deadline", options=list_axis_keys)
        ctx["list_axis"] = list_axis
        ctx["list_axis_options"] = _list_axis_options(list_axis_keys, list_axis)
        # Keep the recently_done bucket visible on the deadline axis
        # even when empty — preserves the inbox layout My Work shipped
        # with from day one.
        ctx["list_sections_by_axis"] = {
            "deadline": group_tasks(tasks, "deadline", request_user=self.request.user, keep_empty={"recently_done"}),
            "status": group_tasks(tasks, "status", request_user=self.request.user),
            "priority": group_tasks(tasks, "priority", request_user=self.request.user),
            "project": group_tasks(tasks, "project", request_user=self.request.user),
        }
        # Personal WIP: flag the statuses where the current user holds more
        # than their per-person workspace limit, so the status-axis section
        # headers can warn (e.g. "!! 4/2 over WIP" next to In progress).
        wip_self_over = {}
        if active:
            mode, limits = active.wip_config()
            if mode == Workspace.WIP_PERSONAL and limits:
                rows = (
                    Task.objects.filter(
                        project__workspace=active,
                        assignee=self.request.user,
                        archived_at__isnull=True,
                        status__in=list(limits.keys()),
                    )
                    .values("status")
                    .annotate(n=Count("id"))
                )
                for row in rows:
                    cap = limits.get(row["status"])
                    if cap and row["n"] > cap:
                        wip_self_over[row["status"]] = {"count": row["n"], "limit": cap}
        ctx["wip_self_over"] = wip_self_over
        # The project strip only offers projects the user actually has
        # tasks in — no point filtering My Work by a project with none.
        my_work_projects = list(
            Project.objects.filter(
                workspace=active,
                tasks__assignee=self.request.user,
            )
            .select_related("workspace")
            .order_by("workspace__name", "name")
            .distinct()
            if active
            else []
        )
        ctx.update(
            filter_sidebar_context(
                self.request,
                available_projects=my_work_projects,
                hide_assignee=True,
                hide_project=True,
                htmx_target="#my-work-content",
                effective_params=params,
            )
        )
        return ctx


# ---------------------------------------------------------------------
# Inbox (notifications)
# ---------------------------------------------------------------------

_INBOX_FILTERS = {
    "all",
    "unread",
    "mentions",
    "assigned",
    "due",
    "comments",
    "announcements",
}

_INBOX_FILTER_KINDS = {
    "mentions": Notification.Kind.MENTION,
    "assigned": Notification.Kind.ASSIGNED,
    "due": Notification.Kind.DUE,
    "comments": Notification.Kind.COMMENT,
    "announcements": Notification.Kind.ANNOUNCEMENT,
}

_INBOX_PAGE_SIZE = 100


def _inbox_base_qs(user):
    """Active (non-archived) Notifications-tab notifications, render-ready.

    Scoped to the user's active workspace (``user.active_workspace_id``):
    the inbox only shows notifications for the workspace the user is
    currently in. Callers that depend on a freshly-resolved active
    workspace (the inbox views) call ``resolve_active_workspace`` first so
    the in-memory ``user.active_workspace_id`` is valid here.

    Excludes ``PROJECT_UPDATE``: "X posted an update" is already surfaced
    by the dedicated Updates tab (read straight from ``ProjectUpdate``),
    so listing it here too would duplicate it. Filtering at the base qs
    drops it from the list *and* the filter-chip counts in one place; the
    sidebar badge is kept in sync by the matching exclude in
    :func:`inbox_unread_count` / ``notifications.services._unread_count``.
    The rows are still created (a future "unread updates" indicator can
    use them) — they're just invisible in the Notifications tab.

    Args:
        user: The recipient :class:`User`.

    Returns:
        A queryset with the FK chain the row/preview templates read
        eager-loaded (``task__project``, ``actor``, ``comment``).
    """
    return (
        Notification.objects.filter(
            recipient=user,
            archived_at__isnull=True,
            workspace_id=user.active_workspace_id,
        )
        .exclude(kind=Notification.Kind.PROJECT_UPDATE)
        .select_related(
            "task__project",
            "actor",
            "comment",
        )
        .order_by("-created_at")
    )


def _inbox_filtered_qs(user, filter_key):
    """Apply one inbox filter chip to the base queryset.

    Args:
        user: The recipient :class:`User`.
        filter_key: One of :data:`_INBOX_FILTERS`.

    Returns:
        The filtered notification queryset.
    """
    qs = _inbox_base_qs(user)
    if filter_key == "unread":
        return qs.filter(is_read=False)
    kind = _INBOX_FILTER_KINDS.get(filter_key)
    if kind is not None:
        return qs.filter(kind=kind)
    return qs


def _inbox_counts(user):
    """Return chip counts for the inbox filter row in a single query.

    Args:
        user: The recipient :class:`User`.

    Returns:
        A dict with ``all`` / ``unread`` / ``mentions`` / ``assigned`` /
        ``due`` / ``comments`` / ``announcements`` integer counts over
        active notifications.
    """
    return _inbox_base_qs(user).aggregate(
        all=Count("id"),
        unread=Count("id", filter=Q(is_read=False)),
        mentions=Count("id", filter=Q(kind=Notification.Kind.MENTION)),
        assigned=Count("id", filter=Q(kind=Notification.Kind.ASSIGNED)),
        due=Count("id", filter=Q(kind=Notification.Kind.DUE)),
        comments=Count("id", filter=Q(kind=Notification.Kind.COMMENT)),
        announcements=Count("id", filter=Q(kind=Notification.Kind.ANNOUNCEMENT)),
    )


def inbox_unread_count(user):
    """Return the active unread notification count for the sidebar badge.

    Scoped to the user's active workspace and mirrors
    :func:`_inbox_base_qs`'s ``PROJECT_UPDATE`` exclude, so the badge
    counts exactly what the Notifications tab will show.

    Args:
        user: The recipient :class:`User`.

    Returns:
        The number of non-archived, unread notifications in the active
        workspace (excluding project updates).
    """
    return (
        Notification.objects.filter(
            recipient=user,
            archived_at__isnull=True,
            is_read=False,
            workspace_id=user.active_workspace_id,
        )
        .exclude(kind=Notification.Kind.PROJECT_UPDATE)
        .count()
    )


def _inbox_badge_oob(user):
    """Render the sidebar unread badge as an out-of-band swap fragment.

    Args:
        user: The recipient :class:`User`.

    Returns:
        Rendered HTML for ``#inbox-badge`` with ``hx-swap-oob`` set.
    """
    return render_to_string(
        "web/_inbox_badge.html",
        {
            "inbox_unread": inbox_unread_count(user),
            "oob": True,
        },
    )


def _notification_row_oob(notification):
    """Render a single notification row as an out-of-band swap fragment.

    Args:
        notification: The :class:`Notification` to re-render.

    Returns:
        Rendered ``_notification_row.html`` with ``hx-swap-oob`` set.
    """
    return render_to_string(
        "web/_notification_row.html",
        {
            "n": notification,
            "oob": True,
        },
    )


def _get_user_notification_or_404(user, pk):
    """Fetch a notification owned by the user, or raise 404.

    Args:
        user: The recipient :class:`User`.
        pk: Notification primary key.

    Returns:
        The :class:`Notification` instance.
    """
    return get_object_or_404(
        Notification.objects.select_related("task__project", "actor", "comment", "project_update__project"),
        pk=pk,
        recipient=user,
    )


class InboxView(LoginRequiredMixin, TemplateView):
    """The user's notification inbox at ``/inbox/``.

    Renders the Notifications tab of the split-pane inbox: filter chips,
    a scrollable notification list, and a preview pane. Filter chip
    clicks swap only the list fragment over HTMX; a cold load returns
    the full page.
    """

    def get_template_names(self):
        """Full page on cold load, list fragment for HTMX filter swaps."""
        if _is_htmx_partial(self.request):
            if self.request.GET.get("tab") == "updates":
                return ["web/_inbox_updates_list.html"]
            return ["web/_inbox_list.html"]
        return ["web/inbox.html"]

    def get_context_data(self, **kwargs):
        """Build the active tab (Notifications / Updates) + its split panes."""
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        # Resolve the active workspace first so the in-memory
        # ``user.active_workspace_id`` the inbox helpers scope by is valid.
        active = resolve_active_workspace(self.request)
        tab = self.request.GET.get("tab", "notifications")
        if tab not in {"notifications", "updates"}:
            tab = "notifications"

        # Project strip (shared with My Work via ``_project_strip.html``):
        # ``project`` / ``xproject`` carry project IDs to include / exclude.
        # Applies to both tabs — notifications via their task's project,
        # updates via the update's project.
        params = self.request.GET
        selected_projects = {int(p) for p in params.getlist("project") if p.isdigit()}
        excluded_projects = {int(p) for p in params.getlist("xproject") if p.isdigit()}
        # The strip only offers projects that actually have something on the
        # active tab — notifications for this user, or project updates —
        # so you can't filter to a project with nothing to show.
        available_qs = Project.objects.filter(workspace=active) if active else Project.objects.none()
        if tab == "updates":
            available_qs = available_qs.filter(updates__isnull=False)
        else:
            available_qs = available_qs.filter(
                tasks__notifications__recipient=user,
                tasks__notifications__archived_at__isnull=True,
            )
        available_projects = list(
            available_qs.select_related("workspace").order_by("workspace__name", "name").distinct()
        )

        counts = _inbox_counts(user)
        updates_qs = (
            ProjectUpdate.objects.filter(project__workspace=active).select_related("project", "author")
            if active
            else ProjectUpdate.objects.none()
        )
        ctx.update(
            active_tab=tab,
            inbox_counts=counts,
            inbox_unread=counts["unread"],
            updates_count=updates_qs.count(),
            available_projects=available_projects,
            selected_projects=selected_projects,
            excluded_projects=excluded_projects,
            can_announce=bool(
                active and (_is_workspace_admin(user.id, active.id) or active.allow_member_announcements)
            ),
        )

        if tab == "updates":
            health_keys = dict(ProjectUpdate.HEALTH_CHOICES)
            selected_health = (self.request.GET.get("health") or "").strip()
            if selected_health not in health_keys:
                selected_health = ""
            filtered = updates_qs
            if selected_health:
                filtered = filtered.filter(health=selected_health)
            if selected_projects:
                filtered = filtered.filter(project_id__in=selected_projects)
            if excluded_projects:
                filtered = filtered.exclude(project_id__in=excluded_projects)
            updates = list(filtered.order_by("-created_at")[:_INBOX_PAGE_SIZE])
            sel_pk = self.request.GET.get("selected")
            selected_update = None
            if sel_pk:
                selected_update = next((u for u in updates if str(u.pk) == sel_pk), None)
            if selected_update is None and updates:
                selected_update = updates[0]
            if selected_update is not None:
                _attach_update_reactions([selected_update], user.id)
                _attach_update_thread_reactions(selected_update, user.id)
            ctx.update(
                updates=updates,
                selected_update=selected_update,
                health_labels=health_keys,
                health_choices=ProjectUpdate.HEALTH_CHOICES,
                update_health=selected_health,
            )
        else:
            filter_key = self.request.GET.get("filter", "all")
            if filter_key not in _INBOX_FILTERS:
                filter_key = "all"
            qs = _inbox_filtered_qs(user, filter_key)
            # A notification belongs to a project via its task OR (for
            # project-update notifications) via the update's project.
            if selected_projects:
                qs = qs.filter(
                    Q(task__project_id__in=selected_projects) | Q(project_update__project_id__in=selected_projects)
                )
            if excluded_projects:
                qs = qs.exclude(task__project_id__in=excluded_projects).exclude(
                    project_update__project_id__in=excluded_projects
                )
            notifications = list(qs[:_INBOX_PAGE_SIZE])
            selected = None
            sel_pk = self.request.GET.get("selected")
            if sel_pk:
                selected = next((n for n in notifications if str(n.pk) == sel_pk), None)
            ctx.update(
                notifications=notifications,
                inbox_filter=filter_key,
                selected_notification=selected,
            )
        return ctx


@require_POST
@login_required
def open_notification(request, pk):
    """Mark a notification read and return its preview pane.

    Returns the preview fragment for the main target, plus out-of-band
    swaps for the (now-read) row and the sidebar unread badge.

    Args:
        request: The current request.
        pk: Notification primary key.

    Returns:
        Rendered ``_inbox_preview.html`` + OOB row + OOB badge.
    """
    notification = _get_user_notification_or_404(request.user, pk)
    notification.mark_read()
    html = render_to_string("web/_inbox_preview.html", {"selected_notification": notification})
    html += _notification_row_oob(notification)
    html += _inbox_badge_oob(request.user)
    return HttpResponse(html)


@require_POST
@login_required
def set_notification_read(request, pk):
    """Toggle a notification's read state; return OOB row + badge.

    The ``read`` POST field decides direction (``"1"`` → read,
    otherwise unread).

    Args:
        request: The current request carrying a ``read`` field.
        pk: Notification primary key.

    Returns:
        OOB row fragment + OOB badge fragment.
    """
    notification = _get_user_notification_or_404(request.user, pk)
    if request.POST.get("read") == "1":
        notification.mark_read()
    else:
        notification.mark_unread()
    html = _notification_row_oob(notification)
    html += _inbox_badge_oob(request.user)
    return HttpResponse(html)


@require_POST
@login_required
def archive_notification(request, pk):
    """Archive a notification out of the inbox.

    The triggering row targets itself with an ``outerHTML`` swap, so the
    empty body removes it; the sidebar badge updates out of band.

    Args:
        request: The current request.
        pk: Notification primary key.

    Returns:
        An empty body (row removed) + OOB badge fragment.
    """
    notification = _get_user_notification_or_404(request.user, pk)
    notification.archived_at = timezone.now()
    notification.save(
        update_fields=[
            "archived_at",
        ],
    )
    return HttpResponse(_inbox_badge_oob(request.user))


@require_POST
@login_required
def bulk_notifications(request):
    """Apply a bulk action to selected notifications, re-render the list.

    The ``action`` field is one of ``read`` / ``unread`` / ``archive``;
    ``ids`` is a repeated form field of notification primary keys. Scoped
    to the requesting user — foreign ids are silently ignored.

    Args:
        request: The current request carrying ``action`` + ``ids``.

    Returns:
        Re-rendered ``_inbox_list.html`` + OOB badge fragment.
    """
    action = request.POST.get("action")
    ids = request.POST.getlist("ids")
    qs = Notification.objects.filter(recipient=request.user, pk__in=ids)
    if action == "read":
        qs.update(is_read=True, read_at=timezone.now())
    elif action == "unread":
        qs.update(is_read=False, read_at=None)
    elif action == "archive":
        qs.update(archived_at=timezone.now())
    else:
        return HttpResponseBadRequest("invalid action")
    return _render_inbox_list(request)


@require_POST
@login_required
def read_all_notifications(request):
    """Mark every active notification read, re-render the list.

    Args:
        request: The current request.

    Returns:
        Re-rendered ``_inbox_list.html`` + OOB badge fragment.
    """
    active = resolve_active_workspace(request)
    Notification.objects.filter(
        recipient=request.user,
        archived_at__isnull=True,
        is_read=False,
        workspace=active,
    ).update(is_read=True, read_at=timezone.now())
    return _render_inbox_list(request)


def _render_inbox_list(request):
    """Render the inbox list fragment for the current filter + OOB badge.

    Args:
        request: The current request (reads ``?filter=``).

    Returns:
        Rendered ``_inbox_list.html`` + OOB badge fragment.
    """
    user = request.user
    resolve_active_workspace(request)  # ensure active_workspace_id is valid for scoping
    filter_key = request.GET.get("filter", "all")
    if filter_key not in _INBOX_FILTERS:
        filter_key = "all"
    notifications = list(_inbox_filtered_qs(user, filter_key)[:_INBOX_PAGE_SIZE])
    counts = _inbox_counts(user)
    html = render_to_string(
        "web/_inbox_list.html",
        {
            "notifications": notifications,
            "inbox_filter": filter_key,
            "inbox_counts": counts,
            "inbox_unread": counts["unread"],
        },
        request=request,
    )
    html += _inbox_badge_oob(user)
    return HttpResponse(html)


@login_required
def inbox_update_preview(request, pk):
    """Render the preview pane for one project update (inbox Updates tab).

    Args:
        request: The current request.
        pk: ProjectUpdate primary key.

    Returns:
        Rendered ``_inbox_update_preview.html`` for the selected update,
        scoped to the user's workspaces (404 otherwise).
    """
    update = get_object_or_404(
        ProjectUpdate.objects.select_related("project", "author").filter(
            project__workspace__memberships__user=request.user,
        ),
        pk=pk,
    )
    _attach_update_reactions([update], request.user.id)
    _attach_update_thread_reactions(update, request.user.id)
    return HttpResponse(
        render_to_string(
            "web/_inbox_update_preview.html",
            {
                "selected_update": update,
                "health_labels": dict(ProjectUpdate.HEALTH_CHOICES),
            },
            request=request,
        )
    )


# ---------------------------------------------------------------------
# My Activity (personal comments + activity feed)
# ---------------------------------------------------------------------

_MY_ACTIVITY_TABS = {
    "comments",
    "activity",
}
_MY_ACTIVITY_PAGE_SIZE = 50

# Activity-tab filter chips → the event types each one covers.
_ACTIVITY_TYPE_GROUPS = {
    "status": ["task.status_changed"],
    "priority": ["task.priority_changed"],
    "assignee": ["task.assigned"],
    "due": ["task.due_changed"],
    "labels": ["task.labels_changed"],
    "comments": ["comment.created"],
    "links": [
        "task.link_added",
        "task.link_removed",
    ],
    "edits": ["task.updated"],
}
_ACTIVITY_TYPE_LABELS = [
    ("status", _("Status")),
    ("priority", _("Priority")),
    ("assignee", _("Assignee")),
    ("due", _("Due")),
    ("labels", _("Labels")),
    ("comments", _("Comments")),
    ("links", _("Links")),
    ("edits", _("Edits")),
]


class MyActivityView(LoginRequiredMixin, TemplateView):
    """The user's own activity at ``/my-activity/``.

    Inbox-style tabs over a single-column feed:

    * **Comments** — every comment the user authored, newest first.
    * **Activity** — every event the user is the actor of (from the
      activity log), each linking back to its task.

    A cold load returns the full page; tab clicks swap only the inner
    feed over HTMX.
    """

    def get_template_names(self):
        """Items fragment on load-more, inner feed on tab swap, full page cold."""
        if self.request.GET.get("items"):
            return ["web/_my_activity_items.html"]
        if _is_htmx_partial(self.request):
            return ["web/_my_activity_inner.html"]
        return ["web/my_activity.html"]

    def get_context_data(self, **kwargs):
        """Build one page of the active tab's feed (offset-paginated)."""
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        tab = self.request.GET.get("tab", "comments")
        if tab not in _MY_ACTIVITY_TABS:
            tab = "comments"
        try:
            offset = max(0, int(self.request.GET.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        page = _MY_ACTIVITY_PAGE_SIZE
        end = offset + page

        active = resolve_active_workspace(self.request)
        if active is None:
            comments_qs = Comment.objects.none()
            events_qs = ActivityLog.objects.none()
        else:
            comments_qs = Comment.objects.filter(
                author=user,
                task__project__workspace=active,
            )
            events_qs = ActivityLog.objects.filter(
                actor=user,
                workspace=active,
            )
        ctx["activity_tab"] = tab
        ctx["tab"] = tab
        ctx["next_offset"] = offset + page
        # Counts drive the tab chips — only needed when the tabs render
        # (cold load / tab swap), not on a load-more items fetch.
        if not self.request.GET.get("items"):
            ctx["my_comments_count"] = comments_qs.count()
            ctx["my_activity_count"] = events_qs.count()

        if tab == "comments":
            comments = list(comments_qs.select_related("task__project").order_by("-created_at")[offset:end])
            ctx["my_comments"] = comments
            total = comments_qs.count()
            ctx["has_more"] = len(comments) == page and total > end
            ctx["remaining_count"] = max(0, total - end)
        else:
            # Filters: multi-select event-type chips + a text search over
            # the comment preview and the linked task's title / slug.
            selected = [t for t in self.request.GET.getlist("types") if t in _ACTIVITY_TYPE_GROUPS]
            activity_q = (self.request.GET.get("q") or "").strip()
            filtered = events_qs
            if selected:
                event_types = []
                for key in selected:
                    event_types += _ACTIVITY_TYPE_GROUPS[key]
                filtered = filtered.filter(event_type__in=event_types)
            if activity_q:
                tmatch = Q(title__icontains=activity_q)
                upper = activity_q.upper()
                if "-" in upper:
                    prefix, _sep, num = upper.rpartition("-")
                    if num.isdigit():
                        tmatch |= Q(project__slug_prefix=prefix, number=int(num))
                elif activity_q.isdigit():
                    tmatch |= Q(number=int(activity_q))
                matched_task_ids = list(
                    Task.objects.filter(project__workspace__memberships__user=user)
                    .filter(tmatch)
                    .values_list("id", flat=True)[:500]
                )
                match = Q(payload__body_preview__icontains=activity_q)
                if matched_task_ids:
                    match |= Q(target_type=ActivityLog.TARGET_TASK, target_id__in=matched_task_ids)
                    match |= Q(target_type=ActivityLog.TARGET_COMMENT, payload__task_id__in=matched_task_ids)
                filtered = filtered.filter(match)

            events = list(filtered.select_related("project").order_by("-created_at")[offset:end])

            # Resolve the task each event points at, in one batch (no N+1).
            # Task events carry it as ``target_id``; comment events
            # (``comment.created`` etc.) carry it in ``payload.task_id``.
            def _event_task_id(event):
                if event.target_type == ActivityLog.TARGET_TASK:
                    return event.target_id
                if event.target_type == ActivityLog.TARGET_COMMENT:
                    return (event.payload or {}).get("task_id")
                return None

            task_ids = [tid for tid in (_event_task_id(e) for e in events) if tid]
            tasks = {t.id: t for t in Task.objects.filter(id__in=task_ids).select_related("project")}
            for e in events:
                e.linked_task = tasks.get(_event_task_id(e))
            _enrich_activity_events(events)
            ctx["my_events"] = events
            ctx["my_event_groups"] = _group_events_by_task(events)
            ctx["status_labels"] = Task.STATUS_LABELS
            ctx["priority_labels"] = dict(Task.PRIORITY_CHOICES)
            total = filtered.count()
            ctx["has_more"] = len(events) == page and total > end
            ctx["remaining_count"] = max(0, total - end)
            ctx["activity_types"] = set(selected)
            ctx["activity_q"] = activity_q
            ctx["activity_type_chips"] = [
                {"key": key, "label": label, "active": key in selected} for key, label in _ACTIVITY_TYPE_LABELS
            ]
            ctx["activity_filter_qs"] = urlencode(
                [("types", t) for t in selected] + ([("q", activity_q)] if activity_q else [])
            )
        return ctx


class DashboardView(LoginRequiredMixin, TemplateView):
    """Workspace dashboard at ``/``.

    Routes the authenticated user to either:

    * The full dashboard (charts + project cards + activity feed)
      placeholder if they belong to at least one workspace.
    * A "no workspaces yet, ask an admin" page if they have none —
      matches the onboarding flow in
      docs/decisions/0010-permissions.md.
    """

    @cached_property
    def active_workspace(self):
        """Memoise the active-workspace resolution across the view's lifecycle.

        ``get_context_data`` and ``get_template_names`` both need to know
        whether the user has any accessible workspace; resolving twice
        per request burned an unnecessary DB round-trip and also dropped
        the redundant ``WorkspaceMember.exists()`` probe — a ``None``
        return already means "no membership / no active workspace".
        """
        return resolve_active_workspace(self.request)

    def get_template_names(self):
        """Return the dashboard, its inner partial, or the no-workspaces page.

        A range-chip click sends ``?partial=1`` (an explicit HTMX swap of
        ``#dash-inner``); we return just the body fragment so switching the
        window repaints the dashboard, not the whole page. A boosted nav to
        ``/`` has no ``partial`` flag and still gets the full page.
        """
        if self.active_workspace is None:
            return ["web/no_workspaces.html"]
        if self.request.GET.get("partial") and self.request.headers.get("HX-Request"):
            return ["web/_dashboard_inner.html"]
        return ["web/dashboard.html"]

    def get_context_data(self, **kwargs):
        """Attach the workspace dashboard aggregates for the active workspace.

        Skipped when the user has no workspace (the no-workspaces template
        is rendered instead and ignores this context).
        """
        ctx = super().get_context_data(**kwargs)
        workspace = self.active_workspace
        if workspace is not None:
            ctx.update(
                build_dashboard_context(
                    workspace,
                    self.request.user,
                    range_key=self.request.GET.get("range", DEFAULT_RANGE),
                ),
            )
        return ctx


class ProjectListView(LoginRequiredMixin, ListView):
    """Index of every project in workspaces the user belongs to.

    Annotates each row with the open-task count and the health of the
    most recent :class:`ProjectUpdate`, all in a single query so the
    template stays N+1-free.
    """

    template_name = "web/projects/list.html"
    context_object_name = "projects"

    def get_queryset(self):
        """Return user-accessible projects with annotated stats.

        ``select_related("lead")`` keeps the lead avatar rendering
        N+1-free; ``distinct=True`` on member count prevents the
        member JOIN from inflating the open_task_count. Each
        per-status count is its own conditional Count so the card
        can render the 5-segment progress bar and breakdown chips
        without an N+1.
        """
        active = resolve_active_workspace(self.request)
        if active is None:
            return Project.objects.none()
        latest = ProjectUpdate.objects.filter(project=OuterRef("pk")).order_by("-created_at").values("health")[:1]
        # Archived projects are hidden by default; ``?archived=1`` reveals
        # them (the "Show archived" toggle on the page).
        base = Project.objects.filter(workspace=active)
        if self.request.GET.get("archived") != "1":
            base = base.filter(archived=False)
        return (
            base.select_related("workspace", "lead")
            .prefetch_related("members")
            .annotate(
                open_task_count=Count(
                    "tasks",
                    filter=Q(tasks__status__in=_OPEN_STATUSES),
                    distinct=True,
                ),
                total_task_count=Count(
                    "tasks",
                    filter=~Q(tasks__status=Task.STATUS_CANCELLED),
                    distinct=True,
                ),
                planned_count=Count(
                    "tasks",
                    filter=Q(tasks__status=Task.STATUS_PLANNED),
                    distinct=True,
                ),
                ready_count=Count(
                    "tasks",
                    filter=Q(tasks__status=Task.STATUS_READY),
                    distinct=True,
                ),
                todo_count=Count(
                    "tasks",
                    filter=Q(tasks__status=Task.STATUS_TODO),
                    distinct=True,
                ),
                in_progress_count=Count(
                    "tasks",
                    filter=Q(tasks__status=Task.STATUS_IN_PROGRESS),
                    distinct=True,
                ),
                in_review_count=Count(
                    "tasks",
                    filter=Q(tasks__status=Task.STATUS_IN_REVIEW),
                    distinct=True,
                ),
                done_count=Count(
                    "tasks",
                    filter=Q(tasks__status=Task.STATUS_DONE),
                    distinct=True,
                ),
                member_count=Count("members", distinct=True),
                latest_health=Subquery(latest),
                last_activity_at=Max("tasks__updated_at"),
            )
            .order_by("archived", "workspace__name", "name")
            .distinct()
        )

    def get_context_data(self, **kwargs):
        """Expose the user's favourite project ids so each card can
        render its star toggle in the correct (starred / unstarred)
        state without an N+1 lookup; and the ``stale_cutoff`` past
        which the "updated X ago" footer dot loses its pulse.
        """
        ctx = super().get_context_data(**kwargs)
        ctx["favourite_project_ids"] = set(
            self.request.user.favourite_projects.values_list("id", flat=True),
        )
        ctx["stale_cutoff"] = timezone.now() - datetime.timedelta(days=3)
        ctx["health_labels"] = dict(ProjectUpdate.HEALTH_CHOICES)
        # "Show archived" toggle state + how many archived projects exist
        # (so the toggle only shows when there's something to reveal).
        active = resolve_active_workspace(self.request)
        ctx["show_archived"] = self.request.GET.get("archived") == "1"
        ctx["archived_count"] = Project.objects.filter(workspace=active, archived=True).count() if active else 0
        return ctx


class ProjectDetailView(LoginRequiredMixin, DetailView):
    """Project page with Kanban / Table view switching."""

    context_object_name = "project"

    def _kanban_columns_ctx(self, *, view_base, table_tasks, project, today):
        """Kanban ctx — used by ``?panel=kanban`` and the cold-load kanban view.

        Falls back to ``table_tasks`` (already in the kanban default ordering)
        unless the user clicked a column-sort header on the table — in which
        case ``table_tasks`` carries that custom order and we re-sort a fresh
        ``view_base`` copy back to the kanban grouping order.
        """
        table_order_key = (self.request.GET.get("order") or "").strip().lstrip("-")
        if table_order_key in SORTABLE_COLUMNS:
            kanban_tasks = list(view_base.order_by("status", "-priority", "-updated_at"))
        else:
            kanban_tasks = table_tasks
        wip_mode, wip_limits, wip_over = _wip_context(project.workspace)
        return {
            "tasks": kanban_tasks,
            "wip_mode": wip_mode,
            "columns": _build_kanban_columns(
                kanban_tasks,
                today=today,
                wip_mode=wip_mode,
                wip_limits=wip_limits,
                over_by_status=wip_over,
            ),
        }

    def _list_axes_ctx(self, *, table_tasks, project):
        """List-view grouping ctx — used by ``?panel=list`` and cold-load list view."""
        list_axis_keys = _with_cycle_axis(("deadline", "status", "priority", "assignee"), project.workspace)
        list_axis = _resolve_list_axis(self.request, default="status", options=list_axis_keys)
        return {
            "list_axis": list_axis,
            "list_axis_options": _list_axis_options(list_axis_keys, list_axis),
            "list_sections_by_axis": {
                key: group_tasks(table_tasks, key, request_user=self.request.user) for key in list_axis_keys
            },
        }

    def get_template_names(self):
        """Full page on cold load; only the panel fragment for HTMX swaps.

        ``HX-Target=task-table-root`` short-circuits to the table-only
        partial so a column sort doesn't repaint kanban + the five
        list-view group axes (which the panel partial rebuilds even
        when the user only wants the rows re-sorted).
        """
        if self.request.headers.get("HX-Target") == "task-table-root":
            return ["web/projects/_table.html"]
        if self.request.GET.get("panel") == "kanban":
            return ["web/projects/_kanban.html"]
        if self.request.GET.get("panel") == "table":
            return ["web/projects/_table.html"]
        if self.request.GET.get("panel") == "list":
            return ["web/projects/_list_panel.html"]
        if self.request.GET.get("panel") == "timeline":
            return ["web/projects/_timeline.html"]
        if self.request.GET.get("panel") == "backlog":
            return ["web/projects/_backlog_panel.html"]
        if _is_htmx_partial(self.request):
            return ["web/projects/_view_panel_wrapper.html"]
        return ["web/projects/detail.html"]

    def get_object(self, queryset=None):
        """Resolve the project by slug_prefix and enforce membership.

        Annotates ``is_favourite`` via an ``Exists`` subquery so the
        overview star renders without a separate favourites lookup, plus
        ``my_workspace_role`` so ``viewer_is_workspace_admin`` derives
        from data we already fetched (instead of a separate
        ``WorkspaceMember.filter().first()`` round-trip). Keeps the page
        query count constant. Viewing a project also pulls its workspace
        into focus (active-workspace switch) so the sidebar and the
        scoped views stay consistent with what's on screen.
        """
        slug_prefix = self.kwargs["slug_prefix"]
        favourited = self.request.user.favourite_projects.filter(pk=OuterRef("pk"))
        my_role = WorkspaceMember.objects.filter(
            workspace=OuterRef("workspace_id"),
            user=self.request.user,
        ).values(
            "role"
        )[:1]
        project = get_object_or_404(
            Project.objects.filter(
                slug_prefix=slug_prefix,
                workspace__memberships__user=self.request.user,
            )
            .select_related("workspace", "lead")
            .annotate(
                is_favourite=Exists(favourited),
                my_workspace_role=Subquery(my_role),
            ),
        )
        set_active_workspace(self.request, project.workspace)
        return project

    def render_to_response(self, context, **response_kwargs):
        """Persist ``view_mode`` + ``show_archived`` + ``list_axis`` cookies."""
        response = super().render_to_response(context, **response_kwargs)
        # Preference cookies (view / list axis / show_archived / show_backlog)
        # are persisted ONLY on real navigations — never on a lazy ``?panel=``
        # fetch. A panel fetch reads them but must not write them back:
        #   * the view would bounce — a lazy tab switch fires the panel fetch
        #     while the URL still carries the previous ``?view=`` (pushState
        #     runs after), so writing it resets the cookie and ``syncFromCookie``
        #     yanks the user back;
        #   * show_backlog / show_archived are set on the client by the
        #     structural-filter toggles, so a slow background panel response
        #     carrying a stale value would race and overwrite the latest choice
        #     (toggling off "didn't stick" because a late ON-era panel response
        #     stamped the cookie back to 1).
        if not self.request.GET.get("panel"):
            response.set_cookie(
                "acta_view_mode",
                context.get("view_mode", "kanban"),
                max_age=60 * 60 * 24 * 365,
                samesite="Lax",
            )
            if context.get("list_axis"):
                response.set_cookie(
                    "acta_list_axis",
                    context["list_axis"],
                    max_age=60 * 60 * 24 * 365,
                    samesite="Lax",
                )
            _persist_archive_cookie(response, _params_with_archive_cookie(self.request))
            response.set_cookie(
                "acta_show_backlog",
                resolve_show_backlog(self.request),
                max_age=60 * 60 * 24 * 365,
                samesite="Lax",
            )
        return response

    def get_context_data(self, **kwargs):
        """Attach the filtered task list, columns, filter sidebar, and
        project-overview metadata for all three view tabs.

        Every page load renders all three view bodies (overview /
        kanban / table) into the DOM; switching between tabs is a
        client-side ``x-show`` toggle via the ``viewMode`` Alpine
        store, no extra requests. ``view_mode`` here only determines
        the initial active tab.

        View mode resolution order for the initial active tab:
        1. ``?view=`` querystring — explicit user click on the toggle.
        2. ``acta_view_mode`` cookie — remembered choice from the
           previous project the user looked at.
        3. ``kanban`` — first-time default.

        The cookie is refreshed in :meth:`render_to_response` so every
        toggle sticks for the next project switch.
        """
        from apps.common.markdown import render_markdown

        ctx = super().get_context_data(**kwargs)
        view_mode = _resolve_view_mode(self.request, default="kanban", allow_overview=True, allow_backlog=True)
        ctx["view_mode"] = view_mode
        # Common per-task display dicts — needed by both the full page
        # and the lazy ``?panel=list`` fragment (``_task_row.html`` uses
        # them via ``status_labels|get_item:...``).
        ctx["status_labels"] = Task.STATUS_LABELS
        ctx["priority_labels"] = dict(Task.PRIORITY_CHOICES)
        ctx["today"] = timezone.localdate()

        from apps.projects.icons import PROJECT_ICON_COLORS, PROJECT_ICONS

        project = self.object
        ctx["is_favourite"] = project.is_favourite  # annotated in get_object (no extra query)
        ctx["description_html"] = render_markdown(project.description) if project.description else ""
        ctx["members"] = list(
            project.members.order_by("first_name", "last_name", "username"),
        )
        # Gate the Overview archive/delete menu to workspace owners/admins.
        # Derived from the ``my_workspace_role`` annotation on ``get_object`` —
        # ``_user_is_workspace_admin`` would re-query ``WorkspaceMember`` for
        # the same row, bumping the project-detail query count by one.
        ctx["viewer_is_workspace_admin"] = project.my_workspace_role in (
            WorkspaceMember.OWNER,
            WorkspaceMember.ADMIN,
        )
        ctx["workspace_members"] = _project_workspace_members(project, exclude_user=None)
        ctx["picker_icons"] = PROJECT_ICONS
        ctx["picker_icon_colors"] = PROJECT_ICON_COLORS

        now = timezone.now()
        today = ctx["today"]
        velocity_cutoff = now - datetime.timedelta(days=7)
        # Collapse the seven separate overview SELECTs (status histogram +
        # overdue + velocity + last-activity) into one aggregate. Each
        # ``Count("id", filter=...)`` compiles to a ``FILTER (WHERE …)``
        # clause Postgres evaluates in a single table scan.
        active = Q(archived_at__isnull=True)
        stats = Task.objects.filter(project=project).aggregate(
            planned=Count("id", filter=active & Q(status=Task.STATUS_PLANNED)),
            ready=Count("id", filter=active & Q(status=Task.STATUS_READY)),
            todo=Count("id", filter=active & Q(status=Task.STATUS_TODO)),
            in_progress=Count("id", filter=active & Q(status=Task.STATUS_IN_PROGRESS)),
            in_review=Count("id", filter=active & Q(status=Task.STATUS_IN_REVIEW)),
            done=Count("id", filter=active & Q(status=Task.STATUS_DONE)),
            cancelled=Count("id", filter=active & Q(status=Task.STATUS_CANCELLED)),
            overdue=Count(
                "id",
                filter=active & Q(due_date__lt=today) & ~Q(status__in=[Task.STATUS_DONE, Task.STATUS_CANCELLED]),
            ),
            velocity_7d=Count(
                "id",
                filter=Q(status=Task.STATUS_DONE, updated_at__gte=velocity_cutoff),
            ),
            last_activity=Max("updated_at"),
        )
        ctx["overview_status_counts"] = {
            Task.STATUS_PLANNED: stats["planned"],
            Task.STATUS_READY: stats["ready"],
            Task.STATUS_TODO: stats["todo"],
            Task.STATUS_IN_PROGRESS: stats["in_progress"],
            Task.STATUS_IN_REVIEW: stats["in_review"],
            Task.STATUS_DONE: stats["done"],
        }
        ctx["overview_total"] = sum(ctx["overview_status_counts"].values())
        ctx["overview_done"] = stats["done"]
        ctx["overview_cancelled"] = stats["cancelled"]
        ctx["overview_overdue"] = stats["overdue"]
        ctx["overview_velocity_7d"] = stats["velocity_7d"]
        ctx["overview_last_activity_at"] = stats["last_activity"]
        # Linear-style: the overview surfaces only the latest update; the
        # full history lives in the inbox Updates tab (filtered by project).
        # Skip the COUNT(*) entirely when there are no updates (the common
        # case), so the empty project keeps its constant query count.
        ctx["overview_latest_updates"] = list(project.updates.select_related("author").order_by("-created_at")[:1])
        ctx["overview_updates_total"] = project.updates.count() if ctx["overview_latest_updates"] else 0
        if ctx["overview_latest_updates"]:
            user_id = self.request.user.id
            _attach_update_reactions(ctx["overview_latest_updates"], user_id)
            _attach_update_thread_reactions(ctx["overview_latest_updates"][0], user_id)
            user_is_admin = _is_workspace_admin(user_id, project.workspace_id)
            for update in ctx["overview_latest_updates"]:
                update.can_modify = user_is_admin or update.author_id == user_id
        ctx["overview_project_age_days"] = (today - project.created_at.date()).days
        ctx["health_labels"] = dict(ProjectUpdate.HEALTH_CHOICES)
        ctx["latest_health"] = ctx["overview_latest_updates"][0].health if ctx["overview_latest_updates"] else None

        # Aging WIP: the timestamp the task last changed status (its
        # ``task.status_changed`` activity row), so the board can show how
        # long a card has sat in its current column. One correlated
        # subquery — not an N+1. Falls back to ``created_at`` in the
        # template when the task never changed status.
        last_status_change = (
            ActivityLog.objects.filter(
                target_type=ActivityLog.TARGET_TASK,
                target_id=OuterRef("pk"),
                event_type="task.status_changed",
            )
            .order_by("-created_at")
            .values("created_at")[:1]
        )
        base = (
            Task.objects.filter(project=project)
            .select_related("assignee", "reporter", "parent", "project__workspace")
            .prefetch_related("labels", "blocks", "blocked_by")
            .annotate(status_since=Subquery(last_status_change))
        )
        params = _params_with_archive_cookie(self.request)
        base = apply_task_filters(base, params, request_user=self.request.user)
        # Backlog (planned/ready) is always rendered into the DOM; the "Show
        # backlog" toggle hides/shows it client-side (instant), like "Show
        # archived" — see acta.js ``rowMatches`` + kanban column hiding. The
        # Backlog tab uses ``base`` too (it filters to planned/ready itself).
        view_base = base
        # Both bodies render in the DOM; table honors ``?order=``,
        # kanban keeps the fixed status grouping. We sort once per
        # body — the difference is small enough not to need separate
        # querysets, but mixing orderings on a single list would
        # confuse one of the two views.
        table_tasks = list(
            apply_task_ordering(
                view_base,
                self.request.GET,
                default_ordering=("status", "-priority", "-updated_at"),
            )
        )
        ctx["table_tasks"] = table_tasks
        # See AllTasksView for the rationale — keeps "clear sort"
        # entirely client-side. Ordering uses the comma-separated
        # querystring shape, not the ORM field tuple, because the
        # client comparators key off the sort-key (``updated``), not
        # the model field (``updated_at``).
        ctx["default_order"] = "status,-priority,-updated"

        # Table-only HTMX swap (column sort header): skip the kanban
        # column build and the five list-axis grouping passes — only
        # ``_table.html`` will render, so those would be wasted work.
        # Cuts sort latency from "rebuild everything" to "ORDER BY +
        # the table partial".
        table_only = self.request.headers.get("HX-Target") == "task-table-root"
        # All non-active view bodies (kanban, table, list, timeline, backlog)
        # render as empty ``data-panel-slot`` divs and are filled by ``acta.js``
        # via ``?panel=<key>`` after first paint (and on demand after a
        # ``acta:task-created`` invalidation). Keeps the cross-view rebuild
        # cost off the cold-load path and guarantees every panel is fresh on
        # the next switch after a create.
        ctx["lazy_view_panels"] = True
        # Per-view lazy panel fragments. Each fetch builds ONLY its own
        # ctx so a table-sort or list-axis switch doesn't pay for the
        # other panels' grouping passes.
        panel = self.request.GET.get("panel")
        if panel == "kanban":
            ctx.update(
                self._kanban_columns_ctx(
                    view_base=view_base,
                    table_tasks=table_tasks,
                    project=project,
                    today=today,
                ),
            )
            return ctx
        if panel == "table":
            ctx["tasks"] = table_tasks
            ctx["show_labels"] = True
            return ctx
        if panel == "list":
            ctx.update(self._list_axes_ctx(table_tasks=table_tasks, project=project))
            return ctx
        if panel == "timeline":
            ctx.update(_timeline_context(table_tasks, today))
            return ctx
        if panel == "backlog":
            ctx.update(_backlog_context(list(base), today=today))
            return ctx
        if table_only:
            ctx["tasks"] = table_tasks
            ctx["show_labels"] = True
        else:
            # Full inner render. With ``lazy_view_panels`` only the active
            # view body renders inline (siblings render as empty
            # ``data-panel-slot`` divs and pull their fragments via
            # ``?panel=`` after first paint), so build the context for the
            # active view alone — not table + kanban + list at once.
            ctx["tasks"] = table_tasks
            if view_mode == "kanban":
                ctx.update(
                    self._kanban_columns_ctx(
                        view_base=view_base,
                        table_tasks=table_tasks,
                        project=project,
                        today=today,
                    ),
                )
            elif view_mode == "list":
                ctx.update(self._list_axes_ctx(table_tasks=table_tasks, project=project))
            elif view_mode == "timeline":
                ctx.update(_timeline_context(table_tasks, today))
            elif view_mode == "backlog":
                ctx.update(_backlog_context(list(base), today=today))

        ctx["cycle_banner"] = _cycle_banner(self.request)

        # Per-project page: scope project + workspace filters away.
        # Show labels in the table view (matches All Tasks layout).
        # ``show_backlog`` / ``show_archived`` resolved from cookies so the
        # sidebar toggles render in their persisted state.
        sidebar_params = params.copy()
        sidebar_params["show_backlog"] = resolve_show_backlog(self.request)
        ctx.update(
            filter_sidebar_context(
                self.request,
                hide_assignee=True,
                hide_project=True,
                hide_status=(view_mode == "kanban"),
                show_backlog_toggle=True,
                htmx_target="#project-view-panel",
                extra_preserved={"view": view_mode},
                effective_params=sidebar_params,
                # Scoped to users who actually have a task in this
                # project (any status) — picking from every workspace
                # member would clutter the strip with people who never
                # appeared on the board.
                available_assignees=list(
                    get_user_model()
                    .objects.filter(assigned_tasks__project=self.object)
                    .exclude(pk=self.request.user.pk)
                    .order_by("first_name", "last_name", "username")
                    .distinct(),
                ),
            )
        )
        ctx["show_labels"] = True

        # Timeline context — shared derivation with AllTasksView.
        ctx.update(_timeline_context(table_tasks, today))

        return ctx


class TaskDetailView(LoginRequiredMixin, DetailView):
    """Single-task page at ``/projects/<slug_prefix>/<number>/``."""

    context_object_name = "task"
    template_name = "web/projects/task_detail.html"

    def get_template_names(self):
        """Pick modal-mode template when ``?modal=1`` is set.

        The HTMX-driven row click in tables / kanban cards fetches the
        task URL with ``?modal=1`` so the response is just the modal
        shell + body partial, ready to drop into ``#modal-root``.
        Direct URL load (no querystring) returns the full page so
        shared links still open as a regular task page.
        """
        if self.request.GET.get("modal") == "1":
            return ["web/projects/task_detail_modal.html"]
        return [self.template_name]

    def get_object(self, queryset=None):
        """Resolve the task by slug_prefix + number, 404 if foreign.

        Adds ``reporter`` and ``parent`` to the base queryset's
        ``select_related`` — both appear on this page (rail's
        "reporter" line and the "subtask of …" breadcrumb in the
        title cell). The base ``_user_task_qs`` omits them so the
        common table / kanban / list views don't pay for joins they
        never use.
        """
        return get_object_or_404(
            _user_task_qs(self.request.user).select_related("reporter", "parent")
            # Links panel + Blocked badge read all three link sets; the
            # ``__project`` hop is needed because each chip renders the
            # linked task's slug (prefix + number).
            .prefetch_related("blocked_by__project", "blocks__project", "related__project"),
            project__slug_prefix=self.kwargs["slug_prefix"],
            number=self.kwargs["number"],
        )

    def get_context_data(self, **kwargs):
        """Attach subtasks, comments, activity timeline, and the merged
        modal timeline that interleaves comments with non-comment
        activity events sorted by ``created_at``.

        The merged timeline is consumed by the modal-mode body template
        (``_task_detail_modal_body.html``). Comment activity events
        (``comment.*``) are filtered out of the merge because the
        comments themselves carry the body — keeping both would render
        each post twice.
        """
        ctx = super().get_context_data(**kwargs)
        task = self.object
        ctx["subtasks"] = list(
            task.subtasks.select_related("assignee").order_by("number"),
        )
        user_id = self.request.user.id
        ctx["comments"] = _task_comments(task, user_id)
        task.reaction_summary = summarize_reactions(
            target_field="task",
            ids=[task.id],
            user_id=user_id,
        ).get(task.id, [])
        ctx["activity"] = _task_activity(task)
        non_comment_activity = [e for e in ctx["activity"] if _timeline_event(e)]
        ctx["timeline"] = _sort_timeline(ctx["comments"], non_comment_activity)
        ctx["status_labels"] = Task.STATUS_LABELS
        ctx["priority_labels"] = dict(Task.PRIORITY_CHOICES)
        ctx["workspace_members"] = _workspace_members(task)
        ctx["workspace_labels"] = _workspace_labels(task)
        ctx["workspace_label_groups"] = _workspace_label_groups(task)
        ctx["workspace_projects"] = _workspace_projects(task)
        ctx["workspace_cycles"] = _workspace_cycles(task.project.workspace)
        # Read from the prefetched ``labels`` queryset on the base task — the
        # ``_user_task_qs`` Prefetch already loaded it. ``values_list`` would
        # issue a fresh query bypassing the prefetch cache.
        ctx["attached_label_ids"] = {label.id for label in task.labels.all()}
        return ctx


@login_required
def task_title_fragment(request, slug_prefix, number):
    """Render the title cell HTML for one task — SSE-triggered refresh.

    Adds ``parent`` to the queryset because ``_title_cell.html`` shows
    the "subtask of <parent>" link and would otherwise lazy-load it on
    render.
    """
    task = get_object_or_404(
        _user_task_qs(request.user).select_related("parent"),
        project__slug_prefix=slug_prefix,
        number=number,
    )
    return HttpResponse(
        render_to_string(
            "web/projects/_title_cell.html",
            {"task": task},
            request=request,
        ),
    )


@login_required
def task_topbar_title_fragment(request, slug_prefix, number):
    """Render the topbar task-title span — SSE-triggered refresh."""
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    return HttpResponse(
        render_to_string(
            "web/projects/_topbar_task_title.html",
            {"task": task},
            request=request,
        ),
    )


@login_required
def task_description_fragment(request, slug_prefix, number):
    """Render the description cell HTML for one task — SSE-triggered refresh."""
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    return HttpResponse(
        render_to_string(
            "web/projects/_description_cell.html",
            {"task": task},
            request=request,
        ),
    )


@login_required
def task_comments_fragment(request, slug_prefix, number):
    """Render the comments list (``<li>`` rows) for one task.

    Returns just the row HTML so the caller can ``hx-swap="innerHTML"``
    the existing ``<ul id="comment-list">`` without nesting another
    list.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    comments = _task_comments(task, request.user.id)
    rows = "".join(render_to_string("web/projects/_comment.html", {"comment": c}, request=request) for c in comments)
    return HttpResponse(rows)


@login_required
def task_meta_fragment(request, slug_prefix, number):
    """Render the right-rail metadata + labels panels for one task.

    Used by the SSE-triggered ``hx-get`` on the task detail page —
    when a peer changes ``status / priority / assignee / due_date /
    labels / size``, the rail refreshes itself without a full page
    reload. See ADR 0015.

    ``reporter`` is added to the queryset because the rail prints the
    reporter's display name; without the join it lazy-loads at render
    time.
    """
    task = get_object_or_404(
        _user_task_qs(request.user).select_related("reporter"),
        project__slug_prefix=slug_prefix,
        number=number,
    )
    return HttpResponse(
        render_to_string(
            "web/projects/_task_meta.html",
            {
                "task": task,
                "status_labels": Task.STATUS_LABELS,
                "priority_labels": dict(Task.PRIORITY_CHOICES),
                "workspace_members": _workspace_members(task),
                "workspace_labels": _workspace_labels(task),
                "workspace_label_groups": _workspace_label_groups(task),
                "workspace_projects": _workspace_projects(task),
                "workspace_cycles": _workspace_cycles(task.project.workspace),
                # Prefetched via ``_user_task_qs``; ``values_list`` would
                # bypass the cache and issue a fresh query. See B3 §3.1.
                "attached_label_ids": {label.id for label in task.labels.all()},
            },
            request=request,
        ),
    )


@login_required
def task_timeline_fragment(request, slug_prefix, number):
    """Render the unified comments + activity timeline for one task.

    SSE-triggered ``hx-get`` lands here when a comment is posted or an
    activity event fires; the page-level timeline div refreshes its
    inner ``<ul>`` so peer-driven updates land without a full reload.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    comments = _task_comments(task, request.user.id)
    activity = _task_activity(task)
    non_comment_activity = [e for e in activity if _timeline_event(e)]
    timeline = _sort_timeline(comments, non_comment_activity)
    return HttpResponse(
        render_to_string(
            "web/projects/_task_detail_timeline.html",
            {
                "timeline": timeline,
                "status_labels": Task.STATUS_LABELS,
                "priority_labels": dict(Task.PRIORITY_CHOICES),
            },
            request=request,
        ),
    )


@login_required
def task_meta_compact_fragment(request, slug_prefix, number):
    """Render the horizontal metadata row for one task (modal layout).

    Counterpart of ``task_meta_fragment`` for the modal view, which uses
    a single-line ``_task_meta_compact.html`` partial instead of the
    vertical rail card. SSE peer-updates hit this endpoint when the
    task is open in modal mode.
    """
    task = get_object_or_404(
        _user_task_qs(request.user).select_related("reporter"),
        project__slug_prefix=slug_prefix,
        number=number,
    )
    return HttpResponse(
        render_to_string(
            "web/projects/_task_meta_compact.html",
            {
                "task": task,
                "status_labels": Task.STATUS_LABELS,
                "priority_labels": dict(Task.PRIORITY_CHOICES),
                "workspace_members": _workspace_members(task),
                "workspace_labels": _workspace_labels(task),
                "workspace_label_groups": _workspace_label_groups(task),
                "workspace_projects": _workspace_projects(task),
                "workspace_cycles": _workspace_cycles(task.project.workspace),
                "attached_label_ids": set(task.labels.values_list("id", flat=True)),
            },
            request=request,
        ),
    )


@login_required
def task_row_fragment(request, task_id):
    """Render a single task row for table or list view.

    Returns just the ``<tr>`` (``?as=table``) or the ``<a>`` row
    (``?as=list``) for one task. The client-side SSE handler in
    ``acta.js`` swaps each appearance of ``data-task-id`` after a
    peer's edit — one row at a time, no flash, no scroll jump.

    ``show_project`` / ``show_labels`` default to ``True`` (matches
    the All Tasks rendering); per-project pages also include both
    columns now, so the fragment is shape-stable across pages.
    """
    task = get_object_or_404(_user_task_qs(request.user), pk=task_id)
    as_type = request.GET.get("as", "table")
    template = "web/_task_row.html" if as_type == "list" else "web/projects/_table_row.html"
    return HttpResponse(
        render_to_string(
            template,
            {
                "task": task,
                "status_labels": Task.STATUS_LABELS,
                "priority_labels": dict(Task.PRIORITY_CHOICES),
                "today": timezone.localdate(),
                "show_project": True,
                "show_labels": True,
            },
            request=request,
        ),
    )


@login_required
def task_activity_fragment(request, slug_prefix, number):
    """Render just the ``_activity_list.html`` partial for one task.

    Used by the SSE-triggered ``hx-get`` on the task detail page —
    when a relevant ``task.*`` or ``comment.*`` event arrives on the
    workspace stream, the activity panel refreshes itself without a
    full page reload. See ADR 0015.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    return HttpResponse(
        render_to_string(
            "web/projects/_activity_list.html",
            {
                "activity": _task_activity(task),
                "status_labels": Task.STATUS_LABELS,
                "priority_labels": dict(Task.PRIORITY_CHOICES),
            },
            request=request,
        ),
    )


def _task_activity(task, limit=25):
    """Return the recent activity events relevant to a single task.

    Includes:
        * Events whose ``target_type='task'`` and ``target_id=task.id``.
        * ``comment.*`` events whose ``payload.task_id`` matches.
          Using the payload (instead of joining through the comments
          table) means an event remains visible on the task even after
          the underlying comment row is deleted.
        * ``attachment.*`` events whose ``payload.task_id`` matches —
          same payload-scoping, so the event stays visible after the
          attachment (and its file) is deleted.

    Excluded from the user-facing feed:
        * ``task.labels_changed`` — too chatty for the timeline.
        * ``task.updated`` whose ``payload.changes`` only touches
          ``title`` and/or ``description`` — title/description history
          will live on a dedicated "full history" page later; the
          inline timeline gets too noisy otherwise. ``task.updated``
          events that also carry other changes (e.g. ``size``) stay
          visible.

    All hidden events are still written to the DB by ``log_event`` so
    dashboards and audit queries can read them directly from
    ``ActivityLog``.

    Attaches ``assigned_from_name`` and ``assigned_to_name`` to every
    ``task.assigned`` event, resolving the user ids in a single
    batched query so the template can show ``X → Y`` without per-row
    lookups. Names use the user's display name (``First Last`` with
    username fallback) so the feed reads naturally — usernames are
    reserved for ``@mention`` autocomplete.

    Args:
        task: The :class:`Task` whose feed to load.
        limit: Maximum number of events to return.

    Returns:
        A list of :class:`ActivityLog` rows, newest first, with the
        assigned-event enrichment described above.
    """
    events = list(
        ActivityLog.objects.filter(
            Q(target_type=ActivityLog.TARGET_TASK, target_id=task.id)
            | Q(target_type=ActivityLog.TARGET_COMMENT, payload__task_id=task.id)
            | Q(target_type=ActivityLog.TARGET_ATTACHMENT, payload__task_id=task.id),
        )
        .exclude(event_type="task.labels_changed")
        .select_related("actor")
        .order_by("-created_at")[:limit],
    )
    # Hide title-only / description-only ``task.updated`` events from
    # the feed. Done in Python (not via JSON queries) because the test
    # is "what keys are present in payload.changes" — clean expressed
    # imperatively, awkward in SQL across DB backends.
    _hide_only_keys = {"title", "description"}

    def _visible(e):
        if e.event_type != "task.updated":
            return True
        keys = set(((e.payload or {}).get("changes") or {}).keys())
        return bool(keys - _hide_only_keys)

    events = [e for e in events if _visible(e)]
    _enrich_activity_events(events)
    return events


def _enrich_activity_events(events):
    """Attach display-name diffs to assigned / labels_changed events.

    Resolves the user ids (assignee from/to) and label ids (added /
    removed) referenced across ``events`` in two batched queries, then
    sets ``assigned_from_name`` / ``assigned_to_name`` and
    ``added_label_names`` / ``removed_label_names`` so
    ``_activity_event.html`` renders the diff without per-row lookups.

    Args:
        events: A list of :class:`ActivityLog` rows (mutated in place).

    Returns:
        The same ``events`` list, enriched.
    """
    user_ids = set()
    label_ids = set()
    for e in events:
        if e.event_type == "task.assigned" and e.payload:
            for key in ("from_user_id", "to_user_id"):
                uid = e.payload.get(key)
                if uid is not None:
                    user_ids.add(uid)
        elif e.event_type == "task.labels_changed" and e.payload:
            for key in ("added_ids", "removed_ids"):
                label_ids.update(e.payload.get(key) or [])
    user_names = {}
    if user_ids:
        user_names = {u.id: u.display_name for u in User.objects.filter(id__in=user_ids)}
    label_map = {}
    if label_ids:
        label_map = {row["id"]: row for row in Label.objects.filter(id__in=label_ids).values("id", "name", "color")}

    def _labels(ids):
        return [label_map.get(lid, {"name": f"#{lid}", "color": "#71717a"}) for lid in (ids or [])]

    for e in events:
        if e.event_type == "task.assigned" and e.payload:
            e.assigned_from_name = user_names.get(e.payload.get("from_user_id"))
            e.assigned_to_name = user_names.get(e.payload.get("to_user_id"))
        elif e.event_type == "task.labels_changed" and e.payload:
            e.added_label_names = [lbl["name"] for lbl in _labels(e.payload.get("added_ids"))]
            e.removed_label_names = [lbl["name"] for lbl in _labels(e.payload.get("removed_ids"))]
            e.added_labels = _labels(e.payload.get("added_ids"))
            e.removed_labels = _labels(e.payload.get("removed_ids"))
    return events


def _group_events_by_task(events):
    """Group consecutive events that share a task into timeline runs.

    Walks the (already time-ordered) list and starts a new group each
    time the linked task changes, so the template can show one task
    header per run with the events stacked under a left rail.

    Args:
        events: Events carrying a ``linked_task`` attribute (or ``None``).

    Returns:
        A list of ``{"task": <Task|None>, "events": [...]}`` dicts in the
        original order.
    """
    groups = []
    for e in events:
        task = getattr(e, "linked_task", None)
        tid = task.id if task else None
        if groups and groups[-1]["task_id"] == tid:
            groups[-1]["events"].append(e)
        else:
            groups.append({"task_id": tid, "task": task, "events": [e]})
    return groups


def _workspace_members(task):
    """Return the workspace's members ordered by username.

    Used by the assignee picker to populate its dropdown. Eager-loads
    ``user`` so the template can render avatar + username without N+1.

    Args:
        task: The :class:`Task` whose workspace's members to fetch.

    Returns:
        A queryset of :class:`WorkspaceMember` rows.
    """
    return (
        WorkspaceMember.objects.filter(workspace=task.project.workspace)
        .select_related("user")
        .order_by("user__username")
    )


def _workspace_projects(task):
    """Return the workspace's projects ordered by name.

    Populates the move-task project picker on the task detail rail —
    every project in the task's workspace is an eligible target (the
    viewer is a member, since they can see this task). Scoped to one
    workspace so a move never crosses the active-workspace boundary.

    Args:
        task: The :class:`Task` whose workspace's projects to fetch.

    Returns:
        A queryset of :class:`Project` rows.
    """
    return Project.objects.filter(workspace=task.project.workspace).order_by("name")


def _workspace_labels(task):
    """Return the workspace's labels in picker order (``position``, then ``name``).

    Used by the labels picker to populate its dropdown. Same ordering the
    grouped picker uses so flat and grouped surfaces agree.

    Args:
        task: The :class:`Task` whose workspace's labels to fetch.

    Returns:
        A queryset of :class:`Label` rows.
    """
    return Label.objects.filter(workspace=task.project.workspace).order_by("position", "name")


def _workspace_label_groups(task):
    """Return the workspace's labels organised by :class:`LabelGroup`.

    Wraps :func:`apps.labels.services.grouped_labels` so view code only
    has to pass a task. Used wherever the labels dropdown / picker
    template wants group sections.
    """
    return grouped_labels(task.project.workspace)


def _workspace_cycles(workspace):
    """Return the assignable cycles for a workspace, active first.

    Materializes the rolling windows (so the current + next cycle exist)
    and returns the active and upcoming (planning) cycles — completed
    cycles are not offered as fresh assignment targets. Returns an empty
    list when the workspace is missing or cadence is disabled, which the
    pickers treat as "no cycle UI".

    Args:
        workspace: The :class:`Workspace` (or ``None``).

    Returns:
        A list of :class:`~apps.cycles.models.Cycle` rows, active first
        then upcoming by start date.
    """
    if workspace is None or not workspace.cycle_config()["enabled"]:
        return []
    ensure_cycles(workspace)
    return list(
        workspace.cycles.exclude(status=Cycle.COMPLETED).order_by(
            "status",
            "start_date",
        ),
    )


def _inline_edit_response(request, task, primary_template, primary_context):
    """Render an inline-edit primary fragment + OOB-swapped activity list.

    HTMX picks up the ``hx-swap-oob`` attribute on the activity ``<ul>``
    and merges it into the current page's ``#activity-list`` without
    requiring a separate request. The primary fragment is swapped into
    the caller-specified target (status cell, priority cell, or the
    end of the comment list).

    Args:
        request: The current Django request.
        task: The :class:`Task` the inline edit was made on.
        primary_template: Template path for the primary fragment.
        primary_context: Context dict for ``primary_template``.

    Returns:
        An :class:`HttpResponse` containing the primary fragment
        followed by the OOB-swapped activity list.
    """
    primary_html = render_to_string(primary_template, primary_context, request=request)
    activity_html = render_to_string(
        "web/projects/_activity_oob.html",
        {
            "task": task,
            "timeline": _build_timeline(task, request.user.id),
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
        },
        request=request,
    )
    return HttpResponse(primary_html + activity_html)


def _is_workspace_admin(user_id, workspace_id):
    """Return True if the user is an owner/admin of the workspace.

    Args:
        user_id: The acting user's id.
        workspace_id: The workspace to check membership role in.

    Returns:
        True iff the user has the OWNER or ADMIN role in that workspace.
    """
    return WorkspaceMember.objects.filter(
        workspace_id=workspace_id,
        user_id=user_id,
        role__in=[
            WorkspaceMember.OWNER,
            WorkspaceMember.ADMIN,
        ],
    ).exists()


def _decorate_comments(comments, task, user_id):
    """Attach ``task`` / ``can_modify`` / ``reaction_summary`` to comments.

    Decorates the given top-level comments AND their prefetched replies in
    place: each gets the already-loaded ``task`` (so the reply / edit URLs
    don't lazy-load it), a ``can_modify`` flag (author or workspace
    admin/owner — drives the edit/delete affordances), and a
    ``reaction_summary`` (all in one reaction query, no N+1).

    Args:
        comments: Iterable of top-level :class:`Comment` rows.
        task: The owning :class:`Task` (with its project loaded).
        user_id: The viewer's id.
    """
    user_is_admin = _is_workspace_admin(user_id, task.project.workspace_id)
    decorated = []
    for comment in comments:
        comment.task = task
        decorated.append(comment)
        for reply in comment.replies.all():
            reply.task = task
            decorated.append(reply)
    for item in decorated:
        item.can_modify = user_is_admin or item.author_id == user_id
    attach_reactions(objs=decorated, target_field="comment", user_id=user_id)


def _task_comments(task, user_id):
    """Top-level task comments, replies prefetched + decorated.

    Only top-level comments (``parent__isnull=True``) are returned — the
    unified timeline shows them as cards, with their one-level replies
    rendered nested inside each card (mirroring the project-update
    threads). Each row + reply is decorated via :func:`_decorate_comments`
    (task, ``can_modify``, ``reaction_summary``).

    Args:
        task: The :class:`Task` whose comments to load.
        user_id: The viewer's id.

    Returns:
        A list of top-level :class:`Comment` rows ordered oldest-first.
    """
    comments = list(
        task.comments.filter(parent__isnull=True)
        .select_related("author")
        .prefetch_related("replies__author", "attachments", "replies__attachments")
        .order_by("created_at")
    )
    _decorate_comments(comments, task, user_id)
    return comments


def _get_user_comment_or_404(user, comment_id):
    """Fetch a comment (task- or project-update-owned) the user may access.

    The membership filter spans both owner types so the unified comment
    edit/delete endpoints work for either; a comment in a workspace the
    user isn't a member of 404s.

    Args:
        user: The acting :class:`User`.
        comment_id: PK of the comment.

    Returns:
        The :class:`apps.comments.models.Comment`, owners eager-loaded.
    """
    return get_object_or_404(
        Comment.objects.select_related(
            "author",
            "task__project__workspace",
            "project_update__project__workspace",
        ).filter(
            Q(task__project__workspace__memberships__user=user)
            | Q(project_update__project__workspace__memberships__user=user)
        ),
        pk=comment_id,
    )


def _comment_owner(comment):
    """Return ``(workspace, project, kind)`` for a comment's owner.

    ``kind`` is ``"task"`` or ``"update"``. Both owner types resolve to a
    project + workspace, which drive permission checks and the editor's
    mention / image-upload endpoints.
    """
    if comment.task_id:
        return comment.task.project.workspace, comment.task.project, "task"
    return comment.project_update.project.workspace, comment.project_update.project, "update"


def _can_modify_any_comment(user, comment):
    """Return True if ``user`` may edit/delete ``comment`` (either owner).

    Allowed for the author or a workspace owner/admin.
    """
    workspace, _project, _kind = _comment_owner(comment)
    return comment.author_id == user.id or _is_workspace_admin(user.id, workspace.id)


def _render_any_comment_card(request, comment):
    """Re-render one comment's card fresh + decorated, picking by owner.

    Task comments render the task card (``_comment`` / ``_comment_reply``)
    decorated via :func:`_decorate_comments`; project-update comments render
    the update card (``_update_comment`` / ``_update_comment_reply``) with
    ``can_modify`` + ``reaction_summary`` attached. Used by the unified
    edit (save) and fragment (cancel) paths.

    Args:
        request: The current request (its user drives ``can_modify``).
        comment: The :class:`Comment` to re-render.

    Returns:
        An :class:`HttpResponse` with the rendered card fragment.
    """
    _workspace, _project, kind = _comment_owner(comment)
    if kind == "task":
        task = comment.task
        fresh = get_object_or_404(
            task.comments.select_related("author").prefetch_related(
                "replies__author",
                "attachments",
                "replies__attachments",
            ),
            pk=comment.id,
        )
        _decorate_comments([fresh], task, request.user.id)
        template = "web/projects/_comment_reply.html" if fresh.parent_id else "web/projects/_comment.html"
        return HttpResponse(render_to_string(template, {"comment": fresh}, request=request))

    fresh = get_object_or_404(
        Comment.objects.select_related("author", "project_update__project__workspace").prefetch_related(
            "replies__author",
        ),
        pk=comment.id,
    )
    user_is_admin = _is_workspace_admin(request.user.id, fresh.project_update.project.workspace_id)
    items = [fresh, *fresh.replies.all()]
    for item in items:
        item.can_modify = user_is_admin or item.author_id == request.user.id
    attach_reactions(objs=items, target_field="comment", user_id=request.user.id)
    template = "web/projects/_update_comment_reply.html" if fresh.parent_id else "web/projects/_update_comment.html"
    return HttpResponse(render_to_string(template, {"comment": fresh}, request=request))


def _timeline_event(event):
    """Return True if an activity event should be its own timeline row.

    ``comment.created`` / ``comment.edited`` are dropped — the comment
    card itself (with its ``(edited)`` marker) already conveys them.
    ``comment.deleted`` is kept: the comment is gone, so the event is its
    only remaining trace in the timeline.

    Args:
        event: An :class:`ActivityLog` row.

    Returns:
        True to render the event as a timeline row.
    """
    return (event.event_type or "") not in (
        "comment.created",
        "comment.edited",
    )


def _timeline_sort_key(kind, item):
    """Return the timestamp a timeline row should sort by.

    Normally an item's own ``created_at``. A ``comment.deleted`` event,
    though, is created at deletion time but should appear where the
    comment originally sat — so it sorts by the ``comment_created_at``
    stashed in its payload (falling back to its own time).

    Args:
        kind: ``"comment"`` or ``"event"``.
        item: The :class:`Comment` or :class:`ActivityLog` row.

    Returns:
        A ``datetime`` to sort the timeline by.
    """
    if kind == "event" and (item.event_type or "") == "comment.deleted":
        raw = (item.payload or {}).get("comment_created_at")
        if raw:
            parsed = parse_datetime(raw)
            if parsed is not None:
                return parsed
    return item.created_at


def _sort_timeline(comments, events):
    """Merge + sort timeline rows, handling the deleted-comment position.

    Args:
        comments: Top-level :class:`Comment` rows (already decorated).
        events: The :class:`ActivityLog` rows to show as their own rows.

    Returns:
        A list of ``(kind, item)`` tuples sorted ascending by
        :func:`_timeline_sort_key`.
    """
    return sorted(
        [("comment", c) for c in comments] + [("event", e) for e in events],
        key=lambda kv: _timeline_sort_key(kv[0], kv[1]),
    )


def _build_timeline(task, user_id):
    """Merge top-level comments + non-comment activity events into the
    unified timeline tuple list consumed by ``_task_detail_timeline.html``.

    Comments (and their replies) are decorated with ``reaction_summary``
    so the OOB timeline refresh keeps their reaction bars intact.
    """
    comments = _task_comments(task, user_id)
    activity = _task_activity(task)
    non_comment = [e for e in activity if _timeline_event(e)]
    return _sort_timeline(comments, non_comment)


# ---------------------------------------------------------------------
# HTMX inline-edit endpoints
# ---------------------------------------------------------------------


def _apply_task_field_change(task, field, value, actor):
    """Apply a single scalar field change and emit diff events.

    Captures the pre-save state, mutates the attribute, saves, and pipes
    the resulting diff through :func:`emit_task_diff_events` so the
    activity log records the change with the proper granular event
    type. Wraps the change in a transaction so the event and the save
    commit together.

    Args:
        task: The :class:`Task` instance to mutate.
        field: The model attribute name to set.
        value: The new value to assign.
        actor: The acting :class:`User`.
    """

    with transaction.atomic():
        old = snapshot_task(task)
        setattr(task, field, value)
        task.save()
        emit_task_diff_events(old_state=old, task=task, actor=actor)


@require_POST
@login_required
def set_task_status(request, slug_prefix, number):
    """Inline status change; returns the new status badge fragment.

    Args:
        request: DRF/Django request carrying a ``status`` form field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_status_cell.html`` with the updated task.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    new_status = request.POST.get("status", "")
    if new_status not in Task.STATUS_VALUES:
        return HttpResponseBadRequest("invalid status")
    with transaction.atomic():
        old = snapshot_task(task)
        task.status = new_status
        # Auto-set start_date on the first transition to in-progress so
        # the timeline view has an anchor without the user needing to set
        # it manually. Not cleared on status revert — the date reflects
        # when work actually started, not the current status.
        if new_status == Task.STATUS_IN_PROGRESS and task.start_date is None:
            task.start_date = timezone.localdate()
        # Cadence policy: planned drops to backlog (no cycle); entering
        # committed work pulls the task into the active cycle. Mutates
        # task.cycle in memory before save so the diff below also emits
        # task.cycle_changed.
        apply_cycle_policy(task)
        task.save()
        emit_task_diff_events(old_state=old, task=task, actor=request.user)
    response = _inline_edit_response(
        request,
        task,
        "web/projects/_status_cell.html",
        {
            "task": task,
            "status_labels": Task.STATUS_LABELS,
        },
    )
    # A status move can auto-stamp the date fields (start_date on in-progress,
    # end_date on done — see ``Task._sync_done_dates``). The acting tab's own
    # SSE refresh is suppressed by the self-event filter, so OOB-swap any
    # changed date cell here; the cells only exist on the rail / modal, so the
    # swap is a no-op on the board.
    extra = ""
    if old["start_date"] != task.start_date:
        extra += render_to_string("web/projects/_start_date_cell.html", {"task": task, "oob": True}, request=request)
    if old["end_date"] != task.end_date:
        extra += render_to_string("web/projects/_end_date_cell.html", {"task": task, "oob": True}, request=request)
    if extra:
        response.content = response.content + extra.encode()
    return response


@require_POST
@login_required
def set_task_priority(request, slug_prefix, number):
    """Inline priority change; returns the priority cell fragment.

    Args:
        request: Django request carrying a ``priority`` form field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_priority_cell.html`` with the updated task.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    raw = request.POST.get("priority", "")
    try:
        priority = int(raw)
    except (TypeError, ValueError):
        return HttpResponseBadRequest("invalid priority")
    if priority not in {Task.NO_PRIORITY, Task.URGENT, Task.HIGH, Task.MEDIUM, Task.LOW}:
        return HttpResponseBadRequest("invalid priority")
    _apply_task_field_change(task, "priority", priority, request.user)
    return _inline_edit_response(
        request,
        task,
        "web/projects/_priority_cell.html",
        {
            "task": task,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
        },
    )


@require_POST
@login_required
def set_task_size(request, slug_prefix, number):
    """Inline size (story-point) change; returns the size cell fragment.

    An empty ``size`` clears the field; a non-empty value must be one of
    ``Task.SIZE_VALUES`` (the Fibonacci set). Routed through the diff path
    so the change logs activity and refreshes peer cards over SSE.

    Returns:
        Rendered ``_size_cell.html`` with the updated task, or 400 on an
        invalid value.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    raw = (request.POST.get("size") or "").strip()
    if raw == "":
        size = None
    else:
        try:
            size = int(raw)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("invalid size")
        if size not in Task.SIZE_VALUES:
            return HttpResponseBadRequest("invalid size")
    _apply_task_field_change(task, "size", size, request.user)
    return _inline_edit_response(
        request,
        task,
        "web/projects/_size_cell.html",
        {"task": task},
    )


@require_POST
@login_required
def set_task_cycle(request, slug_prefix, number):
    """Assign the task to a workspace cycle, or clear it (back to backlog).

    Reads ``cycle_id``; empty clears the cycle. A non-empty value must be
    a cycle in the task's own workspace (cross-workspace assignment is
    rejected). Routed through the diff path so the change logs a
    ``task.cycle_changed`` event and refreshes peer cards over SSE.

    Returns:
        Rendered ``_cycle_cell.html`` for the rail cell (plus the OOB
        activity refresh), or ``400`` on an invalid / foreign cycle.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    raw = (request.POST.get("cycle_id") or "").strip()
    if raw == "":
        cycle = None
    else:
        # planned / ready are the backlog — they can't hold a cycle.
        # Clearing (empty value) is always allowed; assigning is rejected.
        if task.status in (Task.STATUS_PLANNED, Task.STATUS_READY):
            return HttpResponseBadRequest("backlog tasks (planned/ready) stay cycle-free")
        try:
            cycle_pk = int(raw)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("invalid cycle")
        cycle = get_object_or_404(
            Cycle.objects.filter(workspace=task.project.workspace),
            pk=cycle_pk,
        )
    _apply_task_field_change(task, "cycle", cycle, request.user)
    return _inline_edit_response(
        request,
        task,
        "web/projects/_cycle_cell.html",
        {
            "task": task,
            "workspace_cycles": _workspace_cycles(task.project.workspace),
        },
    )


@require_POST
@login_required
def set_task_project(request, slug_prefix, number):
    """Move a task (and its subtasks) to another project in the workspace.

    Reassigns ``task.project`` and allocates a fresh per-project
    ``number`` for the task and each of its subtasks — the
    ``subtask.project == parent.project`` invariant (ADR 0007) forces the
    cascade — so the user-facing slug renumbers (e.g. AUD-167 → HRW-89).
    Each moved task emits a ``task.project_changed`` activity event via
    the standard diff path, so the timeline reads the move and peer
    kanban / table surfaces refresh over SSE.

    Only top-level tasks carry a project picker; a subtask rides along
    with its parent. The target must be a project in the user's active
    workspace, so labels and assignee stay valid (the move never crosses
    a workspace boundary).

    Returns:
        ``204`` with an ``HX-Location`` pointing at the task's new detail
        URL (its slug changed, so the page re-resolves), or ``400`` on an
        invalid / cross-workspace / subtask target.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    if task.parent_id is not None:
        return HttpResponseBadRequest("subtasks move with their parent")
    try:
        target_pk = int((request.POST.get("project_id") or "").strip())
    except (TypeError, ValueError):
        return HttpResponseBadRequest("invalid project")
    target = get_object_or_404(
        _user_accessible_projects(request.user, resolve_active_workspace(request)),
        pk=target_pk,
    )
    if target.id != task.project_id:
        with transaction.atomic():
            subtasks = list(task.subtasks.select_related("project__workspace").all())
            # Reserve numbers for the parent + every subtask in one locked
            # counter step; the parent takes the lowest number so the slug
            # sequence stays readable (parent below its subtasks).
            movers = [task, *subtasks]
            numbers = target.allocate_task_numbers(len(movers))
            for mover, new_number in zip(movers, numbers):
                old = snapshot_task(mover)
                mover.project = target
                mover.number = new_number
                mover.save(update_fields=["project", "number", "updated_at"])
                emit_task_diff_events(old_state=old, task=mover, actor=request.user)
    detail_url = f"/projects/{target.slug_prefix}/{task.number}/"
    response = HttpResponse(status=204)
    if request.POST.get("modal") == "1":
        # Moved from the modal overlay: reload the modal body at the new
        # slug (the task's URL changed) and tell the board behind it to
        # refetch so the moved card lands in the right place.
        response["HX-Location"] = json.dumps(
            {
                "path": f"{detail_url}?modal=1",
                "target": "#modal-root",
                "swap": "innerHTML",
            },
        )
        response["HX-Trigger"] = "acta:task-moved"
    else:
        response["HX-Location"] = json.dumps(
            {
                "path": detail_url,
                "target": "#app-content",
                "select": "#app-content",
                "swap": "outerHTML show:top",
                "headers": {"HX-Boosted": "true"},
            },
        )
    return response


@require_POST
@login_required
def set_task_description(request, slug_prefix, number):
    """Inline description change; returns only the OOB activity list.

    Description is optional — an empty string is allowed (clears the
    description). No length cap beyond the model's ``TextField``. The
    delta is captured by the ``task.updated`` event under
    ``payload.changes.description`` (handled by
    :func:`build_diff_events`, which only stores the old/new lengths
    — not the full text — to keep activity payloads bounded).

    Unlike other inline edits this endpoint **does not** re-render the
    description cell. The editor (TipTap) holds the canonical text in
    the browser already — re-swapping the cell would unmount the
    editor, briefly collapse the wrapper height, and visibly scroll
    the page. The template pairs this with ``hx-swap="none"`` so the
    only swap that actually happens is the OOB-mounted activity list.
    The client-side editor JS updates its baseline after a successful
    save so a second blur with the same value doesn't re-submit.

    Args:
        request: Django request carrying a ``description`` form field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        A response containing **only** the OOB-swapped activity list.
        The form's ``hx-swap="none"`` means the activity is merged in
        and the description cell DOM stays intact.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    # Empty string is valid (clears the description). No strip — the
    # editor produces canonical markdown and trailing whitespace can
    # be meaningful inside code blocks.
    new_description = request.POST.get("description", "")
    _apply_task_field_change(task, "description", new_description, request.user)
    activity_html = render_to_string(
        "web/projects/_activity_oob.html",
        {
            "task": task,
            "timeline": _build_timeline(task, request.user.id),
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
        },
        request=request,
    )
    return HttpResponse(activity_html)


@require_POST
@login_required
def set_task_title(request, slug_prefix, number):
    """Inline title change; returns the title-cell fragment.

    Title is required and must not exceed the model's ``max_length``
    (200). Empty / whitespace-only / overlong values 400. The title
    change is captured by the ``task.updated`` event under
    ``payload.changes.title`` (handled by :func:`build_diff_events`).

    Args:
        request: Django request carrying a ``title`` form field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_title_cell.html`` with the updated task.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    new_title = (request.POST.get("title") or "").strip()
    if not new_title:
        return HttpResponseBadRequest("title required")
    if len(new_title) > 200:
        return HttpResponseBadRequest("title too long")
    _apply_task_field_change(task, "title", new_title, request.user)
    response = _inline_edit_response(
        request,
        task,
        "web/projects/_title_cell.html",
        {"task": task},
    )
    topbar_html = render_to_string(
        "web/projects/_topbar_task_title.html",
        {"task": task, "hx_oob": True},
        request=request,
    )
    response.content += topbar_html.encode()
    return response


@require_POST
@login_required
def toggle_task_label(request, slug_prefix, number):
    """Atomically attach or detach a single label on the task.

    The label must belong to the task's workspace — cross-workspace
    ids are rejected with 400. The operation is run inside a
    transaction so the resulting ``task.labels_changed`` activity
    event commits together with the M2M write.

    Args:
        request: Django request carrying a ``label_id`` form field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_labels_cell.html`` with the updated task.
    """

    task = _get_user_task_or_404(request.user, slug_prefix, number)
    raw = (request.POST.get("label_id") or "").strip()
    try:
        label_id = int(raw)
    except (TypeError, ValueError):
        return HttpResponseBadRequest("invalid label_id")
    label = Label.objects.filter(
        workspace=task.project.workspace,
        id=label_id,
    ).first()
    if label is None:
        return HttpResponseBadRequest("label not in this workspace")
    with transaction.atomic():
        old = snapshot_task(task)
        if task.labels.filter(id=label.id).exists():
            task.labels.remove(label)
        else:
            # ``add_labels_to_tasks`` enforces exclusive-group semantics:
            # if ``label.group.is_exclusive``, sibling labels from the same
            # group are detached from this task before the attach. The
            # helper writes the M2M through table directly, which doesn't
            # invalidate Django's prefetch cache — bust it so the diff
            # below sees the fresh label set.
            add_labels_to_tasks([task.id], [label.id])
            if hasattr(task, "_prefetched_objects_cache"):
                task._prefetched_objects_cache.pop("labels", None)
        emit_task_diff_events(old_state=old, task=task, actor=request.user)
    # ``trigger_layout`` is round-tripped via a hidden input in
    # ``_labels_dropdown_inner.html``: rail uses ``"column"``, modal uses
    # ``"row"``. Without echoing it back here, every toggle would reset
    # the trigger to the default layout, jerking the chip arrangement on
    # one surface or the other.
    trigger_layout = request.POST.get("trigger_layout") or "row"
    ctx = {
        "task": task,
        "workspace_labels": _workspace_labels(task),
        "workspace_label_groups": _workspace_label_groups(task),
        "attached_label_ids": set(task.labels.values_list("id", flat=True)),
        "trigger_layout": trigger_layout,
    }
    # Primary swap: the trigger contents (chips or placeholder). Keeping
    # the outer #labels-cell intact preserves the Alpine state — the
    # dropdown stays open, the search box stays focused with its query,
    # so consecutive label toggles work without reopening the picker.
    trigger_html = render_to_string(
        "web/projects/_labels_trigger.html",
        ctx,
        request=request,
    )
    # OOB: dropdown rows, so the ✓ marks update alongside the chips.
    dropdown_html = render_to_string(
        "web/projects/_labels_dropdown_inner.html",
        {**ctx, "oob": True},
        request=request,
    )
    # OOB: activity timeline.
    activity_html = render_to_string(
        "web/projects/_activity_oob.html",
        {
            "task": task,
            "timeline": _build_timeline(task, request.user.id),
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
        },
        request=request,
    )
    return HttpResponse(trigger_html + dropdown_html + activity_html)


_LINK_KINDS = {"blocks", "blocked_by", "related"}


def _resolve_link_target(user, raw_slug):
    """Resolve a ``PREFIX-NUMBER`` slug to a Task in the user's workspaces.

    Returns ``None`` when the slug is malformed or points at a task
    the user can't see — callers turn that into a 400.
    """
    raw = (raw_slug or "").strip().upper()
    try:
        prefix, num = raw.rsplit("-", 1)
        num_int = int(num)
    except (ValueError, AttributeError):
        return None
    return _user_task_qs(user).filter(project__slug_prefix=prefix, number=num_int).first()


def _links_panel_response(request, task):
    """Render the links panel partial + OOB activity timeline."""
    panel = render_to_string(
        "web/projects/_links_panel.html",
        {"task": task, "status_labels": Task.STATUS_LABELS},
        request=request,
    )
    activity = render_to_string(
        "web/projects/_activity_oob.html",
        {
            "task": task,
            "timeline": _build_timeline(task, request.user.id),
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
        },
        request=request,
    )
    return HttpResponse(panel + activity)


@login_required
def task_links_fragment(request, slug_prefix, number):
    """Re-render the links panel for a live refresh.

    Used when a link changes outside the panel's own add / remove forms —
    e.g. "Create task from comment" links the new task server-side, then
    fires ``acta:link-changed`` so the panel (which listens for it)
    refetches and shows the freshly linked task without a page reload.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    return _links_panel_response(request, task)


@login_required
def task_link_search(request, slug_prefix, number):
    """Typeahead search for the link-target picker.

    Returns up to 10 tasks in the same workspace (excluding this task
    and already-linked ones), matched by title (icontains), full slug
    (``PREFIX-NUMBER``), or bare number. Optional ``status`` filter
    narrows by task status. JSON payload feeds the Alpine autocomplete
    in the links panel.
    """
    from django.db.models import Q

    task = _get_user_task_or_404(request.user, slug_prefix, number)
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    # Already-linked tasks shouldn't show up as add candidates.
    linked_ids = set(task.blocks.values_list("id", flat=True))
    linked_ids |= set(task.blocked_by.values_list("id", flat=True))
    linked_ids |= set(task.related.values_list("id", flat=True))
    linked_ids.add(task.pk)

    qs = (
        _user_task_qs(request.user)
        .filter(project__workspace_id=task.project.workspace_id)
        .exclude(pk__in=linked_ids)
        .select_related("project", "assignee")
    )
    if status in Task.STATUS_VALUES:
        qs = qs.filter(status=status)
    if q:
        match = Q(title__icontains=q)
        upper = q.upper()
        if "-" in upper:
            prefix, _, num = upper.rpartition("-")
            if num.isdigit():
                match |= Q(project__slug_prefix=prefix, number=int(num))
        elif q.isdigit():
            match |= Q(number=int(q))
        qs = qs.filter(match)

    results = []
    for t in qs.order_by("-updated_at")[:10]:
        assignee = None
        if t.assignee_id:
            assignee = {
                "username": t.assignee.username,
                "initial": t.assignee.display_name[:1].upper(),
                "avatar_color": t.assignee.avatar_color,
                "avatar_url": (
                    reverse("accounts:serve_avatar", kwargs={"user_id": t.assignee_id})
                    + f"?v={t.assignee.avatar_version}"
                    if t.assignee.avatar
                    else None
                ),
            }
        results.append(
            {
                "slug": t.slug,
                "title": t.title,
                "status": t.status,
                "project": t.project.name,
                "assignee": assignee,
            }
        )
    return JsonResponse({"results": results})


@login_required
def mention_search(request, slug_prefix):
    """Combined ``@``-mention typeahead: workspace members + tasks.

    Feeds the editor's ``@``-picker with two sections — **Users** (matched
    by username / first / last name) and **Issues** (by title or
    ``PREFIX-NUMBER`` slug). With ``?id=<n>`` it returns a single member
    card for the chip hover popover; with ``?task_id=<n>`` it returns a
    task card (status / priority / assignee / due / labels). All results
    are scoped to the project's workspace.
    """
    from django.db.models import Q

    project = _get_user_project_or_404(request.user, slug_prefix)
    workspace_id = project.workspace_id

    card_id = (request.GET.get("id") or "").strip()
    if card_id:
        member = (
            WorkspaceMember.objects.filter(workspace_id=workspace_id, user_id=card_id).select_related("user").first()
        )
        if member is None:
            return JsonResponse({"user": None}, status=404)
        u = member.user
        return JsonResponse(
            {"user": {"id": u.id, "username": u.username, "name": u.display_name, "avatar_color": u.avatar_color}}
        )

    task_card_id = (request.GET.get("task_id") or "").strip()
    if task_card_id:
        t = (
            Task.objects.filter(project__workspace_id=workspace_id, id=task_card_id)
            .select_related("project", "assignee")
            .prefetch_related("labels")
            .first()
        )
        if t is None:
            return JsonResponse({"task": None}, status=404)
        assignee = None
        if t.assignee_id:
            assignee = {
                "name": t.assignee.display_name,
                "initial": t.assignee.display_name[:1].upper(),
                "avatar_color": t.assignee.avatar_color,
            }
        return JsonResponse(
            {
                "task": {
                    "slug": t.slug,
                    "title": t.title,
                    "status": t.status,
                    "status_label": str(Task.STATUS_LABELS.get(t.status, t.status)),
                    "priority": t.priority,
                    "priority_label": str(dict(Task.PRIORITY_CHOICES).get(t.priority, "")),
                    "assignee": assignee,
                    "due_date": t.due_date.isoformat() if t.due_date else None,
                    "labels": [{"name": label.name, "color": label.color} for label in t.labels.all()],
                }
            }
        )

    q = (request.GET.get("q") or "").strip()

    members = WorkspaceMember.objects.filter(workspace_id=workspace_id).select_related("user")
    if q:
        members = members.filter(
            Q(user__username__icontains=q) | Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q)
        )
    users = [
        {
            "id": m.user_id,
            "username": m.user.username,
            "name": m.user.display_name,
            "avatar_color": m.user.avatar_color,
        }
        for m in members.order_by("user__username")[:6]
    ]

    task_qs = Task.objects.filter(
        project__workspace_id=workspace_id,
        project__workspace__memberships__user=request.user,
        archived_at__isnull=True,
    ).select_related("project")
    if q:
        match = Q(title__icontains=q)
        upper = q.upper()
        if "-" in upper:
            prefix, _, num = upper.rpartition("-")
            if num.isdigit():
                match |= Q(project__slug_prefix=prefix, number=int(num))
        elif q.isdigit():
            match |= Q(number=int(q))
        task_qs = task_qs.filter(match)
    tasks = [
        {"id": t.id, "slug": t.slug, "title": t.title, "status": t.status} for t in task_qs.order_by("-updated_at")[:6]
    ]

    return JsonResponse({"users": users, "tasks": tasks})


@require_POST
@login_required
def add_task_link(request, slug_prefix, number):
    """Add a dependency / relation link from this task to another.

    Form fields: ``kind`` (blocks / blocked_by / related) + ``target``
    (a ``PREFIX-NUMBER`` slug). Validates the target is in the same
    workspace, isn't the task itself, and — for blocks — doesn't create
    a direct reciprocal block (A blocks B while B blocks A). Emits a
    ``task.link_added`` activity event on both endpoints of the link.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    kind = (request.POST.get("kind") or "").strip()
    if kind not in _LINK_KINDS:
        return HttpResponseBadRequest("invalid kind")
    target = _resolve_link_target(request.user, request.POST.get("target"))
    if target is None:
        return HttpResponseBadRequest("target not found")
    if target.pk == task.pk:
        return HttpResponseBadRequest("a task cannot link to itself")
    if target.project.workspace_id != task.project.workspace_id:
        return HttpResponseBadRequest("target is in a different workspace")

    with transaction.atomic():
        if kind == "related":
            task.related.add(target)
        elif kind == "blocks":
            # Reject direct reciprocal: if target already blocks task,
            # adding task-blocks-target makes a 2-cycle.
            if task.blocked_by.filter(pk=target.pk).exists():
                return HttpResponseBadRequest("that would create a circular block")
            task.blocks.add(target)
        else:  # blocked_by — the reverse direction
            if task.blocks.filter(pk=target.pk).exists():
                return HttpResponseBadRequest("that would create a circular block")
            target.blocks.add(task)
        broadcast_link_change(
            task=task,
            target=target,
            event_type="task.link_added",
            payload={"kind": kind, "target_slug": target.slug, "target_title": target.title},
            actor=request.user,
        )
    return _links_panel_response(request, task)


@require_POST
@login_required
def remove_task_link(request, slug_prefix, number):
    """Remove a dependency / relation link.

    Form fields: ``kind`` + ``target_id`` (the linked task's pk). The
    inverse of :func:`add_task_link` — symmetric for ``related``,
    directional for ``blocks`` / ``blocked_by``.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    kind = (request.POST.get("kind") or "").strip()
    if kind not in _LINK_KINDS:
        return HttpResponseBadRequest("invalid kind")
    try:
        target_id = int(request.POST.get("target_id") or "")
    except (TypeError, ValueError):
        return HttpResponseBadRequest("invalid target_id")
    target = _user_task_qs(request.user).filter(pk=target_id).first()
    if target is None:
        return HttpResponseBadRequest("target not found")

    with transaction.atomic():
        if kind == "related":
            task.related.remove(target)
        elif kind == "blocks":
            task.blocks.remove(target)
        else:  # blocked_by
            target.blocks.remove(task)
        broadcast_link_change(
            task=task,
            target=target,
            event_type="task.link_removed",
            payload={"kind": kind, "target_slug": target.slug, "target_title": target.title},
            actor=request.user,
        )
    return _links_panel_response(request, task)


def _attachments_panel_response(request, task, *, error=None, with_activity=True):
    """Render the task attachments panel, optionally + OOB activity refresh.

    Args:
        request: The current request (drives ``can_modify`` per attachment).
        task: The task whose panel to render.
        error: An inline error message to show in the panel, or ``None``.
        with_activity: Append the OOB activity-timeline fragment. Skipped on
            an error response, where nothing actually changed.

    Returns:
        An ``HttpResponse`` carrying the panel HTML (status 200 so HTMX
        swaps it even on a validation error).
    """
    html = render_to_string(
        "web/projects/_attachments_panel.html",
        {"task": task, "attachment_error": error},
        request=request,
    )
    if with_activity and error is None:
        html += render_to_string(
            "web/projects/_activity_oob.html",
            {
                "task": task,
                "timeline": _build_timeline(task, request.user.id),
                "status_labels": Task.STATUS_LABELS,
                "priority_labels": dict(Task.PRIORITY_CHOICES),
            },
            request=request,
        )
    return HttpResponse(html)


@require_POST
@login_required
def upload_task_attachment(request, slug_prefix, number):
    """Attach an uploaded file to a task (multipart POST, field ``file``)."""
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    upload = request.FILES.get("file")
    if upload is None:
        return HttpResponseBadRequest("file required")
    try:
        with transaction.atomic():
            attachment = create_task_attachment(task=task, uploader=request.user, uploaded_file=upload)
            log_event(
                workspace=task.project.workspace,
                project=task.project,
                actor=request.user,
                event_type="attachment.created",
                target_type=ActivityLog.TARGET_ATTACHMENT,
                target_id=attachment.id,
                payload={
                    "task_id": task.id,
                    "filename": attachment.original_name,
                    "size": attachment.size,
                },
            )
    except ValidationError as exc:
        return _attachments_panel_response(request, task, error="; ".join(exc.messages))
    return _attachments_panel_response(request, task)


@require_POST
@login_required
def delete_attachment(request, pk):
    """Delete one attachment, by its uploader or a workspace admin."""
    attachment = get_object_or_404(
        Attachment.objects.select_related("workspace", "task__project__workspace", "uploader"),
        pk=pk,
        workspace__memberships__user=request.user,
    )
    task = attachment.task
    if task is None:
        return HttpResponseBadRequest("not a task attachment")
    if attachment.uploader_id != request.user.id and not _is_workspace_admin(request.user.id, attachment.workspace_id):
        raise PermissionDenied
    filename = attachment.original_name
    attachment_id = attachment.id
    with transaction.atomic():
        attachment.delete()
        log_event(
            workspace=task.project.workspace,
            project=task.project,
            actor=request.user,
            event_type="attachment.deleted",
            target_type=ActivityLog.TARGET_ATTACHMENT,
            target_id=attachment_id,
            payload={"task_id": task.id, "filename": filename},
        )
    return _attachments_panel_response(request, task)


@login_required
def serve_attachment(request, pk):
    """Stream an attachment's file after a workspace-membership check.

    The membership filter on the queryset is the access gate — a non-member
    gets a 404, never the bytes. Delivery honors ATTACHMENT_SENDFILE_BACKEND
    (see ADR 0025 and ``apps.attachments.serving``).
    """
    attachment = get_object_or_404(
        Attachment.objects.filter(workspace__memberships__user=request.user),
        pk=pk,
    )
    return serve_attachment_response(attachment)


@require_POST
@login_required
def upload_task_inline_image(request, slug_prefix, number):
    """Store an image embedded in a task's description or a comment on it.

    Called by the TipTap editor's paste/drop handler (description cell and
    the comment composers). The image is owned by the task; the returned
    URL is the auth-gated serve endpoint, which the editor inserts as an
    ``<img>`` and the saved markdown references as ``![](url)``.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    upload = request.FILES.get("image")
    if upload is None:
        return HttpResponseBadRequest("image required")
    try:
        with transaction.atomic():
            attachment = create_inline_image(
                owner_field="task",
                owner=task,
                workspace=task.project.workspace,
                uploader=request.user,
                uploaded_file=upload,
            )
    except ValidationError as exc:
        return JsonResponse({"error": "; ".join(exc.messages)}, status=400)
    return JsonResponse({"url": reverse("web:serve_attachment", kwargs={"pk": attachment.id})})


@require_POST
@login_required
def upload_project_inline_image(request, slug_prefix):
    """Store an image embedded in a project description; return ``{"url": …}``."""
    project = _get_user_project_or_404(request.user, slug_prefix)
    upload = request.FILES.get("image")
    if upload is None:
        return HttpResponseBadRequest("image required")
    try:
        with transaction.atomic():
            attachment = create_inline_image(
                owner_field="project",
                owner=project,
                workspace=project.workspace,
                uploader=request.user,
                uploaded_file=upload,
            )
    except ValidationError as exc:
        return JsonResponse({"error": "; ".join(exc.messages)}, status=400)
    return JsonResponse({"url": reverse("web:serve_attachment", kwargs={"pk": attachment.id})})


@require_POST
@login_required
def set_task_assignee(request, slug_prefix, number):
    """Inline assignee change; returns the assignee cell fragment.

    Accepts an integer ``assignee_id`` form field, or an empty value to
    unassign. The user must be a member of the task's workspace —
    non-member ids return 400 rather than 404, since the request is
    against an existing task but with malformed input.

    Args:
        request: Django request carrying an ``assignee_id`` form field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_assignee_cell.html`` with the updated task.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    raw = (request.POST.get("assignee_id") or "").strip()
    if raw == "":
        new_assignee = None
    else:
        try:
            user_id = int(raw)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("invalid assignee_id")
        new_assignee = User.objects.filter(
            workspace_memberships__workspace=task.project.workspace,
            id=user_id,
        ).first()
        if new_assignee is None:
            return HttpResponseBadRequest("user not a workspace member")
    _apply_task_field_change(task, "assignee", new_assignee, request.user)
    return _inline_edit_response(
        request,
        task,
        "web/projects/_assignee_cell.html",
        {
            "task": task,
            "workspace_members": _workspace_members(task),
        },
    )


def _can_edit_task_dates(user, task):
    """Whether ``user`` may change the task's start / end dates.

    Scheduling a task's timeline (start / end) is the assignee's call:
    only the current assignee may move those dates. An **unassigned** task
    is open to any workspace member (someone has to be able to plan it
    before it's picked up). The hard ``due_date`` deadline is intentionally
    not restricted — anyone can set a deadline. See the timeline / rail
    date cells.

    Args:
        user: The acting request user.
        task: The :class:`Task` being edited.

    Returns:
        ``True`` when the edit is allowed.
    """
    return task.assignee_id is None or task.assignee_id == user.id


@require_POST
@login_required
def set_task_due_date(request, slug_prefix, number):
    """Inline due-date change; returns the due-date cell fragment.

    Accepts an ISO-8601 date string (``YYYY-MM-DD``) in the ``due_date``
    form field, or an empty value to clear the deadline. Invalid formats
    are rejected with 400 — the picker only ever submits ISO dates, so
    anything else is a hand-rolled request and not worth tolerating.

    Args:
        request: Django request carrying a ``due_date`` form field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_due_date_cell.html`` with the updated task.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    raw = (request.POST.get("due_date") or "").strip()
    if raw == "":
        new_due_date = None
    else:
        try:
            new_due_date = datetime.date.fromisoformat(raw)
        except ValueError:
            return HttpResponseBadRequest("invalid due_date")
    _apply_task_field_change(task, "due_date", new_due_date, request.user)
    response = _inline_edit_response(
        request,
        task,
        "web/projects/_due_date_cell.html",
        {"task": task},
    )
    # Refetch the active view panel so date-driven surfaces (the timeline
    # Gantt, date-sorted / date-filtered lists) reflect the new deadline
    # without a reload — e.g. editing the deadline in the task modal redraws
    # the Gantt bar underneath it. The timeline's drag-resize hits this same
    # endpoint via raw ``fetch`` (not HTMX), so it ignores the header and
    # won't trigger a refetch loop (it redraws its bar locally instead).
    response["HX-Trigger"] = "acta:task-changed"
    return response


@require_POST
@login_required
def set_task_start_date(request, slug_prefix, number):
    """Inline start-date change; rail cell editor + timeline drag.

    Accepts ``start_date`` as an ISO-8601 date string or empty string
    (clears the field). Returns the ``_start_date_cell.html`` fragment so
    the rail picker swaps in place; the timeline drag posts the same form
    via raw ``fetch`` and simply ignores the response body.

    Args:
        request: Django request with a ``start_date`` POST field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_start_date_cell.html`` plus the panel-refetch trigger.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    if not _can_edit_task_dates(request.user, task):
        return HttpResponseForbidden("Only the assignee can change the start/end date.")
    raw = (request.POST.get("start_date") or "").strip()
    if raw == "":
        new_start_date = None
    else:
        try:
            new_start_date = datetime.date.fromisoformat(raw)
        except ValueError:
            return HttpResponseBadRequest("invalid start_date")
    _apply_task_field_change(task, "start_date", new_start_date, request.user)
    response = _inline_edit_response(
        request,
        task,
        "web/projects/_start_date_cell.html",
        {"task": task, "compact": request.POST.get("compact") == "1"},
    )
    # See ``set_task_due_date``: refetch date-driven panels on an HTMX edit.
    # The timeline drag posts here via raw ``fetch`` and ignores this header.
    response["HX-Trigger"] = "acta:task-changed"
    return response


@require_POST
@login_required
def set_task_end_date(request, slug_prefix, number):
    """Inline planned-finish ("End") change; rail cell editor + timeline drag.

    Accepts ``end_date`` as an ISO-8601 date string or empty string
    (clears the field). ``end_date`` drives the right edge of the timeline
    bar and is separate from the hard ``due_date`` deadline. Returns the
    ``_end_date_cell.html`` fragment; the timeline drag posts via raw
    ``fetch`` and ignores the body.

    Args:
        request: Django request with an ``end_date`` POST field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_end_date_cell.html`` plus the panel-refetch trigger.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    if not _can_edit_task_dates(request.user, task):
        return HttpResponseForbidden("Only the assignee can change the start/end date.")
    raw = (request.POST.get("end_date") or "").strip()
    if raw == "":
        new_end_date = None
    else:
        try:
            new_end_date = datetime.date.fromisoformat(raw)
        except ValueError:
            return HttpResponseBadRequest("invalid end_date")
    _apply_task_field_change(task, "end_date", new_end_date, request.user)
    response = _inline_edit_response(
        request,
        task,
        "web/projects/_end_date_cell.html",
        {"task": task, "compact": request.POST.get("compact") == "1"},
    )
    response["HX-Trigger"] = "acta:task-changed"
    return response


@require_POST
@login_required
@require_POST
@login_required
def switch_workspace(request, workspace_id):
    """Set the user's active workspace and reload into it.

    Acta scopes All Tasks / Projects / My Work / Inbox / My Activity to a
    single active workspace; the sidebar switcher POSTs here to change it.
    Membership is enforced (a user can only switch into a workspace they
    belong to). On success we persist ``User.active_workspace`` and send
    the browser to that workspace's project list — via ``HX-Redirect`` so
    a boosted form POST does a full reload (sidebar, favourites and the
    unread badge all re-render for the new scope).

    Args:
        request: POST request.
        workspace_id: PK of the workspace to switch into.
    """
    workspace = get_object_or_404(
        Workspace.objects.filter(memberships__user=request.user),
        pk=workspace_id,
    )
    request.user.active_workspace = workspace
    request.user.save(update_fields=["active_workspace"])
    target = reverse("web:project_list")
    if request.headers.get("HX-Request") == "true":
        resp = HttpResponse(status=204)
        resp["HX-Redirect"] = target
        return resp
    return redirect(target)


@require_POST
@login_required
def toggle_project_favourite(request, slug_prefix):
    """Star / unstar a project from the user's favourites.

    Sidebar nav lists only favourited projects. Project list cards
    expose a star toggle that POSTs here; response is the freshly
    rendered star button (so the icon flips state in place) plus an
    OOB swap of the sidebar nav list so the new entry appears /
    disappears live.
    """
    project = _get_user_project_or_404(request.user, slug_prefix)
    user = request.user
    if user.favourite_projects.filter(pk=project.pk).exists():
        user.favourite_projects.remove(project)
        is_favourite = False
    else:
        user.favourite_projects.add(project)
        is_favourite = True
    star_html = render_to_string(
        "web/projects/_project_favourite_star.html",
        {"project": project, "is_favourite": is_favourite, "oob": True},
        request=request,
    )
    # The favourites OOB reads ``active_workspace`` / ``nav_has_favourites``
    # straight from the ``workspace_nav`` context processor (request passed),
    # so it re-renders the active workspace's freshly-updated favourites.
    sidebar_oob = render_to_string(
        "web/_sidebar_favourites_oob.html",
        {},
        request=request,
    )
    return HttpResponse(star_html + sidebar_oob)


@require_POST
@login_required
def set_project_icon(request, slug_prefix):
    """Set the project's Lucide icon name from the curated picker.

    ``Project.icon`` is a free-text Lucide name (admin can set anything;
    the renderer falls back to ``folder`` on unknowns). The user-facing
    picker only exposes a curated subset listed in
    ``apps.projects.icons.PROJECT_ICONS`` — submissions outside that
    list are 400'd so an end-user can't paste in a custom Lucide name
    that would render but break the visual catalog.

    Empty value clears the icon (reverts to the ``folder`` default).
    Response is the freshly-rendered icon thumb that the picker swaps
    into ``#project-icon-thumb`` (the trigger element on the overview
    header) so the change is visible without a full page reload.
    """
    from apps.projects.icons import is_curated, is_curated_color

    project = _get_user_project_or_404(request.user, slug_prefix)
    # Each field is updated only when present in the form so the
    # colour-picker form (which omits ``icon``) doesn't accidentally
    # blank the icon, and vice-versa for the icon grid (which omits
    # ``icon_color``). Empty-string submissions are explicit clears.
    update_fields = []
    if "icon" in request.POST:
        icon = request.POST.get("icon", "").strip()
        if icon and not is_curated(icon):
            return HttpResponseBadRequest("icon not in curated set")
        if project.icon != icon:
            project.icon = icon
            update_fields.append("icon")
    if "icon_color" in request.POST:
        color = request.POST.get("icon_color", "").strip()
        if color and not is_curated_color(color):
            return HttpResponseBadRequest("icon_color not in curated palette")
        if project.icon_color != color:
            project.icon_color = color
            update_fields.append("icon_color")
    if update_fields:
        project.save(update_fields=update_fields)
    thumb_html = render_to_string(
        "web/projects/_project_icon_thumb.html",
        {"project": project},
        request=request,
    )
    # OOB swap: the sidebar nav also renders this project's icon via
    # ``[data-project-icon-for]``. Appending the OOB partial here means
    # the rail icon refreshes the moment the picker fires — no need to
    # navigate or hard-refresh.
    sidebar_oob = render_to_string(
        "web/projects/_project_icon_sidebar_oob.html",
        {"project": project},
        request=request,
    )
    return HttpResponse(thumb_html + sidebar_oob)


@require_POST
@login_required
def set_project_description(request, slug_prefix):
    """Inline description (Markdown) update on the project overview.

    Mirrors the task-description edit flow: the TipTap editor blurs,
    submits the latest Markdown via HTMX, server saves and re-renders
    the cell. No length cap — descriptions can be long.
    """
    project = _get_user_project_or_404(request.user, slug_prefix)
    new_description = request.POST.get("description", "")
    project.description = new_description
    project.save(update_fields=["description"])
    # Same trick as the task description endpoint — return 204 + no
    # body and let the editor JS update its baseline. Re-rendering the
    # cell would re-mount TipTap and cause the page-scroll hop Vox
    # reported.
    return HttpResponse(status=204)


def _get_user_project_or_404(user, slug_prefix):
    """Look up a project by slug_prefix, 404 when foreign / missing.

    Args:
        user: Acting :class:`User`.
        slug_prefix: Project slug prefix from the URL.

    Returns:
        The :class:`Project` instance with ``workspace`` + ``lead``
        pre-fetched.
    """
    return get_object_or_404(
        Project.objects.filter(
            slug_prefix=slug_prefix,
            workspace__memberships__user=user,
        ).select_related("workspace", "lead"),
    )


@require_POST
@login_required
def set_project_archived(request, slug_prefix):
    """Archive or unarchive a project — soft hide, all data retained.

    Owner/admin only. Reads ``archived`` (``"1"``/``"0"``). Archived
    projects drop out of the sidebar favourites and the active project
    list (the list shows them only under "Show archived"); an archived
    project keeps an Unarchive control on its overview. Redirects to the
    project list on archive and back to the overview on restore, with a
    flash toast.
    """
    project = _get_user_project_or_404(request.user, slug_prefix)
    if not _user_is_workspace_admin(request.user, project.workspace):
        return HttpResponseForbidden("admin only")
    archived = request.POST.get("archived") == "1"
    if project.archived != archived:
        project.archived = archived
        project.save(update_fields=["archived"])
    if archived:
        messages.success(request, _("Project “%(name)s” archived.") % {"name": project.name})
        return redirect("web:project_list")
    messages.success(request, _("Project “%(name)s” restored.") % {"name": project.name})
    return redirect("web:project_detail", slug_prefix=project.slug_prefix)


@require_POST
@login_required
def delete_project(request, slug_prefix):
    """Permanently delete a project and everything under it (DB cascade).

    Owner/admin only; irreversible. Requires ``confirm_slug`` to equal the
    project's ``slug_prefix`` — a typed-confirmation guard against an
    accidental delete. Cascades to tasks, comments, attachments, activity
    and the rest via their FKs. Redirects to the project list.
    """
    project = _get_user_project_or_404(request.user, slug_prefix)
    if not _user_is_workspace_admin(request.user, project.workspace):
        return HttpResponseForbidden("admin only")
    if (request.POST.get("confirm_slug") or "").strip() != project.slug_prefix:
        return HttpResponseBadRequest("slug confirmation does not match")
    name = project.name
    project.delete()
    messages.success(request, _("Project “%(name)s” deleted.") % {"name": name})
    return redirect("web:project_list")


@require_POST
@login_required
def transfer_workspace_ownership(request, slug):
    """Hand workspace ownership to another member — owner only (ADR 0010).

    Reads ``new_owner_id`` (a current member). In one transaction the target
    membership becomes ``OWNER``, the previous owner is demoted to ``ADMIN``
    (keeps full access), and the ``Workspace.owner`` FK is repointed — the
    two representations of ownership stay in sync. Exactly one owner remains.
    """
    workspace = _get_user_workspace_or_404(request.user, slug)
    if not _user_is_workspace_owner(request.user, workspace):
        return HttpResponseForbidden("owner only")
    try:
        new_owner_id = int(request.POST.get("new_owner_id") or "")
    except ValueError:
        return HttpResponseBadRequest("invalid member")
    if new_owner_id == request.user.id:
        return HttpResponseBadRequest("already the owner")
    new_membership = (
        WorkspaceMember.objects.filter(workspace=workspace, user_id=new_owner_id).select_related("user").first()
    )
    if new_membership is None:
        return HttpResponseBadRequest("not a workspace member")
    with transaction.atomic():
        old_membership = WorkspaceMember.objects.filter(workspace=workspace, user=request.user).first()
        new_membership.role = WorkspaceMember.OWNER
        new_membership.save(update_fields=["role"])
        if old_membership is not None:
            old_membership.role = WorkspaceMember.ADMIN
            old_membership.save(update_fields=["role"])
        workspace.owner = new_membership.user
        workspace.save(update_fields=["owner"])
    messages.success(
        request,
        _("Ownership transferred to %(name)s — you are now an admin.") % {"name": new_membership.user.display_name},
    )
    return redirect("web:workspace_settings", slug=workspace.slug)


@require_POST
@login_required
def delete_workspace(request, slug):
    """Permanently delete a workspace and everything in it — owner only.

    Irreversible. Requires ``confirm_slug`` to equal the workspace ``slug``
    (typed-confirmation guard). Cascades to every project, task, comment,
    membership, invite and cycle via their FKs; members' ``active_workspace``
    is ``SET_NULL``, so no user is harmed. Redirects to the dashboard, where
    the active workspace re-resolves to a remaining one (or the empty state).
    """
    workspace = _get_user_workspace_or_404(request.user, slug)
    if not _user_is_workspace_owner(request.user, workspace):
        return HttpResponseForbidden("owner only")
    if (request.POST.get("confirm_slug") or "").strip() != workspace.slug:
        return HttpResponseBadRequest("slug confirmation does not match")
    name = workspace.name
    workspace.delete()
    messages.success(request, _("Workspace “%(name)s” deleted.") % {"name": name})
    return redirect("web:dashboard")


def _settings_panel_response(request, template, context, *, toast=None):
    """Render a settings-card partial, with an optional HX-Trigger toast.

    Lets the WIP / cadence save endpoints swap just their card in place
    (no full-page reload) and ride a success toast on the response, the
    same way the invite panel does.
    """
    response = HttpResponse(render_to_string(template, context, request=request))
    if toast is not None:
        import json

        # ``default=str`` coerces any lazy ``gettext_lazy`` proxy in the
        # toast payload — those aren't JSON-serializable on their own.
        response["HX-Trigger"] = json.dumps({"acta:toast": toast}, default=str)
    return response


@require_POST
@login_required
def set_workspace_general(request, slug):
    """Save the General settings panel — name + basic workspace policy.

    Admin-gated. Updates the display ``name``, the auto-archive horizon
    (``auto_archive_done_after_days``; blank or 0 disables it), and the
    member-announcement toggle. ``slug`` / ``owner`` are not editable here
    (slug is a URL key; owner changes go through a transfer flow).
    """
    workspace = _get_user_workspace_or_404(request.user, slug)
    if not _user_is_workspace_admin(request.user, workspace):
        return HttpResponseForbidden("admin only")
    name = (request.POST.get("name") or "").strip()
    if not name:
        return HttpResponseBadRequest("name is required")
    raw_archive = (request.POST.get("auto_archive_done_after_days") or "").strip()
    if raw_archive:
        try:
            archive_days = int(raw_archive)
        except ValueError:
            return HttpResponseBadRequest("invalid auto-archive value")
        if archive_days < 0:
            return HttpResponseBadRequest("invalid auto-archive value")
        archive_days = archive_days or None
    else:
        archive_days = None
    workspace.name = name[:120]
    workspace.auto_archive_done_after_days = archive_days
    workspace.allow_member_announcements = bool(request.POST.get("allow_member_announcements"))
    workspace.save(update_fields=["name", "auto_archive_done_after_days", "allow_member_announcements"])
    if request.headers.get("HX-Request"):
        return _settings_panel_response(
            request,
            "web/workspaces/_settings_general.html",
            _render_workspace_general(workspace, viewer_is_admin=True),
            toast={"message": str(_("General settings saved.")), "level": "success"},
        )
    return redirect("web:workspace_settings", slug=workspace.slug)


@require_POST
@login_required
def set_workspace_wip(request, slug):
    """Save the workspace-wide WIP policy from the settings panel.

    Admin-gated. Reads ``mode`` (``off`` / ``personal`` / ``column``)
    and a ``limit_<status>`` field per kanban status; stores them in
    ``Workspace.wip_limits`` as ``{"mode": …, "limits": {status: n}}``
    (only positive limits kept). Redirects back to the settings page.
    """
    workspace = _get_user_workspace_or_404(request.user, slug)
    if not _user_is_workspace_admin(request.user, workspace):
        return HttpResponseForbidden("admin only")
    mode = request.POST.get("mode", Workspace.WIP_OFF)
    if mode not in {Workspace.WIP_OFF, Workspace.WIP_PERSONAL, Workspace.WIP_COLUMN}:
        return HttpResponseBadRequest("invalid mode")
    limits = {}
    for status in Task.KANBAN_STATUS_VALUES:
        raw = (request.POST.get(f"limit_{status}") or "").strip()
        if not raw:
            continue
        try:
            n = int(raw)
        except ValueError:
            return HttpResponseBadRequest("invalid limit")
        if n > 0:
            limits[status] = n
    workspace.wip_limits = {"mode": mode, "limits": limits}
    workspace.save(update_fields=["wip_limits"])
    if request.headers.get("HX-Request"):
        return _settings_panel_response(
            request,
            "web/workspaces/_settings_wip.html",
            _render_workspace_wip(workspace, viewer_is_admin=True),
            toast={"message": str(_("WIP limits saved.")), "level": "success"},
        )
    return redirect("web:workspace_settings", slug=workspace.slug)


@require_POST
@login_required
def set_workspace_cycles(request, slug):
    """Save the workspace cadence config from the settings panel.

    Admin-gated. Reads ``enabled`` (checkbox), ``length_weeks`` and
    ``start_date`` (ISO anchor of cycle 1) and stores them in
    ``Workspace.cycle_settings``. Enabling without a start date defaults
    the anchor to today. When the resulting config is enabled the current
    + next cycle are materialized right away so they're ready to assign
    to. Redirects back to the settings page.
    """
    workspace = _get_user_workspace_or_404(request.user, slug)
    if not _user_is_workspace_admin(request.user, workspace):
        return HttpResponseForbidden("admin only")
    was_enabled = workspace.cycle_config()["enabled"]
    enabled = bool(request.POST.get("enabled"))
    try:
        length = int((request.POST.get("length_weeks") or "").strip() or Workspace.CYCLE_DEFAULT_LENGTH_WEEKS)
    except ValueError:
        return HttpResponseBadRequest("invalid length")
    length = max(1, min(length, Workspace.CYCLE_MAX_LENGTH_WEEKS))
    start_date = None
    start_raw = (request.POST.get("start_date") or "").strip()
    if start_raw:
        try:
            start_date = datetime.date.fromisoformat(start_raw).isoformat()
        except ValueError:
            return HttpResponseBadRequest("invalid start date")
    if enabled and not start_date:
        start_date = timezone.localdate().isoformat()
    workspace.cycle_settings = {
        "enabled": enabled,
        "length_weeks": length,
        "start_date": start_date,
        "auto_rollover": bool(request.POST.get("auto_rollover")),
    }
    workspace.save(update_fields=["cycle_settings"])
    now_enabled = workspace.cycle_config()["enabled"]
    if now_enabled:
        ensure_cycles(workspace)
    if request.headers.get("HX-Request"):
        # Enable-state transition flips ``nav_cycles_enabled`` in the sidebar
        # context processor — the in-place card swap can't update that (it
        # lives outside the card). Force a full refresh so the sidebar's
        # Cycles link appears/disappears immediately; the toast rides on
        # Django messages across the reload.
        if was_enabled != now_enabled:
            messages.success(request, str(_("Cycles saved.")))
            response = HttpResponse(status=204)
            response["HX-Refresh"] = "true"
            return response
        return _settings_panel_response(
            request,
            "web/workspaces/_settings_cycles.html",
            _render_workspace_cycles(workspace, viewer_is_admin=True),
            toast={"message": str(_("Cycles saved.")), "level": "success"},
        )
    return redirect("web:workspace_settings", slug=workspace.slug)


def _cycle_histogram(hours: list[float]) -> dict[str, list]:
    """Bucket a list of cycle/lead-time hours into day-based ranges.

    Returns a ``{labels, data}`` pair ready for a Chart.js bar chart.
    Buckets: <1d, 1-2d, 2-3d, 3-5d, 5-10d, 10d+.
    """
    bounds = [
        (24, "< 1d"),
        (48, "1-2d"),
        (72, "2-3d"),
        (120, "3-5d"),
        (240, "5-10d"),
        (float("inf"), "10d+"),
    ]
    counts = [0] * len(bounds)
    for h in hours:
        for i, (hi, _label) in enumerate(bounds):
            if h < hi:
                counts[i] += 1
                break
    return {"labels": [label for _hi, label in bounds], "data": counts}


@login_required
def project_insights(request, slug_prefix):
    """Flow-metrics insights page for a project (scrumban dashboard).

    Renders cycle time / lead time / weekly throughput computed live
    from the activity log (see :func:`apps.tasks.metrics.compute_flow_metrics`).
    Charts are drawn client-side with Chart.js from the JSON blobs in
    context.
    """
    project = _get_user_project_or_404(request.user, slug_prefix)
    metrics = compute_flow_metrics(project, weeks=8)
    cfd = compute_cfd(project, weeks=8)
    bottlenecks = compute_bottlenecks(project, weeks=8)
    throughput = metrics["throughput"]
    avg_throughput = round(sum(p["count"] for p in throughput) / len(throughput), 1) if throughput else 0

    def fmt(hours):
        if hours is None:
            return "—"
        if hours < 24:
            return f"{hours:.0f}h"
        return f"{hours / 24:.1f}d"

    # CFD bands, oldest status at the bottom — colours mirror the status
    # palette (planned zinc → done emerald).
    cfd_colors = {
        "planned": "rgb(113 113 122 / 0.55)",
        "ready": "rgb(6 182 212 / 0.55)",
        "to-do": "rgb(59 130 246 / 0.55)",
        "in-progress": "rgb(139 92 246 / 0.55)",
        "in-review": "rgb(245 158 11 / 0.55)",
        "done": "rgb(16 185 129 / 0.55)",
    }
    cfd_datasets = [
        {
            "label": str(Task.STATUS_LABELS[s]),
            "data": cfd["series"][s],
            "color": cfd_colors.get(s, "rgb(113 113 122 / 0.5)"),
        }
        for s in cfd["statuses"]
    ]
    tis = bottlenecks["time_in_status"]
    tis_statuses = [s for s in Task.KANBAN_STATUS_VALUES if s != Task.STATUS_DONE]

    ctx = {
        "project": project,
        "metrics": metrics,
        "bottlenecks": bottlenecks,
        "avg_throughput": avg_throughput,
        "cycle_median_fmt": fmt(metrics["cycle_median"]),
        "cycle_p85_fmt": fmt(metrics["cycle_p85"]),
        "lead_median_fmt": fmt(metrics["lead_median"]),
        "throughput_labels_json": json.dumps([p["label"] for p in throughput]),
        "throughput_data_json": json.dumps([p["count"] for p in throughput]),
        "cycle_hist_json": json.dumps(_cycle_histogram(metrics["cycle_times"])),
        "cfd_labels_json": json.dumps(cfd["labels"]),
        "cfd_datasets_json": json.dumps(cfd_datasets),
        "tis_labels_json": json.dumps([str(Task.STATUS_LABELS[s]) for s in tis_statuses]),
        "tis_data_json": json.dumps([tis.get(s, 0) for s in tis_statuses]),
        "wip_items": [(Task.STATUS_LABELS[s], bottlenecks["wip"].get(s, 0)) for s in tis_statuses],
    }
    return render(request, "web/projects/insights.html", ctx)


@login_required
def cycles_overview(request):
    """Workspace cycles dashboard — active-cycle burndown + velocity + list.

    Workspace-level (cycles span every project). Shows the active cycle's
    burndown (open tasks per day vs. the ideal line), a velocity bar over
    recent cycles, and the full cycle list with per-cycle progress. All
    charts are drawn client-side with Chart.js from JSON blobs, mirroring
    the project Insights page. Renders an empty state when cadence is off.
    """
    workspace = resolve_active_workspace(request)
    if workspace is None or not workspace.cycle_config()["enabled"]:
        return render(
            request,
            "web/cycles/overview.html",
            {"cycle_enabled": False, "workspace": workspace},
        )
    today = timezone.localdate()
    ensure_cycles(workspace, today)
    active = current_cycle(workspace, today)
    # Most recent cycles first, decorated with their progress summary.
    # Batch the summaries in one query (not one per cycle).
    cycles = list(workspace.cycles.order_by("-start_date")[:12])
    summaries = cycle_summaries(cycles, today)
    for cycle in cycles:
        cycle.summary = summaries[cycle.id]
    velocity = compute_velocity(workspace)
    ctx = {
        "cycle_enabled": True,
        "workspace": workspace,
        "active_cycle": active,
        "active_summary": cycle_summary(active, today) if active else None,
        "cycles": cycles,
        "velocity_labels_json": json.dumps([v["label"] for v in velocity]),
        "velocity_count_json": json.dumps([v["count"] for v in velocity]),
        "velocity_points_json": json.dumps([v["points"] for v in velocity]),
    }
    if active:
        burndown = compute_cycle_burndown(active, today)
        ctx["burndown_labels_json"] = json.dumps(burndown["labels"])
        ctx["burndown_ideal_json"] = json.dumps(burndown["ideal"])
        ctx["burndown_remaining_json"] = json.dumps(burndown["remaining"])
    return render(request, "web/cycles/overview.html", ctx)


@require_POST
@login_required
def set_project_lead(request, slug_prefix):
    """Inline lead change on the project overview.

    Owner/admin only. Accepts an integer ``lead_id`` form field, or an
    empty value to clear the lead. The chosen user must be a member of
    the project's workspace; non-member ids 400. Returns the rendered
    lead cell fragment so HTMX can swap it in place.
    """
    project = _get_user_project_or_404(request.user, slug_prefix)
    if not _user_is_workspace_admin(request.user, project.workspace):
        return HttpResponseForbidden("admin only")
    raw = (request.POST.get("lead_id") or "").strip()
    if raw == "":
        new_lead = None
    else:
        try:
            user_id = int(raw)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("invalid lead_id")
        new_lead = User.objects.filter(
            workspace_memberships__workspace=project.workspace,
            id=user_id,
        ).first()
        if new_lead is None:
            return HttpResponseBadRequest("user not a workspace member")
    project.lead = new_lead
    project.save(update_fields=["lead"])
    project.refresh_from_db(fields=["lead"])
    return HttpResponse(
        render_to_string(
            "web/projects/_overview_lead.html",
            {
                "project": project,
                "workspace_members": _project_workspace_members(project, exclude_user=None),
            },
            request=request,
        ),
    )


@require_POST
@login_required
def post_project_update(request, slug_prefix):
    """Create a status update from the project overview composer.

    Reads ``health`` (one of :attr:`ProjectUpdate.HEALTH_CHOICES`) and a
    Markdown ``body``, authored by the current user. Returns the rendered
    update card so HTMX can prepend it to the overview Updates list.

    Args:
        request: Django request carrying ``health`` + ``body`` fields.
        slug_prefix: Project slug prefix from the URL.

    Returns:
        Rendered ``_overview_update_card.html`` for the new update, or
        400 when ``health`` is invalid or ``body`` is empty.
    """
    project = _get_user_project_or_404(request.user, slug_prefix)
    health = (request.POST.get("health") or "").strip()
    if health not in {key for key, _ in ProjectUpdate.HEALTH_CHOICES}:
        return HttpResponseBadRequest("invalid health")
    body = (request.POST.get("body") or "").strip()
    if not body:
        return HttpResponseBadRequest("body required")
    update = ProjectUpdate.objects.create(
        project=project,
        author=request.user,
        health=health,
        body=body,
    )
    notify_project_update_created(update=update, actor=request.user)
    update.reaction_summary = []
    update.can_modify = True
    return HttpResponse(
        render_to_string(
            "web/projects/_overview_update_card.html",
            {
                "update": update,
                "health_labels": dict(ProjectUpdate.HEALTH_CHOICES),
            },
            request=request,
        ),
    )


@require_POST
@login_required
def post_announcement(request):
    """Broadcast an announcement to every member of the active workspace.

    Gated to owners/admins, or any member when the workspace enables
    ``allow_member_announcements``. Reads a ``title`` (subject) and a
    Markdown ``body``. Fans out one ANNOUNCEMENT notification per member via
    :func:`notify_announcement` (the sender is self-suppressed); linked
    members are always force-notified on Telegram too.

    Args:
        request: Django request carrying ``title`` / ``body``.

    Returns:
        ``HttpResponse`` 204 with an ``HX-Trigger`` carrying the recipient
        count on success; 400 when the active workspace or fields are
        missing; 403 when the user may not announce here.
    """
    workspace = resolve_active_workspace(request)
    if workspace is None:
        return HttpResponseBadRequest("no active workspace")
    if not (_is_workspace_admin(request.user.id, workspace.id) or workspace.allow_member_announcements):
        return HttpResponseForbidden("not allowed to announce")
    title = (request.POST.get("title") or "").strip()
    body = (request.POST.get("body") or "").strip()
    if not title or not body:
        return HttpResponseBadRequest("title and body required")
    count = notify_announcement(
        workspace_id=workspace.id,
        actor=request.user,
        title=title,
        body=body,
    )
    response = HttpResponse(status=204)
    response["HX-Trigger"] = json.dumps({"acta:announcement-sent": {"count": count}})
    return response


def _get_user_update_or_404(user, pk):
    """Resolve a project update scoped to the user's workspaces.

    Args:
        user: The acting :class:`User`.
        pk: ProjectUpdate primary key.

    Returns:
        The :class:`ProjectUpdate` with project + author preloaded.

    Raises:
        Http404: If the update is missing or in another workspace.
    """
    return get_object_or_404(
        ProjectUpdate.objects.select_related("project__workspace", "author").filter(
            project__workspace__memberships__user=user,
        ),
        pk=pk,
    )


def _can_modify_update(user, update):
    """Return True if ``user`` may edit/delete ``update`` (author or ws admin)."""
    return update.author_id == user.id or _is_workspace_admin(user.id, update.project.workspace_id)


def _render_overview_update_card(request, update):
    """Render one overview update card, fully decorated (reactions + thread)."""
    user_id = request.user.id
    _attach_update_reactions([update], user_id)
    _attach_update_thread_reactions(update, user_id)
    update.can_modify = _can_modify_update(request.user, update)
    return render_to_string(
        "web/projects/_overview_update_card.html",
        {
            "update": update,
            "health_labels": dict(ProjectUpdate.HEALTH_CHOICES),
        },
        request=request,
    )


@login_required
def update_edit_form(request, pk):
    """Render the in-place edit composer (health chips + TipTap) for an update.

    Loaded via ``hx-get`` into the overview card's ``#update-edit-<pk>``
    slot; the editor mounts on ``htmx:afterSwap``, pre-filled from the
    update body.

    Returns:
        Rendered ``_update_edit_form.html``, 403 if not permitted, or 404
        for a foreign / missing update.
    """
    update = _get_user_update_or_404(request.user, pk)
    if not _can_modify_update(request.user, update):
        raise PermissionDenied()
    return HttpResponse(
        render_to_string(
            "web/projects/_update_edit_form.html",
            {
                "update": update,
                "health_labels": dict(ProjectUpdate.HEALTH_CHOICES),
            },
            request=request,
        ),
    )


@login_required
def update_card_fragment(request, pk):
    """Re-render one overview update card (used by the edit Cancel)."""
    update = _get_user_update_or_404(request.user, pk)
    return HttpResponse(_render_overview_update_card(request, update))


@require_POST
@login_required
def edit_project_update(request, pk):
    """Save an edit to a project update; return its refreshed overview card.

    Allowed for the author or a workspace admin/owner. Reads ``health`` +
    ``body``; ``updated_at`` bumps so the card shows ``(edited)``. Updates
    are not on the activity log (ADR 0009), so no event is emitted.

    Returns:
        The refreshed ``_overview_update_card.html``, 400 on bad health /
        empty body, 403 if not permitted, or 404 for a foreign update.
    """
    update = _get_user_update_or_404(request.user, pk)
    if not _can_modify_update(request.user, update):
        raise PermissionDenied()
    health = (request.POST.get("health") or "").strip()
    if health not in {key for key, _ in ProjectUpdate.HEALTH_CHOICES}:
        return HttpResponseBadRequest("invalid health")
    body = (request.POST.get("body") or "").strip()
    if not body:
        return HttpResponseBadRequest("body required")
    update.health = health
    update.body = body
    update.save(update_fields=["health", "body", "updated_at"])
    return HttpResponse(_render_overview_update_card(request, update))


@require_POST
@login_required
def delete_project_update(request, pk):
    """Delete a project update and re-render the overview latest-update slot.

    Allowed for the author or a workspace admin/owner. Comments cascade.
    The overview shows only the latest update, so the response is the
    re-rendered ``#overview-updates-list`` (the new latest card, or empty)
    plus an ``HX-Trigger`` flipping the panel's ``hasUpdates`` so the
    empty-state shows when the last update is gone.

    Returns:
        The re-rendered list fragment, 403 if not permitted, or 404 for a
        foreign / missing update.
    """
    update = _get_user_update_or_404(request.user, pk)
    if not _can_modify_update(request.user, update):
        raise PermissionDenied()
    project = update.project
    update.delete()
    latest = list(project.updates.select_related("author").order_by("-created_at")[:1])
    html = ""
    if latest:
        html = _render_overview_update_card(request, latest[0])
    response = HttpResponse(html)
    response["HX-Trigger"] = json.dumps({"updates-changed": {"hasUpdates": bool(latest)}})
    return response


@require_POST
@login_required
def toggle_project_member(request, slug_prefix):
    """Toggle a single user in / out of the project's members M2M.

    Accepts an integer ``user_id`` form field. The user must be a
    member of the project's workspace; non-member ids 400. Returns
    the rendered members chip list fragment.
    """
    project = _get_user_project_or_404(request.user, slug_prefix)
    raw = (request.POST.get("user_id") or "").strip()
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        return HttpResponseBadRequest("invalid user_id")
    user = User.objects.filter(
        workspace_memberships__workspace=project.workspace,
        id=user_id,
    ).first()
    if user is None:
        return HttpResponseBadRequest("user not a workspace member")
    if project.members.filter(pk=user.pk).exists():
        project.members.remove(user)
    else:
        project.members.add(user)
    members = list(project.members.order_by("first_name", "last_name", "username"))
    return HttpResponse(
        render_to_string(
            "web/projects/_overview_members.html",
            {
                "project": project,
                "members": members,
                "workspace_members": _project_workspace_members(project, exclude_user=None),
            },
            request=request,
        ),
    )


def _project_workspace_members(project, *, exclude_user):
    """Return workspace members eligible to be project lead / members.

    Args:
        project: The :class:`Project` providing the workspace scope.
        exclude_user: Optionally drop this user from the result (used
            when the picker is for "add others").

    Returns:
        Ordered list of :class:`User`.
    """
    qs = project.workspace.members.order_by("first_name", "last_name", "username")
    if exclude_user is not None:
        qs = qs.exclude(pk=exclude_user.pk)
    return list(qs)


@require_POST
@login_required
def archive_task(request, slug_prefix, number):
    """Archive (or unarchive) a task — orthogonal to status.

    Sets ``Task.archived_at`` to ``now()`` to archive, or ``None`` to
    unarchive. The task's status is untouched so unarchive restores
    the prior state. The activity event (``task.archived`` /
    ``task.unarchived``) and SSE broadcast both ride the standard diff
    path so the kanban card refresh is consistent with every other
    inline edit — see :func:`apps.tasks.events.emit_task_diff_events`.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    unarchive = request.POST.get("unarchive") == "1"
    with transaction.atomic():
        if unarchive and task.archived_at is None:
            return HttpResponseBadRequest("task is not archived")
        if not unarchive and task.archived_at is not None:
            return HttpResponseBadRequest("task is already archived")
        old_state = snapshot_task(task)
        task.archived_at = None if unarchive else timezone.now()
        task.save(update_fields=["archived_at", "updated_at"])
        emit_task_diff_events(old_state=old_state, task=task, actor=request.user)
    return _inline_edit_response(
        request,
        task,
        "web/projects/_task_meta.html",
        {
            "task": task,
            "workspace_members": _workspace_members(task),
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
            "workspace_labels": _workspace_labels(task),
            "workspace_label_groups": _workspace_label_groups(task),
            "workspace_projects": _workspace_projects(task),
            "workspace_cycles": _workspace_cycles(task.project.workspace),
            "attached_label_ids": set(task.labels.values_list("id", flat=True)),
        },
    )


@require_POST
@login_required
def cancel_task(request, slug_prefix, number):
    """Cancel a task (terminal "won't do") or reopen a cancelled one.

    A discoverable one-click affordance over the status picker: cancel
    sets ``status`` to ``cancelled``; reopen (``reopen=1``) sets it back
    to ``to-do``. The change rides the standard diff path
    (:func:`emit_task_diff_events`), so it emits the same
    ``task.status_changed`` event, SSE broadcast, and notifications as
    picking the status manually — no new status semantics. Re-renders
    the whole rail so the Cancel/Reopen button flips alongside the
    status cell.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    reopen = request.POST.get("reopen") == "1"
    if reopen and task.status != Task.STATUS_CANCELLED:
        return HttpResponseBadRequest("task is not cancelled")
    if not reopen and task.status == Task.STATUS_CANCELLED:
        return HttpResponseBadRequest("task is already cancelled")
    new_status = Task.STATUS_TODO if reopen else Task.STATUS_CANCELLED
    _apply_task_field_change(task, "status", new_status, request.user)
    return _inline_edit_response(
        request,
        task,
        "web/projects/_task_meta.html",
        {
            "task": task,
            "workspace_members": _workspace_members(task),
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
            "workspace_labels": _workspace_labels(task),
            "workspace_label_groups": _workspace_label_groups(task),
            "workspace_projects": _workspace_projects(task),
            "workspace_cycles": _workspace_cycles(task.project.workspace),
            "attached_label_ids": set(task.labels.values_list("id", flat=True)),
        },
    )


@require_POST
@login_required
def delete_task(request, slug_prefix, number):
    """Hard-delete a task and its subtasks, emitting ``task.deleted``.

    Mirrors the bulk-delete path (``apps.tasks.bulk._run_bulk_delete``):
    one ``task.deleted`` activity row per removed task (parent +
    cascaded subtasks), each carrying a snapshot so the timeline / audit
    survives the row deletion, then a single SSE broadcast with no
    ``card_html`` so connected boards drop the matching cards.

    The acting client's own board refetches via the ``acta:task-changed``
    event the context menu fires after this returns (SSE self-events are
    filtered out for the originator). Used by the right-click context
    menu; irreversible, so the menu gates it behind an inline confirm.

    Returns:
        ``204 No Content`` — the caller removes the row / refetches.
    """
    task = get_object_or_404(
        _user_task_qs(request.user).prefetch_related("subtasks"),
        project__slug_prefix=slug_prefix,
        number=number,
    )
    with transaction.atomic():
        workspace = task.project.workspace
        project = task.project
        targets = [task, *task.subtasks.all()]
        events = [
            ActivityLog(
                workspace=workspace,
                project=project,
                actor=request.user,
                event_type="task.deleted",
                target_type=ActivityLog.TARGET_TASK,
                target_id=t.id,
                payload={
                    "snapshot": {
                        "title": t.title,
                        "project_id": t.project_id,
                        "number": t.number,
                        "status": t.status,
                    },
                },
            )
            for t in targets
        ]
        # Cascade delete (parent FK is on_delete=CASCADE) removes the
        # subtasks; activity rows survive since target_id is a plain int.
        Task.objects.filter(pk=task.pk).delete()
        ActivityLog.objects.bulk_create(events)
        broadcast_task_events(events, {}, request.user)
    return HttpResponse(status=204)


@login_required
def task_context_menu(request, slug_prefix, number):
    """Render the right-click context menu fragment for one task.

    Server-renders the whole menu (with every submenu's options
    pre-populated — statuses, priorities, the workspace's members /
    projects / labels) so the client just positions it at the cursor.
    Each action posts to the existing ``set_task_*`` / ``cancel_task`` /
    ``archive_task`` / ``delete_task`` endpoints; the menu fires
    ``acta:task-changed`` afterwards so the board refetches.
    """
    task = get_object_or_404(
        _user_task_qs(request.user).select_related("reporter").prefetch_related("labels"),
        project__slug_prefix=slug_prefix,
        number=number,
    )
    return HttpResponse(
        render_to_string(
            "web/projects/_task_context_menu.html",
            {
                "task": task,
                "status_labels": Task.STATUS_LABELS,
                "priority_labels": dict(Task.PRIORITY_CHOICES),
                "size_values": Task.SIZE_VALUES,
                "workspace_members": _workspace_members(task),
                "workspace_projects": _workspace_projects(task),
                "workspace_cycles": _workspace_cycles(task.project.workspace),
                "workspace_labels": _workspace_labels(task),
                "workspace_label_groups": _workspace_label_groups(task),
                "attached_label_ids": set(task.labels.values_list("id", flat=True)),
            },
            request=request,
        ),
    )


@login_required
def bulk_context_menu(request):
    """Render the bulk context-menu fragment for the current selection.

    Selection-aware right-click (and the bulk bar's Actions button) open
    this when 2+ tasks are selected. Unlike the per-task menu it isn't
    scoped to one task — its pickers come from the **active workspace**
    (members / projects / labels), and its actions post to the bulk
    endpoint (``actaBulkPatch`` / ``actaBulkDelete``) against the ids in
    the client-side selection store. The "N tasks" count is filled in
    client-side from that store.
    """
    workspace = resolve_active_workspace(request)
    members, projects, labels = [], [], []
    cycles = []
    if workspace:
        members = list(
            WorkspaceMember.objects.filter(workspace=workspace).select_related("user").order_by("user__username"),
        )
        projects = list(Project.objects.filter(workspace=workspace).order_by("name"))
        labels = list(Label.objects.filter(workspace=workspace).order_by("position", "name"))
        label_groups_ctx = grouped_labels(workspace)
        cycles = _workspace_cycles(workspace)
    return HttpResponse(
        render_to_string(
            "web/projects/_bulk_context_menu.html",
            {
                "status_labels": Task.STATUS_LABELS,
                "priority_labels": dict(Task.PRIORITY_CHOICES),
                "size_values": Task.SIZE_VALUES,
                "workspace_members": members,
                "workspace_projects": projects,
                "workspace_labels": labels,
                "workspace_label_groups": label_groups_ctx,
                "workspace_cycles": cycles,
            },
            request=request,
        ),
    )


@require_POST
@login_required
def post_comment(request, slug_prefix, number):
    """Create a comment (or one-level reply) on the task.

    Reads a Markdown ``body`` and an optional ``parent`` (a top-level
    comment id on the same task). Top-level comments use the inline-edit
    response (new card appended to the timeline + OOB timeline refresh);
    replies append surgically into the parent card's reply list, leaving
    other in-progress inputs untouched. Either way a ``comment.created``
    activity event fires and :func:`notify_comment_created` fans out —
    a reply notifies exactly like a top-level comment, plus the parent
    comment's author.

    Args:
        request: Django request carrying ``body`` + optional ``parent``.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_comment.html`` (top-level, via the inline-edit
        response) or ``_comment_reply.html`` (reply), 400 on an empty
        body / bad parent, or 404 for a foreign / missing task.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    body = (request.POST.get("body") or "").strip()
    files = request.FILES.getlist("file")
    if not body and not files:
        return HttpResponseBadRequest("body or file required")
    parent = None
    parent_raw = (request.GET.get("parent") or request.POST.get("parent") or "").strip()
    if parent_raw:
        if not parent_raw.isdigit():
            return HttpResponseBadRequest("invalid parent")
        parent = task.comments.filter(parent__isnull=True, pk=int(parent_raw)).first()
        if parent is None:
            return HttpResponseBadRequest("invalid parent")
    # Validate every file up front so a bad upload never leaves a
    # half-created comment or an orphaned blob on disk.
    try:
        for upload in files:
            categorize(upload)
    except ValidationError as exc:
        return HttpResponseBadRequest("; ".join(exc.messages))
    with transaction.atomic():
        comment = Comment.objects.create(task=task, author=request.user, parent=parent, body=body)
        for upload in files:
            create_comment_attachment(comment=comment, uploader=request.user, uploaded_file=upload)
        log_event(
            workspace=task.project.workspace,
            project=task.project,
            actor=request.user,
            event_type="comment.created",
            target_type=ActivityLog.TARGET_COMMENT,
            target_id=comment.id,
            payload={"task_id": task.id, "body_preview": body[:120]},
        )
    notify_comment_created(comment=comment, actor=request.user)
    comment.reaction_summary = []
    comment.can_modify = True
    if parent is not None:
        return HttpResponse(render_to_string("web/projects/_comment_reply.html", {"comment": comment}, request=request))
    return _inline_edit_response(
        request,
        task,
        "web/projects/_comment.html",
        {"comment": comment},
    )


@login_required
def comment_reply_form(request, slug_prefix, number, comment_id):
    """Render the lazy TipTap reply composer for one task comment.

    Loaded on demand via ``hx-get`` when the user clicks "Reply", so the
    page doesn't carry a live TipTap editor per comment (they're heavy).
    The returned fragment is mounted by the editor bundle's
    ``htmx:afterSwap`` hook.

    Args:
        request: The current request.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.
        comment_id: PK of the top-level comment being replied to.

    Returns:
        Rendered ``_comment_reply_form.html``, or 404 for a foreign /
        missing task or a non-top-level / foreign parent comment.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    parent = get_object_or_404(task.comments, parent__isnull=True, pk=comment_id)
    return HttpResponse(
        render_to_string(
            "web/projects/_comment_reply_form.html",
            {"task": task, "parent": parent},
            request=request,
        ),
    )


@login_required
def comment_fragment(request, comment_id):
    """Re-render one comment's card (the edit Cancel path), either owner."""
    comment = _get_user_comment_or_404(request.user, comment_id)
    return _render_any_comment_card(request, comment)


@login_required
def comment_edit_form(request, comment_id):
    """Render the inline TipTap edit composer for one comment (either owner).

    Loaded via ``hx-get`` when the author / a workspace admin clicks Edit;
    it replaces the comment's body region in place. Owner-aware mention +
    image-upload endpoints and the card id are passed to the template.

    Returns:
        Rendered ``_comment_edit_form.html``, 403 if not permitted, or 404
        for a foreign / missing comment.
    """
    comment = _get_user_comment_or_404(request.user, comment_id)
    if not _can_modify_any_comment(request.user, comment):
        raise PermissionDenied()
    _workspace, project, kind = _comment_owner(comment)
    mention_url = reverse("web:mention_search", kwargs={"slug_prefix": project.slug_prefix})
    if kind == "task":
        image_url = reverse(
            "web:upload_task_inline_image",
            kwargs={"slug_prefix": project.slug_prefix, "number": comment.task.number},
        )
        card_id = f"comment-{comment.id}"
    else:
        image_url = reverse("web:upload_project_inline_image", kwargs={"slug_prefix": project.slug_prefix})
        card_id = f"update-comment-{comment.id}"
    return HttpResponse(
        render_to_string(
            "web/projects/_comment_edit_form.html",
            {"comment": comment, "mention_url": mention_url, "image_url": image_url, "card_id": card_id},
            request=request,
        ),
    )


@require_POST
@login_required
def edit_comment(request, comment_id):
    """Save an edit to a comment and return its refreshed card (either owner).

    Allowed for the author or a workspace admin/owner. Task comments emit a
    ``comment.edited`` activity event (kept in the audit log, filtered from
    the visible timeline); project-update comments don't (updates are off
    the activity log). Body required.

    Returns:
        The refreshed comment card, 400 on empty body, 403 if not
        permitted, or 404 for a foreign / missing comment.
    """
    comment = _get_user_comment_or_404(request.user, comment_id)
    if not _can_modify_any_comment(request.user, comment):
        raise PermissionDenied()
    body = (request.POST.get("body") or "").strip()
    if not body:
        return HttpResponseBadRequest("body required")
    comment.body = body
    comment.save(update_fields=["body", "updated_at"])
    workspace, project, kind = _comment_owner(comment)
    if kind == "task":
        log_event(
            workspace=workspace,
            project=project,
            actor=request.user,
            event_type="comment.edited",
            target_type=ActivityLog.TARGET_COMMENT,
            target_id=comment.id,
            payload={"task_id": comment.task_id},
        )
    return _render_any_comment_card(request, comment)


@require_POST
@login_required
def delete_comment(request, comment_id):
    """Delete a comment (either owner); allowed for the author or a ws admin.

    Task comments emit ``comment.deleted`` and return the OOB timeline
    refresh (the card vanishes from the re-rendered timeline + a deleted
    marker appears). Project-update comments are off the activity log, so
    deletion just removes the card (an empty response swapped into it).
    Replies cascade with their parent.

    Returns:
        The OOB timeline fragment (task) or an empty response (update),
        403 if not permitted, or 404 for a foreign / missing comment.
    """
    comment = _get_user_comment_or_404(request.user, comment_id)
    if not _can_modify_any_comment(request.user, comment):
        raise PermissionDenied()
    workspace, project, kind = _comment_owner(comment)
    if kind == "task":
        task = comment.task
        deleted_id = comment.id
        # Keep the original post time so the timeline can slot the "deleted
        # a comment" marker where the comment used to sit, not at "now".
        original_created_at = comment.created_at.isoformat()
        comment.delete()
        log_event(
            workspace=workspace,
            project=project,
            actor=request.user,
            event_type="comment.deleted",
            target_type=ActivityLog.TARGET_COMMENT,
            target_id=deleted_id,
            payload={"task_id": task.id, "comment_created_at": original_created_at},
        )
        return HttpResponse(
            render_to_string(
                "web/projects/_activity_oob.html",
                {
                    "task": task,
                    "timeline": _build_timeline(task, request.user.id),
                    "status_labels": Task.STATUS_LABELS,
                    "priority_labels": dict(Task.PRIORITY_CHOICES),
                },
                request=request,
            ),
        )
    comment.delete()
    return HttpResponse("")


@require_POST
@login_required
def post_update_comment(request, pk):
    """Create a comment (or one-level reply) on a project update.

    Reads a Markdown ``body`` and an optional ``parent`` (a top-level
    comment id). Returns just the new node so HTMX appends it without
    disturbing other in-progress inputs: a whole thread card for a
    top-level comment, or a single reply block for a reply. Posted from
    both the inbox Updates preview and the project overview.

    Args:
        request: Django request carrying ``body`` + optional ``parent``.
        pk: Project update primary key.

    Returns:
        Rendered ``_update_comment.html`` (new card) or
        ``_update_comment_reply.html`` (new reply), 400 on an empty body
        / bad parent, or 404 for a foreign / missing update.
    """
    update = get_object_or_404(
        ProjectUpdate.objects.filter(project__workspace__memberships__user=request.user).select_related("project"),
        pk=pk,
    )
    body = (request.POST.get("body") or "").strip()
    if not body:
        return HttpResponseBadRequest("body required")
    parent = None
    parent_raw = (request.GET.get("parent") or request.POST.get("parent") or "").strip()
    if parent_raw:
        if not parent_raw.isdigit():
            return HttpResponseBadRequest("invalid parent")
        parent = Comment.objects.filter(project_update=update, parent__isnull=True, pk=int(parent_raw)).first()
        if parent is None:
            return HttpResponseBadRequest("invalid parent")
    comment = Comment.objects.create(project_update=update, author=request.user, parent=parent, body=body)
    comment.reaction_summary = []
    comment.can_modify = True
    template = "web/projects/_update_comment_reply.html" if parent else "web/projects/_update_comment.html"
    return HttpResponse(render_to_string(template, {"comment": comment}, request=request))


def _get_reaction_target_or_404(user, target_type, target_id):
    """Resolve a reaction target, scoped to the user's workspaces.

    Args:
        user: The acting :class:`User`.
        target_type: One of ``task`` / ``comment`` / ``update``.
        target_id: Primary key of the target.

    Returns:
        The target model instance — a :class:`Task`, :class:`Comment`, or
        :class:`ProjectUpdate` the user can see.

    Raises:
        Http404: If the target is missing or lives in another workspace.
    """
    if target_type == "task":
        qs = Task.objects.filter(project__workspace__memberships__user=user)
    elif target_type == "comment":
        qs = Comment.objects.filter(
            Q(task__project__workspace__memberships__user=user)
            | Q(project_update__project__workspace__memberships__user=user),
        )
    else:
        qs = ProjectUpdate.objects.filter(project__workspace__memberships__user=user)
    return get_object_or_404(qs, pk=target_id)


@require_POST
@login_required
def toggle_reaction_view(request, target_type, target_id):
    """Toggle the current user's emoji reaction on a task / comment / update.

    Reads an ``emoji`` form field and flips the reaction (add if absent,
    remove if present). Returns the freshly rendered reaction bar for the
    target so HTMX can swap it in place.

    Args:
        request: Django request carrying the ``emoji`` form field.
        target_type: One of ``task`` / ``comment`` / ``update``.
        target_id: Primary key of the target.

    Returns:
        Rendered ``web/_reaction_bar.html`` for the target, 400 on an
        unknown target type or empty / oversized emoji, or 404 for a
        foreign / missing target.
    """
    target_field = TARGET_TYPES.get(target_type)
    if target_field is None:
        return HttpResponseBadRequest("invalid target type")
    emoji = (request.POST.get("emoji") or "").strip()
    if not emoji or len(emoji) > 64:
        return HttpResponseBadRequest("invalid emoji")
    target = _get_reaction_target_or_404(request.user, target_type, target_id)
    toggle_reaction(user=request.user, target_field=target_field, target=target, emoji=emoji)
    reactions = summarize_reactions(
        target_field=target_field,
        ids=[target.id],
        user_id=request.user.id,
    ).get(target.id, [])
    return HttpResponse(
        render_to_string(
            "web/_reaction_bar.html",
            {
                "target_type": target_type,
                "target_id": target.id,
                "reactions": reactions,
                "can_react": True,
            },
            request=request,
        ),
    )


def _attach_update_reactions(updates, user_id):
    """Attach update-level ``reaction_summary`` to each project update.

    One query for the whole batch. Use for any list of updates whose own
    reaction bar will render (the inbox preview card, the overview card).

    Args:
        updates: Iterable of :class:`ProjectUpdate`.
        user_id: The viewer's id (highlights their own reactions).

    Returns:
        The materialized list of updates, each carrying ``reaction_summary``.
    """
    return attach_reactions(objs=updates, target_field="project_update", user_id=user_id)


def _attach_update_thread_reactions(update, user_id):
    """Decorate one update's comment thread with reactions + ``can_modify``.

    Attaches ``reaction_summary`` (one reaction query for the whole thread)
    and ``can_modify`` (author or workspace admin — drives the edit/delete
    affordances) to every top-level comment and reply the thread renders.
    Relies on ``ProjectUpdate.top_level_comments`` being cached +
    reply-prefetched so the decorated objects are the exact ones the
    template re-reads.

    Args:
        update: The :class:`ProjectUpdate` whose thread is being shown.
        user_id: The viewer's id.
    """
    comments = []
    for comment in update.top_level_comments:
        comments.append(comment)
        comments.extend(comment.replies.all())
    user_is_admin = _is_workspace_admin(user_id, update.project.workspace_id)
    for comment in comments:
        comment.can_modify = user_is_admin or comment.author_id == user_id
    attach_reactions(objs=comments, target_field="comment", user_id=user_id)


def _user_accessible_projects(user, workspace=None):
    """Return projects the user can post tasks to, with workspace eager-loaded.

    When ``workspace`` is given, scoped to it — the active-workspace
    boundary (the sidebar switcher): once a workspace is active, project
    pickers and the create-task path must never offer projects from another
    workspace. ``None`` spans all the user's workspaces (used only when
    there's no active workspace to scope to).

    Args:
        user: The acting :class:`User`.
        workspace: Optional :class:`Workspace` to scope to.

    Returns:
        A queryset of :class:`Project` rows ordered by workspace name,
        then project name. Empty workspaces don't surface (no projects).
    """
    qs = Project.objects.filter(workspace__memberships__user=user)
    if workspace is not None:
        qs = qs.filter(workspace=workspace)
    return qs.select_related("workspace").order_by("workspace__name", "name").distinct()


def _project_members_qs(project):
    """Members of ``project``'s workspace ordered by display name.

    Args:
        project: The :class:`Project` whose workspace members to fetch.

    Returns:
        A queryset of :class:`User` rows.
    """
    return project.workspace.members.order_by("first_name", "last_name", "username")


def _project_labels_qs(project):
    """Labels available in ``project``'s workspace (picker order: ``position``).

    Args:
        project: The :class:`Project` whose workspace labels to fetch.

    Returns:
        A queryset of :class:`Label` rows ordered by ``position`` then ``name``
        — the same order the management UI uses.
    """
    return Label.objects.filter(workspace=project.workspace).order_by("position", "name")


@login_required
def create_task(request):
    """Render the create-task modal (GET) or persist the new task (POST).

    GET: returns the ``_create_task_modal.html`` fragment, pre-filled
    from optional querystring args (``?project=<slug_prefix>`` to
    lock the project picker, ``?status=<value>`` to pre-pick a status —
    used by the per-kanban-column ``+`` button so the new task lands in
    the column the user clicked from).

    POST: validates the form, creates the :class:`Task`, and returns a
    302 redirect to its detail page (HTMX honours the ``HX-Redirect``
    response header). Each form field is gated to values the user has
    permission to pick — project must be in a workspace the user is a
    member of, assignee must be a member of that workspace, labels must
    belong to that workspace too. Activity events ride the standard
    ``emit_task_diff_events`` path (kanban cards refresh via SSE).
    """
    if request.method == "POST":
        return _create_task_post(request)
    return _create_task_get(request)


def _create_task_get(request):
    """Render the modal form for a fresh task.

    Pre-fills:

    * ``project`` — from ``?project=<slug>`` if set, else the first
      accessible project (alphabetically per workspace then name).
    * ``status`` — from ``?status=<value>`` if a valid status; else
      ``planned`` (backlog). Kanban's per-column ``+`` always passes
      ``?status=`` so the new task lands in the column the user clicked.
    * ``assignee`` — the current user when they're a member of the
      selected project's workspace; ``None`` otherwise (the form
      offers an explicit "Unassigned" option to clear it).

    Args:
        request: The active ``HttpRequest`` carrying optional ``project``
            and ``status`` querystring keys.

    Returns:
        Rendered HTML of the modal partial.
    """
    projects = list(_user_accessible_projects(request.user, resolve_active_workspace(request)))
    requested_slug = request.GET.get("project") or ""
    selected_project = None
    for project in projects:
        if project.slug_prefix == requested_slug:
            selected_project = project
            break
    if selected_project is None and projects:
        selected_project = projects[0]
    members = list(_project_members_qs(selected_project)) if selected_project else []
    labels = list(_project_labels_qs(selected_project)) if selected_project else []
    label_groups = grouped_labels(selected_project.workspace) if selected_project else []
    pre_status = request.GET.get("status") or Task.STATUS_PLANNED
    if pre_status not in Task.STATUS_VALUES:
        pre_status = Task.STATUS_PLANNED
    # Preserve user-entered fields across project-change re-renders. The
    # project ``<select>`` fires ``hx-get`` with ``hx-include="closest
    # form"`` so every typed value lands here as a querystring param;
    # we feed it back into the template as ``pre_*`` so the input keeps
    # its value when the modal HTML returns. Assignee / labels reset
    # when they're not members of the newly-picked workspace — those
    # values would 400 on submit otherwise.
    pre_title = request.GET.get("title") or ""
    pre_description = request.GET.get("description") or ""
    try:
        pre_priority = int(request.GET.get("priority") or 0)
    except (TypeError, ValueError):
        pre_priority = 0
    pre_due_date = request.GET.get("due_date") or ""
    requested_assignee = request.GET.get("assignee") or ""
    pre_assignee_id = None
    try:
        requested_assignee_id = int(requested_assignee)
    except (TypeError, ValueError):
        requested_assignee_id = None
    if requested_assignee_id and any(m.pk == requested_assignee_id for m in members):
        pre_assignee_id = requested_assignee_id
    elif not requested_assignee and selected_project and any(m.pk == request.user.pk for m in members):
        # First open (no assignee querystring) — default to "me" when
        # the user is a member of the selected workspace.
        pre_assignee_id = request.user.pk
    requested_label_ids = set()
    for raw in request.GET.getlist("labels"):
        try:
            requested_label_ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    pre_label_ids = {label.id for label in labels if label.id in requested_label_ids}
    # ``link_related`` (a PREFIX-NUMBER slug) opts the new task into being
    # linked as a related task to an origin — set by "Create task from
    # comment" / "Create task from selection". Resolve it now so the modal
    # can show which task it'll link to (and skip silently if it doesn't
    # resolve to a task the user can see).
    link_related_task = _resolve_link_target(request.user, request.GET.get("link_related") or "")
    return HttpResponse(
        render_to_string(
            "web/_create_task_modal.html",
            {
                "projects": projects,
                "selected_project": selected_project,
                "members": members,
                "labels": labels,
                "label_groups": label_groups,
                "pre_status": pre_status,
                "pre_priority": pre_priority,
                "pre_assignee_id": pre_assignee_id,
                "pre_title": pre_title,
                "pre_description": pre_description,
                "pre_due_date": pre_due_date,
                "pre_label_ids": pre_label_ids,
                "link_related_task": link_related_task,
                "status_labels": Task.STATUS_LABELS,
                "priority_labels": dict(Task.PRIORITY_CHOICES),
            },
            request=request,
        ),
    )


def _create_task_post(request):
    """Persist a new task and tell HTMX to navigate to its detail page.

    All access checks: the project must belong to a workspace the user
    is a member of, the assignee (if set) must be a member of that
    workspace, and any labels must live in that workspace. Anything
    else returns ``400`` — the modal stays open, the user can fix the
    field. The activity log gets a ``task.created`` event with
    ``actor=request.user``; per-watched-field diff events do not fire
    because there is no prior state.

    Args:
        request: ``HttpRequest`` whose POST body carries the form.

    Returns:
        ``204 No Content`` with an ``HX-Redirect`` header pointing at
        the new task's detail page on success; ``400`` on validation
        failure.
    """
    project_slug = (request.POST.get("project") or "").strip()
    title = (request.POST.get("title") or "").strip()
    if not project_slug:
        return HttpResponseBadRequest("project required")
    if not title:
        return HttpResponseBadRequest("title required")
    if len(title) > 200:
        return HttpResponseBadRequest("title too long")
    project = get_object_or_404(
        _user_accessible_projects(request.user, resolve_active_workspace(request)),
        slug_prefix=project_slug,
    )
    description = request.POST.get("description") or ""
    status = request.POST.get("status") or Task.STATUS_PLANNED
    if status not in Task.STATUS_VALUES:
        return HttpResponseBadRequest("invalid status")
    raw_priority = request.POST.get("priority") or str(Task.NO_PRIORITY)
    try:
        priority = int(raw_priority)
    except (TypeError, ValueError):
        return HttpResponseBadRequest("invalid priority")
    if priority not in {p[0] for p in Task.PRIORITY_CHOICES}:
        return HttpResponseBadRequest("invalid priority")
    due_date_raw = (request.POST.get("due_date") or "").strip()
    due_date = None
    if due_date_raw:
        try:
            due_date = datetime.date.fromisoformat(due_date_raw)
        except ValueError:
            return HttpResponseBadRequest("invalid due_date")
    assignee = None
    assignee_id_raw = request.POST.get("assignee") or ""
    if assignee_id_raw:
        try:
            assignee_id = int(assignee_id_raw)
        except ValueError:
            return HttpResponseBadRequest("invalid assignee")
        # ``filter(...).first()`` returns None when the user is not in
        # the project workspace — we treat that as a 400 rather than a
        # 404 because the form sent a malformed value, not a missing
        # resource.
        assignee = (
            User.objects.filter(
                pk=assignee_id,
                workspace_memberships__workspace=project.workspace,
            )
            .distinct()
            .first()
        )
        if assignee is None:
            return HttpResponseBadRequest("assignee not in workspace")
    label_ids_raw = request.POST.getlist("labels")
    label_ids: list[int] = []
    for raw in label_ids_raw:
        try:
            label_ids.append(int(raw))
        except ValueError:
            return HttpResponseBadRequest("invalid label id")
    if label_ids:
        valid_ids = set(
            Label.objects.filter(id__in=label_ids, workspace=project.workspace).values_list("id", flat=True),
        )
        if valid_ids != set(label_ids):
            return HttpResponseBadRequest("labels not in workspace")
    with transaction.atomic():
        task = Task(
            project=project,
            title=title,
            description=description,
            status=status,
            priority=priority,
            due_date=due_date,
            assignee=assignee,
            reporter=request.user,
        )
        # Mirror ``set_task_status``: a task that's born in-progress gets its
        # start_date stamped now, so the timeline knows when it began (a task
        # created straight into in-progress never passes through the status
        # transition that would otherwise set it). A done-on-create task gets
        # its end_date stamped by ``Task.save`` → ``_sync_done_dates``.
        if status == Task.STATUS_IN_PROGRESS:
            task.start_date = timezone.localdate()
        task.save()
        if label_ids:
            # Drop duplicates within an exclusive group (form lets the user
            # tick more than one; only the first survives, see
            # ``trim_exclusive_conflicts``).
            task.labels.set(trim_exclusive_conflicts(label_ids))
        # Pre-render the kanban card once: feeds the local HX-Retarget swap
        # below AND rides the SSE broadcast (``broadcast_extras``) so peers
        # on the kanban view can live-insert it without a server round-trip.
        kanban_card_html = _render_kanban_card_html(task, request)
        log_event(
            workspace=project.workspace,
            project=project,
            actor=request.user,
            event_type="task.created",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"title": task.title, "status": task.status},
            broadcast_extras={"html_kanban": kanban_card_html},
        )
        notify_task_created(task=task, actor=request.user)
        # "Create task from comment / selection" passes ``link_related``;
        # link the fresh task to the origin as a related (symmetric) task,
        # recording the link on the origin's timeline. Skipped when the
        # slug doesn't resolve to a visible task or points at itself.
        origin = _resolve_link_target(request.user, (request.POST.get("link_related") or "").strip())
        linked = origin is not None and origin.pk != task.pk
        if linked:
            task.related.add(origin)
            broadcast_link_change(
                task=origin,
                target=task,
                event_type="task.link_added",
                payload={"kind": "related", "target_slug": task.slug, "target_title": task.title},
                actor=request.user,
            )
    detail_url = f"/projects/{project.slug_prefix}/{task.number}/"
    # ``acta:link-changed`` makes the origin's links panel + activity log
    # refetch live (they have no SSE subscription; the modal returns 204).
    # Fire it FIRST and via JSON: the modal closes on ``acta:task-created``
    # by emptying ``#modal-root`` (detaching the form HTMX dispatches these
    # events on), so anything after that no longer bubbles to ``body``.
    # JSON also avoids the ambiguous comma-separated parse.
    if linked:
        created_trigger = json.dumps({"acta:link-changed": True, "acta:task-created": True})
    else:
        created_trigger = "acta:task-created"
    response = HttpResponse(status=204)
    open_after = request.POST.get("open_after_create") == "1"
    if open_after:
        # Boosted client-side navigation (``HX-Location``) instead of a
        # full-page ``HX-Redirect`` — the new task opens by swapping
        # ``#app-content`` only, no reload / loader flash. ``acta:task-created``
        # closes the modal; the panel-refetch it also triggers on the
        # current page is harmless here since ``#app-content`` (panels
        # included) is being replaced wholesale by the swap.
        response["HX-Trigger"] = created_trigger
        response["HX-Location"] = json.dumps(
            {
                "path": detail_url,
                "target": "#app-content",
                "select": "#app-content",
                "swap": "outerHTML show:top",
                "headers": {"HX-Boosted": "true"},
            }
        )
    else:
        response = _task_card_insert_response(request, task, linked=linked, kanban_html=kanban_card_html)
    return response


def _render_kanban_card_html(task, request):
    """Render the kanban card fragment for one task (re-used by SSE peers)."""
    return render_to_string(
        "web/projects/_task_card.html",
        {
            "task": task,
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
            "today": timezone.localdate(),
        },
        request=request,
    )


def _render_table_row_html(task, request, *, show_project):
    """Render one ``<tr>`` for the table view (column count tracks ``show_project``)."""
    return render_to_string(
        "web/projects/_table_row.html",
        {
            "task": task,
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
            "today": timezone.localdate(),
            "show_labels": True,
            "show_project": show_project,
        },
        request=request,
    )


def _render_task_row_html(task, request):
    """Render the generic ``web/_task_row.html`` partial used by the list view."""
    return render_to_string(
        "web/_task_row.html",
        {
            "task": task,
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
            "today": timezone.localdate(),
        },
        request=request,
    )


def _compute_list_section_keys(task, request):
    """Return ``{axis: section_key}`` mapping ``task`` to its bucket per list axis.

    Used to drive client-side row insertion in the list view: for each axis the
    panel pre-renders, the JS handler looks up the matching ``[data-list-axis]``
    wrapper and the ``[data-section-key]`` ``<section>`` within it. Reuses
    :func:`apps.web.grouping.group_tasks` so the keying logic stays in one
    place — pass a single-task list and pick the only non-empty bucket.

    Args:
        task: The freshly-created :class:`Task`.
        request: HTTP request, used for the acting-user timezone in the
            deadline axis.

    Returns:
        Dict ``{axis: key}`` covering every axis in :data:`LIST_AXES` where a
        bucket exists for ``task``. ``key`` is always a string (matches the
        ``data-section-key`` attribute the template emits).
    """
    keys = {}
    for axis in LIST_AXES:
        sections = group_tasks([task], axis, request_user=request.user)
        for section in sections:
            if section["tasks"]:
                keys[axis] = str(section["key"])
                break
    return keys


def _current_view_from_htmx(request):
    """Return ``(view, show_project)`` derived from htmx's ``HX-Current-URL``.

    htmx sends ``HX-Current-URL`` with every request so the server knows the
    URL the click came from. We parse ``?view=`` to know which surface the
    new card needs to land on, and use the path to decide ``show_project``
    (AllTasks needs the project column, project-scoped pages don't).
    """
    current_url = (request.headers.get("HX-Current-URL") or "").strip()
    if not current_url:
        return "kanban", True
    try:
        parsed = urlparse(current_url)
    except ValueError:
        return "kanban", True
    qs = parse_qs(parsed.query)
    view = (qs.get("view") or ["kanban"])[0]
    show_project = not parsed.path.startswith("/projects/")
    return view, show_project


def _task_card_insert_response(request, task, *, linked, kanban_html):
    """Build the in-page create response that drops the new card into the active view.

    The form posts with ``hx-swap="none"``; the server overrides that via
    ``HX-Retarget`` + ``HX-Reswap`` and returns the rendered fragment as the
    body. htmx appends just that one element — no wrapper refetch, no
    cascade rebuild of the kanban. Active view comes from ``HX-Current-URL``:

    * ``kanban`` — kanban card into ``#kanban-col-<status>``.
    * ``table`` — table row into ``#task-table-body`` (``show_project`` flag
      tracks AllTasks vs ProjectDetail so the column count matches).
    * ``list`` — pre-renders the row HTML + per-axis section keys and fires
      ``acta:list-insert-row``; ``acta.js`` finds the matching
      ``[data-list-axis] section[data-section-key]`` wrapper and appends.
      No swap on the response itself (the panel pre-renders all axes, so a
      single ``HX-Retarget`` can't reach all of them) — JS does the work.
    * ``timeline`` / ``backlog`` — toast-only; gantt positioning and the
      backlog's own grouping aren't covered yet. Modal still closes.

    Why split the triggers: toast fires immediately, but the modal-close +
    link-changed events ride ``HX-Trigger-After-Settle`` so the form stays
    in the DOM until htmx finishes the swap — otherwise the indicator class
    never gets cleaned up (loader spins forever) and the swap can error out
    with the elt detached mid-request.
    """
    view, show_project = _current_view_from_htmx(request)
    body = ""
    retarget = None
    reswap = None
    list_insert = None
    if view == "table":
        body = _render_table_row_html(task, request, show_project=show_project)
        retarget = "#task-table-body"
        reswap = "beforeend"
    elif view == "list":
        list_insert = {
            "task_id": task.id,
            "row_html": _render_task_row_html(task, request),
            "section_keys": _compute_list_section_keys(task, request),
        }
    elif view in {"timeline", "backlog"}:
        pass  # toast-only — see docstring
    else:
        body = kanban_html
        retarget = f"#kanban-col-{task.status}"
        reswap = "beforeend"
    toast = {
        "message": str(_("Created %(slug)s") % {"slug": task.slug}),
        "level": "success",
    }
    after_settle = {"acta:task-created": True}
    if linked:
        after_settle["acta:link-changed"] = True
    immediate = {"acta:toast": toast}
    if list_insert is not None:
        immediate["acta:list-insert-row"] = list_insert
    if retarget:
        response = HttpResponse(body, status=200, content_type="text/html; charset=utf-8")
        response["HX-Retarget"] = retarget
        response["HX-Reswap"] = reswap
        response["HX-Trigger"] = json.dumps(immediate, default=str)
        # Modal-close + link-changed wait until after the swap so the form
        # stays in the DOM (indicator class gets cleaned up, swap doesn't
        # error on a detached elt).
        response["HX-Trigger-After-Settle"] = json.dumps(after_settle, default=str)
    else:
        # No swap → settle never fires → ``HX-Trigger-After-Settle`` would
        # silently drop. Send everything on immediate ``HX-Trigger`` instead;
        # there's no swap to wait for and nothing to detach the form from.
        response = HttpResponse(status=204)
        response["HX-Trigger"] = json.dumps({**immediate, **after_settle}, default=str)
    return response


# -----------------------------------------------------------------------------
# Workspace settings + member management
# -----------------------------------------------------------------------------


def _get_user_workspace_or_404(user, slug):
    """Return the workspace if ``user`` is a member, otherwise 404.

    Mirrors the per-project / per-task ``_get_user_*_or_404`` helpers.
    Non-members get a flat 404 rather than 403 so the workspace's
    existence is not leaked.

    Args:
        user: The current authenticated :class:`User`.
        slug: URL slug of the workspace.

    Returns:
        The :class:`Workspace` instance.

    Raises:
        Http404: When the workspace does not exist OR the user has no
            membership in it.
    """
    return get_object_or_404(
        Workspace.objects.filter(memberships__user=user),
        slug=slug,
    )


def _wip_context(workspace):
    """Resolve a workspace's WIP policy into ``(mode, limits, over_by_status)``.

    ``mode`` / ``limits`` come straight from :meth:`Workspace.wip_config`.
    For ``personal`` mode it also counts each member's **active**
    (non-archived, non-done/cancelled) tasks per limited status across
    the whole workspace and returns, per status, the ``{user_id: count}``
    of members who are over their per-person cap — one grouped query.

    Args:
        workspace: The active :class:`Workspace`, or ``None``.

    Returns:
        ``(mode, limits, over_by_status)``; ``over_by_status`` is empty
        outside personal mode.
    """
    if workspace is None:
        return Workspace.WIP_OFF, {}, {}
    mode, limits = workspace.wip_config()
    over_by_status: dict[str, dict[int, int]] = {}
    if mode == Workspace.WIP_PERSONAL and limits:
        rows = (
            Task.objects.filter(
                project__workspace=workspace,
                archived_at__isnull=True,
                assignee__isnull=False,
                status__in=list(limits.keys()),
            )
            .values("status", "assignee_id")
            .annotate(n=Count("id"))
        )
        for row in rows:
            cap = limits.get(row["status"])
            if cap and row["n"] > cap:
                over_by_status.setdefault(row["status"], {})[row["assignee_id"]] = row["n"]
    return mode, limits, over_by_status


def _build_kanban_columns(tasks, today=None, wip_mode=None, wip_limits=None, over_by_status=None, hide_statuses=None):
    """Build the kanban column dicts from an already-materialised task
    list — single pass over ``tasks`` bucketing by status and
    accumulating per-column substatus stats (``overdue_count``,
    ``active_avatars`` for the header avatar stack, ``done_this_week``
    for the Done column trend line).

    Iterates ``Task.KANBAN_STATUS_VALUES`` so the terminal
    ``cancelled`` status gets no column — cancelled cards drop off the
    board (the queryset already hides them, this is a belt-and-braces
    skip). All in-memory work, no DB hits. ``tasks`` is whatever the
    caller already paid for; this just shape-shifts it for the template.

    WIP limits come from the **workspace** policy (see
    :meth:`Workspace.wip_config`):

    * ``column`` mode — ``wip_limits[status]`` is a team cap on the
      column; the column gets ``limit`` / ``over_limit`` / ``fill_pct``
      for the ``N/limit`` fraction + capacity bar.
    * ``personal`` mode — ``over_by_status[status]`` maps the user ids
      who hold more than their per-person limit in that status
      (workspace-wide); the column flags how many of them appear here so
      their avatars can be ringed.

    Args:
        tasks: Materialised task list (one board's worth).
        today: Date anchor for overdue / done-this-week buckets.
        wip_mode: ``"column"`` / ``"personal"`` / ``None``.
        wip_limits: ``{status_key: max}`` for column mode.
        over_by_status: ``{status_key: {user_id: count}}`` for personal mode.
    """
    wip_limits = wip_limits or {}
    over_by_status = over_by_status or {}
    today = today or datetime.date.today()
    week_ago = today - datetime.timedelta(days=7)

    buckets: dict[str, list] = {status: [] for status in Task.KANBAN_STATUS_VALUES}
    overdue: dict[str, int] = {status: 0 for status in Task.KANBAN_STATUS_VALUES}
    avatars: dict[str, dict[int, "User"]] = {status: {} for status in Task.KANBAN_STATUS_VALUES}
    done_this_week = 0

    for t in tasks:
        status = t.status if t.status in buckets else None
        if status is None:
            continue
        buckets[status].append(t)
        if t.due_date and t.due_date < today and status != Task.STATUS_DONE:
            overdue[status] += 1
        col_avs = avatars[status]
        if t.assignee_id and t.assignee_id not in col_avs and len(col_avs) < 4:
            col_avs[t.assignee_id] = t.assignee
        if status == Task.STATUS_DONE and t.updated_at.date() >= week_ago:
            done_this_week += 1
        # Aging WIP: days the card has sat in its current column, from the
        # ``status_since`` annotation (last status change), for active
        # statuses only — a settled Done card isn't "aging". Drives the
        # card's left-edge age bar. ``None`` when not annotated / done.
        # ``status_since`` is annotated on the queryset only when the
        # caller paid for it: ``ProjectDetailView.get_context_data`` adds
        # the ``Subquery`` (last ``task.status_changed`` activity row),
        # while ``_user_task_qs`` (used by All Tasks / My Work) does not.
        # So the aging bar is project-scoped by construction; cross-
        # project kanban renders without it. Intentional — see
        # ``_task_card.html`` aging-WIP comment for the rationale.
        t.age_days = None
        since = getattr(t, "status_since", None)
        if status != Task.STATUS_DONE and since is not None:
            t.age_days = (today - since.date()).days

    def _limit_for(status):
        try:
            return int(wip_limits.get(status) or 0)
        except (TypeError, ValueError):
            return 0

    is_column = wip_mode == "column"
    is_personal = wip_mode == "personal"
    hide_statuses = hide_statuses or set()
    columns = []
    for status in Task.KANBAN_STATUS_VALUES:
        if status in hide_statuses:
            continue
        count = len(buckets[status])
        limit = _limit_for(status) if is_column else 0
        over_members = over_by_status.get(status, {}) if is_personal else {}
        columns.append(
            {
                "key": status,
                "label": Task.STATUS_LABELS[status],
                "tasks": buckets[status],
                "overdue_count": overdue[status],
                "active_avatars": list(avatars[status].values()),
                "done_this_week": done_this_week if status == Task.STATUS_DONE else 0,
                # Column-mode WIP (team cap on the column).
                "limit": limit,
                "over_limit": bool(limit) and count > limit,
                "at_limit": bool(limit) and count == limit,
                "fill_pct": min(100, round(count * 100 / limit)) if limit else 0,
                # Personal-mode WIP: {user_id: count} of members over their
                # per-person cap in this status (workspace-wide).
                "over_member_count": len(over_members),
                "over_members_map": over_members,
            }
        )
    return columns


def _workspace_member_or_none(user, workspace):
    """Return the :class:`WorkspaceMember` row for ``user`` in ``workspace``."""
    return WorkspaceMember.objects.filter(user=user, workspace=workspace).first()


def _user_is_workspace_admin(user, workspace):
    """True when the user is owner or admin of the workspace.

    Used by web views to gate mutations on the settings page. Mirrors
    ``IsWorkspaceAdmin`` from the DRF layer but in a plain-function
    shape so view code can branch on it.
    """
    m = _workspace_member_or_none(user, workspace)
    return m is not None and m.role in (WorkspaceMember.OWNER, WorkspaceMember.ADMIN)


def _user_is_workspace_owner(user, workspace):
    """True only when the user is the workspace owner.

    Owner-only actions (transfer ownership, delete workspace) gate on this,
    not :func:`_user_is_workspace_admin` — admins manage members/projects
    but can't hand off ownership or destroy the workspace (ADR 0010).
    """
    return workspace.owner_id == user.id


def _render_workspace_invites(workspace, *, viewer):
    """Build context for the invites panel — pending only, freshest first.

    Consumed invites stay in the DB for the audit trail but don't
    show up here; expired ones still surface so the admin sees them
    sitting in the list and either deletes them or mints a fresh one.
    """
    from django.utils import timezone

    invites = list(
        workspace.invites.filter(accepted_at__isnull=True).select_related("created_by").order_by("-created_at")
    )
    viewer_membership = _workspace_member_or_none(viewer, workspace)
    viewer_is_admin = viewer_membership is not None and viewer_membership.role in (
        WorkspaceMember.OWNER,
        WorkspaceMember.ADMIN,
    )
    return {
        "workspace": workspace,
        "pending_invites": invites,
        "invite_role_choices": [
            (WorkspaceMember.ADMIN, WorkspaceMember.ROLE_CHOICES[1][1]),
            (WorkspaceMember.MEMBER, WorkspaceMember.ROLE_CHOICES[2][1]),
        ],
        "now": timezone.now(),
        "viewer_is_admin": viewer_is_admin,
    }


def _invites_partial_response(request, workspace, *, toast=None):
    """Render just the invites panel — used after every mutation.

    Optional ``toast`` (``{"message": ..., "level": "success"|"error"}``)
    rides on a ``HX-Trigger: {"acta:toast": ...}`` response header so
    the client-side ``acta:toast`` listener can surface a confirmation
    or error message without us having to embed it in the partial.
    """
    response = HttpResponse(
        render_to_string(
            "web/workspaces/_settings_invites.html",
            _render_workspace_invites(workspace, viewer=request.user),
            request=request,
        ),
    )
    if toast is not None:
        import json

        response["HX-Trigger"] = json.dumps({"acta:toast": toast})
    return response


@require_POST
@login_required
def create_workspace_invite(request, slug):
    """Mint a workspace invite + send the email.

    Admin-only. Form fields: ``email`` (required) and ``role``
    (admin|member). Owner is never grantable through invite (the
    workspace already has one). Re-rendering the invites partial on
    success so HTMX swaps it in place.
    """
    from apps.workspaces.models import WorkspaceInvite
    from apps.workspaces.services import send_invite_email

    workspace = _get_user_workspace_or_404(request.user, slug)
    if not _user_is_workspace_admin(request.user, workspace):
        return HttpResponseBadRequest(_("Admin or owner only"))
    email = (request.POST.get("email") or "").strip()
    if not email or "@" not in email:
        return HttpResponseBadRequest(_("Email is required"))
    role = request.POST.get("role") or WorkspaceMember.MEMBER
    if role not in {WorkspaceMember.ADMIN, WorkspaceMember.MEMBER}:
        return HttpResponseBadRequest(_("Invalid role"))
    invite = WorkspaceInvite.generate(
        workspace=workspace,
        email=email,
        role=role,
        created_by=request.user,
    )
    sent = send_invite_email(invite, request=request)
    if sent:
        toast = {"message": _("Invite sent to %(email)s.") % {"email": email}, "level": "success"}
    else:
        toast = {
            "message": _("Invite created but email failed — copy the link from the list."),
            "level": "warning",
        }
    return _invites_partial_response(request, workspace, toast=toast)


@require_POST
@login_required
def revoke_workspace_invite(request, slug, invite_id):
    """Revoke (delete) a pending invite.

    Admin-only. A revoked invite stops working immediately — the
    landing view's ``WorkspaceInvite.DoesNotExist`` branch covers the
    case. Consumed invites can't reach this endpoint (the partial
    only renders ``accepted_at__isnull=True`` rows).
    """
    from apps.workspaces.models import WorkspaceInvite

    workspace = _get_user_workspace_or_404(request.user, slug)
    if not _user_is_workspace_admin(request.user, workspace):
        return HttpResponseBadRequest(_("Admin or owner only"))
    invite = WorkspaceInvite.objects.filter(workspace=workspace, pk=invite_id).first()
    if invite is None:
        return HttpResponseBadRequest(_("Invite not found"))
    revoked_email = invite.email
    invite.delete()
    return _invites_partial_response(
        request,
        workspace,
        toast={"message": _("Invite to %(email)s revoked.") % {"email": revoked_email}, "level": "success"},
    )


@require_POST
@login_required
def resend_workspace_invite(request, slug, invite_id):
    """Re-send the invite email without rotating the token.

    Useful when the recipient's mailbox bounced or they lost the
    original. Admin-only. The same partial swap as create/revoke
    keeps the panel in sync.
    """
    from apps.workspaces.models import WorkspaceInvite
    from apps.workspaces.services import send_invite_email

    workspace = _get_user_workspace_or_404(request.user, slug)
    if not _user_is_workspace_admin(request.user, workspace):
        return HttpResponseBadRequest(_("Admin or owner only"))
    invite = WorkspaceInvite.objects.filter(workspace=workspace, pk=invite_id, accepted_at__isnull=True).first()
    if invite is None:
        return HttpResponseBadRequest(_("Invite not found or already accepted"))
    sent = send_invite_email(invite, request=request)
    if sent:
        toast = {"message": _("Invite re-sent to %(email)s.") % {"email": invite.email}, "level": "success"}
    else:
        toast = {"message": _("Resend failed — try again later."), "level": "error"}
    return _invites_partial_response(request, workspace, toast=toast)


def _render_workspace_members(workspace, *, viewer):
    """Build the context payload shared by full-page + HTMX fragment.

    ``viewer_membership`` lets the template gate admin-only buttons
    without making the template query the DB again.
    """
    memberships = list(
        workspace.memberships.select_related("user").order_by(
            "-role",
            "user__first_name",
            "user__last_name",
            "user__username",
        )
    )
    viewer_membership = _workspace_member_or_none(viewer, workspace)
    return {
        "workspace": workspace,
        "memberships": memberships,
        "role_choices": WorkspaceMember.ROLE_CHOICES,
        # The Members panel brings people in by email invite (not by
        # picking from every user on the instance — that leaked the whole
        # roster across workspaces), so it needs the invite role choices.
        "invite_role_choices": [
            (WorkspaceMember.ADMIN, WorkspaceMember.ROLE_CHOICES[1][1]),
            (WorkspaceMember.MEMBER, WorkspaceMember.ROLE_CHOICES[2][1]),
        ],
        "viewer_membership": viewer_membership,
        "viewer_is_admin": (
            viewer_membership is not None and viewer_membership.role in (WorkspaceMember.OWNER, WorkspaceMember.ADMIN)
        ),
    }


def _render_workspace_general(workspace, *, viewer_is_admin):
    """Build the General-panel context — workspace identity + basic policy.

    Fields read straight off the workspace; ``viewer_is_admin`` is passed
    in (not re-derived) so the full page doesn't repeat the membership
    lookup the members panel already did.
    """
    return {
        "workspace": workspace,
        "viewer_is_admin": viewer_is_admin,
    }


def _render_workspace_wip(workspace, *, viewer_is_admin):
    """Build the WIP-policy panel context — mode + per-status limit rows.

    Shared by the full settings page and the HTMX save endpoint so the
    card re-renders identically whether it's first paint or an in-place
    swap. ``wip_status_rows`` is a ``(key, label, current)`` tuple per
    kanban status; ``current`` is the saved limit or ``""`` when unset.
    ``viewer_is_admin`` is passed in (not re-derived) so the page doesn't
    repeat the membership lookup the members panel already did.
    """
    wip_mode, wip_limits = workspace.wip_config()
    return {
        "workspace": workspace,
        "viewer_is_admin": viewer_is_admin,
        "wip_mode": wip_mode,
        "wip_mode_choices": Workspace.WIP_MODE_CHOICES,
        "wip_status_rows": [(s, Task.STATUS_LABELS[s], wip_limits.get(s, "")) for s in Task.KANBAN_STATUS_VALUES],
    }


def _render_workspace_cycles(workspace, *, viewer_is_admin):
    """Build the cadence panel context — config + live current/upcoming preview.

    Shared by the full settings page and the HTMX save endpoint. When the
    cadence is enabled the rolling windows are materialized (``ensure_cycles``
    is idempotent) so the preview reflects the just-saved schedule.
    ``viewer_is_admin`` is passed in to avoid a redundant membership query.
    """
    cycle_cfg = workspace.cycle_config()
    ctx = {
        "workspace": workspace,
        "viewer_is_admin": viewer_is_admin,
        "cycle_enabled": cycle_cfg["enabled"],
        "cycle_length_weeks": cycle_cfg["length_weeks"],
        "cycle_start_date": cycle_cfg["start_date"],
        "cycle_auto_rollover": cycle_cfg["auto_rollover"],
        "cycle_length_choices": [
            1,
            2,
            3,
            4,
        ],
    }
    if cycle_cfg["enabled"]:
        today = timezone.localdate()
        ensure_cycles(workspace, today)
        ctx["cycle_today"] = today
        ctx["cycle_current"] = current_cycle(workspace, today)
        ctx["cycle_upcoming"] = workspace.cycles.filter(status=Cycle.PLANNING).order_by("start_date").first()
    return ctx


class WorkspaceSettingsView(LoginRequiredMixin, TemplateView):
    """Workspace settings — member list with admin-only mutation controls.

    Any workspace member can open the page and see the roster. Add /
    remove / role-change actions are submitted to dedicated POST views
    and gated to owners and admins there. The page is intentionally
    minimal for v0.1.0 — one tab, one panel; future fields (auto-
    archive policy, label management entry, etc.) layer on as panels.
    """

    template_name = "web/workspaces/settings.html"

    def get_context_data(self, **kwargs):
        """Return workspace + member roster + pending invites."""
        ctx = super().get_context_data(**kwargs)
        workspace = _get_user_workspace_or_404(self.request.user, self.kwargs["slug"])
        ctx.update(_render_workspace_members(workspace, viewer=self.request.user))
        # Invites panel sits next to members on the same page — share
        # the same workspace + viewer lookup so the two panels stay
        # consistent (admin-gated mutations on both).
        invites_ctx = _render_workspace_invites(workspace, viewer=self.request.user)
        # ``viewer_is_admin`` is computed by both helpers — last one
        # wins on merge, but the value is the same. Keep the explicit
        # update so the template gets every key it expects.
        ctx.update(invites_ctx)
        # WIP-limit + cadence panels — same context builders the HTMX save
        # endpoints use so the cards swap in place identically. Reuse the
        # ``viewer_is_admin`` the members panel already computed rather than
        # re-running the membership lookup twice more.
        viewer_is_admin = ctx["viewer_is_admin"]
        ctx.update(_render_workspace_wip(workspace, viewer_is_admin=viewer_is_admin))
        ctx.update(_render_workspace_cycles(workspace, viewer_is_admin=viewer_is_admin))
        # Labels card — open to every member (no admin gate per the
        # 2026-05-28 UX decision); same context builder the CRUD endpoints
        # use so the partial swaps in identically.
        ctx.update(_labels_section_context(workspace))
        # Danger tab — owner-only actions (transfer ownership, delete). The
        # project count powers the delete warning; transfer candidates are
        # filtered from ``memberships`` in the template (no extra query).
        ctx["viewer_is_workspace_owner"] = workspace.owner_id == self.request.user.id
        ctx["workspace_project_count"] = workspace.projects.count()
        return ctx


def _members_partial_response(request, workspace):
    """Render just the members panel — used after every mutation."""
    return HttpResponse(
        render_to_string(
            "web/workspaces/_settings_members.html",
            _render_workspace_members(workspace, viewer=request.user),
            request=request,
        ),
    )


@require_POST
@login_required
def add_workspace_member(request, slug):
    """Add an existing user to the workspace.

    Form fields: ``user_id`` (int), ``role`` (one of the role choices,
    defaults to ``member``). The acting user must be admin or owner.
    Returns the re-rendered members partial so HTMX can swap it
    in-place.
    """
    workspace = _get_user_workspace_or_404(request.user, slug)
    if not _user_is_workspace_admin(request.user, workspace):
        return HttpResponseBadRequest(_("Admin or owner only"))
    try:
        user_id = int(request.POST.get("user_id") or "")
    except (TypeError, ValueError):
        return HttpResponseBadRequest(_("Invalid user_id"))
    role = request.POST.get("role") or WorkspaceMember.MEMBER
    if role not in {choice for choice, _label in WorkspaceMember.ROLE_CHOICES}:
        return HttpResponseBadRequest(_("Invalid role"))
    if role == WorkspaceMember.OWNER:
        # Ownership is reassigned through a dedicated transfer flow
        # (not yet built). The Add form must not be able to mint a
        # second owner — the constraint is one owner per workspace.
        return HttpResponseBadRequest(_("Cannot add another owner"))
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return HttpResponseBadRequest(_("User not found"))
    if WorkspaceMember.objects.filter(workspace=workspace, user=user).exists():
        return HttpResponseBadRequest(_("User already a member"))
    WorkspaceMember.objects.create(workspace=workspace, user=user, role=role)
    return _members_partial_response(request, workspace)


@require_POST
@login_required
def set_workspace_member_role(request, slug, user_id):
    """Promote / demote a workspace member.

    ``role`` form field carries the new role. The owner cannot be
    demoted from this endpoint (use a transfer-ownership flow instead).
    """
    workspace = _get_user_workspace_or_404(request.user, slug)
    if not _user_is_workspace_admin(request.user, workspace):
        return HttpResponseBadRequest(_("Admin or owner only"))
    target = WorkspaceMember.objects.filter(workspace=workspace, user_id=user_id).first()
    if target is None:
        return HttpResponseBadRequest(_("Not a member"))
    if target.role == WorkspaceMember.OWNER:
        return HttpResponseBadRequest(_("Transfer ownership first"))
    role = request.POST.get("role") or ""
    if role not in {choice for choice, _label in WorkspaceMember.ROLE_CHOICES}:
        return HttpResponseBadRequest(_("Invalid role"))
    if role == WorkspaceMember.OWNER:
        return HttpResponseBadRequest(_("Ownership transfer not supported here"))
    if target.role == role:
        # No-op write; surface the panel without touching the DB.
        return _members_partial_response(request, workspace)
    target.role = role
    target.save(update_fields=["role"])
    return _members_partial_response(request, workspace)


@require_POST
@login_required
def remove_workspace_member(request, slug, user_id):
    """Remove a member from the workspace. Owner can never be removed."""
    workspace = _get_user_workspace_or_404(request.user, slug)
    if not _user_is_workspace_admin(request.user, workspace):
        return HttpResponseBadRequest(_("Admin or owner only"))
    target = WorkspaceMember.objects.filter(workspace=workspace, user_id=user_id).first()
    if target is None:
        return HttpResponseBadRequest(_("Not a member"))
    if target.role == WorkspaceMember.OWNER:
        return HttpResponseBadRequest(_("Cannot remove owner"))
    target.delete()
    return _members_partial_response(request, workspace)


# -----------------------------------------------------------------------------
# Project create modal
# -----------------------------------------------------------------------------


@login_required
def create_project(request):
    """Create-project modal: GET renders the form, POST creates the project.

    ADR 0010 grants project creation to any workspace member (including
    plain Member role), so the only gate is workspace membership — no
    extra admin check here.

    GET:
        Returns the modal HTML fragment (``_create_project_modal.html``).
        Re-fires itself on workspace ``<select>`` change via ``hx-get``
        so the lead picker can refresh against the new workspace's
        members.

    POST:
        Validates name + slug_prefix + workspace + lead; on success
        creates the :class:`Project` and tells HTMX to navigate to the
        new project's detail page via ``HX-Redirect``.
    """
    if request.method == "POST":
        return _create_project_post(request)
    if not _is_htmx_partial(request):
        return redirect("/projects/")
    return _create_project_get(request)


def _create_project_get(request):
    """Render the create-project modal pre-filled from the querystring.

    The workspace dropdown change event fires this view again with the
    new workspace pre-selected (``?workspace=<id>``), so the lead
    picker can re-populate with that workspace's members.
    """
    workspaces = list(Workspace.objects.filter(memberships__user=request.user).order_by("name"))
    selected_workspace = None
    raw_ws = request.GET.get("workspace") or ""
    if raw_ws:
        try:
            ws_id = int(raw_ws)
        except (TypeError, ValueError):
            ws_id = None
        if ws_id is not None:
            selected_workspace = next((w for w in workspaces if w.pk == ws_id), None)
    if selected_workspace is None and workspaces:
        selected_workspace = workspaces[0]

    members = []
    if selected_workspace is not None:
        members = list(selected_workspace.members.order_by("first_name", "last_name", "username"))

    return HttpResponse(
        render_to_string(
            "web/_create_project_modal.html",
            {
                "workspaces": workspaces,
                "selected_workspace": selected_workspace,
                "members": members,
                "pre_name": request.GET.get("name") or "",
                "pre_slug_prefix": request.GET.get("slug_prefix") or "",
                "pre_description": request.GET.get("description") or "",
                "pre_lead_id": _safe_int(request.GET.get("lead")),
            },
            request=request,
        ),
    )


def _safe_int(raw):
    """Return ``int(raw)`` or ``None`` — small helper used by the GET path."""
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _create_project_post(request):
    """Validate + persist a new project and HX-Redirect to its detail page.

    All access checks: the workspace must be one the user is a member
    of; the lead (if set) must also be a workspace member. Anything
    else returns ``400`` with a short reason. The slug_prefix is
    matched against the regex from the model validator BEFORE the DB
    insert so the bad-shape case returns a friendlier ``400`` than a
    raw IntegrityError.
    """
    raw_workspace = request.POST.get("workspace") or ""
    try:
        workspace_id = int(raw_workspace)
    except (TypeError, ValueError):
        return HttpResponseBadRequest(_("Invalid workspace"))
    workspace = Workspace.objects.filter(
        memberships__user=request.user,
        pk=workspace_id,
    ).first()
    if workspace is None:
        return HttpResponseBadRequest(_("Workspace not accessible"))

    name = (request.POST.get("name") or "").strip()
    if not name:
        return HttpResponseBadRequest(_("Name is required"))
    if len(name) > 120:
        return HttpResponseBadRequest(_("Name too long"))

    slug_prefix = (request.POST.get("slug_prefix") or "").strip().upper()
    if not re.match(r"^[A-Z]{2,6}$", slug_prefix):
        return HttpResponseBadRequest(_("Slug prefix must be 2–6 uppercase Latin letters."))
    if Project.objects.filter(workspace=workspace, slug_prefix=slug_prefix).exists():
        return HttpResponseBadRequest(_("Slug prefix already used in this workspace"))

    description = request.POST.get("description") or ""

    lead = None
    raw_lead = (request.POST.get("lead") or "").strip()
    if raw_lead:
        try:
            lead_id = int(raw_lead)
        except (TypeError, ValueError):
            return HttpResponseBadRequest(_("Invalid lead"))
        lead = User.objects.filter(
            workspace_memberships__workspace=workspace,
            pk=lead_id,
        ).first()
        if lead is None:
            return HttpResponseBadRequest(_("Lead must be a member of the project's workspace."))

    with transaction.atomic():
        project = Project.objects.create(
            workspace=workspace,
            name=name,
            slug_prefix=slug_prefix,
            description=description,
            lead=lead,
        )

    # Boosted client-side nav (no full-page reload / loader): swap
    # ``#app-content`` to the new project and close the modal via
    # ``acta:project-created``. The new project isn't a favourite yet, so
    # the sidebar (favourites only) needs no refresh.
    response = HttpResponse(status=204)
    response["HX-Trigger"] = "acta:project-created"
    response["HX-Location"] = json.dumps(
        {
            "path": f"/projects/{project.slug_prefix}/",
            "target": "#app-content",
            "select": "#app-content",
            "swap": "outerHTML show:top",
            "headers": {"HX-Boosted": "true"},
        }
    )
    return response


# -----------------------------------------------------------------------------
# Workspace create modal
# -----------------------------------------------------------------------------


@login_required
def create_workspace(request):
    """Create-workspace modal: GET renders the form, POST creates the workspace.

    Any logged-in user can create a workspace and becomes its owner;
    a matching :class:`WorkspaceMember` row with ``role="owner"`` is
    seeded in the same transaction, mirroring what
    :class:`WorkspaceSerializer.create` does for the API.

    GET without an HX-Request header (i.e. a direct browser navigation
    to ``/workspaces/new/``) lands on the projects list instead of
    rendering the bare modal fragment — the page chrome only shows up
    when the fragment is swapped into ``#modal-root``.
    """
    if request.method == "POST":
        return _create_workspace_post(request)
    if not _is_htmx_partial(request):
        return redirect("/projects/")
    return HttpResponse(
        render_to_string(
            "web/_create_workspace_modal.html",
            {
                "pre_name": request.GET.get("name") or "",
                "pre_slug": request.GET.get("slug") or "",
            },
            request=request,
        ),
    )


def _create_workspace_post(request):
    """Validate + persist a new workspace and HX-Redirect to its settings page.

    Slug logic:
        * If the user provided a slug, validate it and use as-is
          (failures bubble up as ``400``).
        * Else auto-generate from ``name`` via Django's ``slugify``,
          appending ``-2``, ``-3`` until unique. Practical caveat:
          two simultaneous creates with the same slug race — the DB
          unique constraint catches it and we ``400`` with a clear
          message asking for a manual slug.
    """
    from django.utils.text import slugify

    name = (request.POST.get("name") or "").strip()
    if not name:
        return HttpResponseBadRequest(_("Name is required"))
    if len(name) > 120:
        return HttpResponseBadRequest(_("Name too long"))

    raw_slug = (request.POST.get("slug") or "").strip().lower()
    if raw_slug:
        slug = slugify(raw_slug)
        if not slug:
            return HttpResponseBadRequest(_("Invalid slug"))
        if Workspace.objects.filter(slug=slug).exists():
            return HttpResponseBadRequest(_("Slug already taken"))
    else:
        base = slugify(name) or "workspace"
        slug = base
        suffix = 2
        while Workspace.objects.filter(slug=slug).exists():
            slug = f"{base}-{suffix}"
            suffix += 1
            if suffix > 100:  # pragma: no cover — runaway loop guard
                return HttpResponseBadRequest(_("Cannot generate unique slug"))

    with transaction.atomic():
        workspace = Workspace.objects.create(
            name=name,
            slug=slug,
            owner=request.user,
        )
        WorkspaceMember.objects.create(
            workspace=workspace,
            user=request.user,
            role=WorkspaceMember.OWNER,
        )
        # Make the just-created workspace the user's active one so the
        # sidebar lands on it after the reload.
        request.user.active_workspace = workspace
        request.user.save(update_fields=["active_workspace"])

    # Full browser navigation (HX-Redirect, not HX-Location with a partial
    # ``#app-content`` swap) — the sidebar lives outside that region, so a
    # partial swap left the new workspace missing from the switcher until a
    # manual reload.
    response = HttpResponse(status=204)
    response["HX-Redirect"] = f"/workspaces/{workspace.slug}/settings/"
    return response


# -----------------------------------------------------------------------------
# JSON export — download the current filtered view
# -----------------------------------------------------------------------------


def _json_download(payload, filename):
    """Build an attachment ``HttpResponse`` carrying pretty-printed JSON.

    Args:
        payload: A JSON-serializable object.
        filename: The download filename (RFC 5987 encoded in the header).

    Returns:
        An ``HttpResponse`` with ``application/json`` + a
        ``Content-Disposition: attachment`` header.
    """
    response = HttpResponse(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        content_type="application/json; charset=utf-8",
    )
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    return response


def _export_tasks_payload(tasks, *, scope):
    """Wrap a serialized task list with export metadata.

    Args:
        tasks: An iterable of :class:`~apps.tasks.models.Task` (filtered + ordered).
        scope: A short label describing what was exported (workspace or project name).

    Returns:
        A dict with ``scope`` / ``exported_at`` / ``count`` / ``tasks``.
    """
    serialized = serialize_tasks(tasks, include_comments=True)
    return {
        "scope": scope,
        "exported_at": timezone.now().isoformat(),
        "count": len(serialized),
        "tasks": serialized,
    }


@login_required
def export_all_tasks_json(request):
    """Download the All Tasks view (active workspace + querystring filters) as JSON.

    Mirrors :class:`AllTasksView.get_queryset` — same active-workspace
    scope, ``apply_task_filters``, and ordering — so the file matches what
    the page shows. Adds ``reporter`` + ``parent__project`` joins the lean
    table queryset omits, keeping the export N+1-free.
    """
    active = resolve_active_workspace(request)
    if active is None:
        return _json_download(_export_tasks_payload([], scope=None), "acta-tasks.json")
    qs = _user_task_qs(request.user).select_related("reporter", "parent__project").filter(project__workspace=active)
    params = _params_with_archive_cookie(request)
    qs = apply_task_filters(qs, params, request_user=request.user)
    qs = apply_task_ordering(qs, params)
    filename = f"acta-tasks-{timezone.now():%Y%m%d}.json"
    return _json_download(_export_tasks_payload(list(qs), scope=active.name), filename)


@login_required
def export_my_work_json(request):
    """Download the My Work view (assigned, querystring-filtered) as JSON.

    Reuses :func:`_my_work_tasks` so the export reflects the same
    recently-done window, assignee scope, and ordering as the page.
    """
    active = resolve_active_workspace(request)
    params = _params_with_archive_cookie(request)
    tasks = _my_work_tasks(request.user, params, active)
    filename = f"acta-my-work-{timezone.now():%Y%m%d}.json"
    return _json_download(_export_tasks_payload(tasks, scope=active.name if active else None), filename)


def _project_for_export(request, slug_prefix):
    """Resolve a project by slug for export, enforcing membership (404 otherwise)."""
    return get_object_or_404(
        Project.objects.filter(
            slug_prefix=slug_prefix,
            workspace__memberships__user=request.user,
        ).select_related("workspace", "lead"),
    )


@login_required
def export_project_tasks_json(request, slug_prefix):
    """Download a project's task list (querystring-filtered) as JSON.

    Mirrors :class:`ProjectDetailView`'s table queryset — same filters and
    ordering — scoped to the one project.
    """
    project = _project_for_export(request, slug_prefix)
    qs = (
        Task.objects.filter(project=project)
        .select_related("project__workspace", "assignee", "reporter", "parent__project")
        .prefetch_related("labels")
    )
    params = _params_with_archive_cookie(request)
    qs = apply_task_filters(qs, params, request_user=request.user)
    qs = apply_task_ordering(qs, params)
    filename = f"acta-{project.slug_prefix}-tasks-{timezone.now():%Y%m%d}.json"
    return _json_download(_export_tasks_payload(list(qs), scope=project.name), filename)


@login_required
def export_project_overview_json(request, slug_prefix):
    """Download a project's overview (description + updates + comments) as JSON."""
    project = _project_for_export(request, slug_prefix)
    payload = serialize_project_overview(project, viewer_id=request.user.id)
    payload["exported_at"] = timezone.now().isoformat()
    filename = f"acta-{project.slug_prefix}-overview-{timezone.now():%Y%m%d}.json"
    return _json_download(payload, filename)


# -----------------------------------------------------------------------------
# Labels & label-groups management (workspace settings)
# -----------------------------------------------------------------------------
#
# CRUD endpoints behind the Labels card on the workspace settings page. All
# accept POST + return either the freshly rendered ``_settings_labels.html``
# partial (success path — HTMX swaps the whole card) or a 400 with a toast
# trigger explaining the failure. Permission: any workspace member. Activity
# log is intentionally NOT touched — labels are taxonomy, not task content.


def _workspace_for_member(request, slug):
    """Return the workspace if the user is a member, else 404.

    Membership scope is the only gate for label CRUD (per the 2026-05-28
    UX decision — every member can groom the taxonomy). Owner / admin gating
    lives on workspace-level destructive actions (transfer, delete), not on
    labels.
    """
    return get_object_or_404(
        Workspace.objects.filter(memberships__user=request.user),
        slug=slug,
    )


def _labels_section_context(workspace):
    """Build the context the ``_settings_labels.html`` partial needs.

    Loads groups in (name) order with their labels nested by ``(position,
    name)``, plus an "ungrouped" bucket for labels with no group. Each label
    carries its usage count via a single ``Count`` aggregate, so the section
    renders in two queries regardless of label count.

    Args:
        workspace: The active :class:`Workspace`.

    Returns:
        A dict with ``workspace``, ``groups`` (list of ``{group, labels}``),
        ``ungrouped`` (labels list), and ``label_colors`` for the picker.
    """
    label_qs = (
        Label.objects.filter(workspace=workspace)
        .annotate(usage_count=Count("tasks", distinct=True))
        .order_by("position", "name")
    )
    by_group: dict[int | None, list[Label]] = {}
    for label in label_qs:
        by_group.setdefault(label.group_id, []).append(label)
    groups = []
    for group in LabelGroup.objects.filter(workspace=workspace).order_by("name"):
        groups.append({"group": group, "labels": by_group.get(group.id, [])})
    return {
        "workspace": workspace,
        "label_groups": groups,
        "ungrouped_labels": by_group.get(None, []),
        "label_colors": LABEL_COLORS,
    }


def _render_labels_section(workspace, request):
    """Render ``_settings_labels.html`` as a string for a HTMX swap response."""
    ctx = _labels_section_context(workspace)
    return render_to_string("web/workspaces/_settings_labels.html", ctx, request=request)


def _labels_section_response(workspace, request, *, toast=None):
    """Return the labels card HTML wrapped with optional toast trigger."""
    body = _render_labels_section(workspace, request)
    response = HttpResponse(body, content_type="text/html; charset=utf-8")
    triggers: dict = {"acta:labels-changed": True}
    if toast:
        triggers["acta:toast"] = toast
    response["HX-Trigger"] = json.dumps(triggers, default=str)
    return response


def _labels_error_response(message):
    """Return a 400 response that just fires a toast — no DOM swap."""
    response = HttpResponse(status=400)
    response["HX-Trigger"] = json.dumps({"acta:toast": {"message": str(message), "level": "error"}})
    response["HX-Reswap"] = "none"
    return response


def _resolve_group(workspace, raw_group_id):
    """Resolve a group-id form field, ``""`` / missing → ``None`` (ungrouped).

    Returns ``(group_or_none, error_message_or_none)``. A non-blank id that
    doesn't resolve to a same-workspace group is treated as a validation
    error so a tampered form can't silently re-parent a label across
    workspaces.
    """
    raw = (raw_group_id or "").strip()
    if not raw:
        return None, None
    try:
        group_id = int(raw)
    except (TypeError, ValueError):
        return None, _("Unknown label group.")
    group = LabelGroup.objects.filter(workspace=workspace, pk=group_id).first()
    if group is None:
        return None, _("Unknown label group.")
    return group, None


def _next_label_position(workspace, group):
    """Return the next ``position`` for a label being appended to ``group``."""
    current_max = Label.objects.filter(workspace=workspace, group=group).aggregate(m=Max("position"))["m"]
    return (current_max or 0) + 1


@require_POST
@login_required
def create_label_group(request, slug):
    """Create a new :class:`LabelGroup` and re-render the labels section."""
    workspace = _workspace_for_member(request, slug)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _labels_error_response(_("Group name is required."))
    description = (request.POST.get("description") or "").strip()
    is_exclusive = request.POST.get("is_exclusive") == "1"
    if LabelGroup.objects.filter(workspace=workspace, name__iexact=name).exists():
        return _labels_error_response(_("A group with that name already exists."))
    LabelGroup.objects.create(
        workspace=workspace,
        name=name,
        description=description,
        is_exclusive=is_exclusive,
    )
    return _labels_section_response(
        workspace,
        request,
        toast={"message": str(_("Group created.")), "level": "success"},
    )


@require_POST
@login_required
def update_label_group(request, slug, group_id):
    """Rename / re-describe / toggle exclusivity for an existing group."""
    workspace = _workspace_for_member(request, slug)
    group = get_object_or_404(LabelGroup, workspace=workspace, pk=group_id)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _labels_error_response(_("Group name is required."))
    description = (request.POST.get("description") or "").strip()
    is_exclusive = request.POST.get("is_exclusive") == "1"
    clash = LabelGroup.objects.filter(workspace=workspace, name__iexact=name).exclude(pk=group.pk).exists()
    if clash:
        return _labels_error_response(_("A group with that name already exists."))
    group.name = name
    group.description = description
    group.is_exclusive = is_exclusive
    group.save(update_fields=["name", "description", "is_exclusive"])
    return _labels_section_response(workspace, request)


@require_POST
@login_required
def delete_label_group(request, slug, group_id):
    """Delete a group — its labels stay in the workspace as ungrouped (SET_NULL)."""
    workspace = _workspace_for_member(request, slug)
    group = get_object_or_404(LabelGroup, workspace=workspace, pk=group_id)
    group.delete()
    return _labels_section_response(
        workspace,
        request,
        toast={"message": str(_("Group deleted. Its labels moved to Ungrouped.")), "level": "success"},
    )


@require_POST
@login_required
def create_label(request, slug):
    """Create a label, optionally inside a group, and re-render the section."""
    workspace = _workspace_for_member(request, slug)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _labels_error_response(_("Label name is required."))
    color = (request.POST.get("color") or "").strip()
    if not is_curated_label_color(color):
        return _labels_error_response(_("Pick a colour from the palette."))
    group, err = _resolve_group(workspace, request.POST.get("group"))
    if err:
        return _labels_error_response(err)
    if Label.objects.filter(workspace=workspace, name__iexact=name).exists():
        return _labels_error_response(_("A label with that name already exists in this workspace."))
    Label.objects.create(
        workspace=workspace,
        name=name,
        color=color,
        group=group,
        position=_next_label_position(workspace, group),
    )
    return _labels_section_response(
        workspace,
        request,
        toast={"message": str(_("Label created.")), "level": "success"},
    )


@require_POST
@login_required
def update_label(request, slug, label_id):
    """Rename / recolour / move-to-group an existing label."""
    workspace = _workspace_for_member(request, slug)
    label = get_object_or_404(Label, workspace=workspace, pk=label_id)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _labels_error_response(_("Label name is required."))
    color = (request.POST.get("color") or "").strip()
    if not is_curated_label_color(color):
        return _labels_error_response(_("Pick a colour from the palette."))
    group, err = _resolve_group(workspace, request.POST.get("group"))
    if err:
        return _labels_error_response(err)
    clash = Label.objects.filter(workspace=workspace, name__iexact=name).exclude(pk=label.pk).exists()
    if clash:
        return _labels_error_response(_("A label with that name already exists in this workspace."))
    fields_to_update = ["name", "color"]
    label.name = name
    label.color = color
    if group != label.group:
        # Group changed — drop to the bottom of the new group so the
        # re-render reads in a sensible order. Drag-drop within the new
        # group can still reposition it after.
        label.group = group
        label.position = _next_label_position(workspace, group)
        fields_to_update.extend(["group", "position"])
    label.save(update_fields=fields_to_update)
    return _labels_section_response(workspace, request)


@require_POST
@login_required
def delete_label(request, slug, label_id):
    """Hard-delete a label; M2M ``task_labels`` rows cascade automatically."""
    workspace = _workspace_for_member(request, slug)
    label = get_object_or_404(Label, workspace=workspace, pk=label_id)
    label.delete()
    return _labels_section_response(
        workspace,
        request,
        toast={"message": str(_("Label deleted.")), "level": "success"},
    )


@require_POST
@login_required
def reorder_labels(request, slug):
    """Persist a drag-drop reorder. Body carries one ``group_id`` slice at a time.

    Expected POST:

    * ``group`` — target group id, or ``""`` for the ungrouped bucket.
    * ``label_ids`` — repeated form fields with the labels' new top-to-bottom
      order. The client only sends the slice it touched (Sortable.js fires
      a single event per drop), so the persisted order is dense within that
      group; other groups stay untouched.

    Returns 204 — the client already moved the DOM nodes; no swap needed.
    """
    workspace = _workspace_for_member(request, slug)
    group, err = _resolve_group(workspace, request.POST.get("group"))
    if err:
        return HttpResponseBadRequest(err)
    raw_ids = request.POST.getlist("label_ids")
    try:
        ordered_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Invalid label id payload.")
    labels = list(
        Label.objects.filter(workspace=workspace, pk__in=ordered_ids).only("id", "group_id", "position"),
    )
    by_id = {label.id: label for label in labels}
    to_update = []
    for index, label_id in enumerate(ordered_ids, start=1):
        label = by_id.get(label_id)
        if label is None:
            return HttpResponseBadRequest("Label id outside workspace.")
        if label.position != index or label.group_id != (group.id if group else None):
            label.position = index
            label.group = group
            to_update.append(label)
    if to_update:
        Label.objects.bulk_update(to_update, ["position", "group"])
    return HttpResponse(status=204)


@login_required
def palette_search(request):
    """JSON typeahead for the global command palette (``Cmd/Ctrl+K``).

    Scoped to the user's active workspace. Returns three sections so the
    Alpine component can render them as labelled groups:

    * ``tasks`` — ILIKE on ``Task.title`` plus exact ``PREFIX-NUMBER`` or
      bare-number match (same shape as ``task_link_search``). Up to 8,
      newest-updated first.
    * ``projects`` — ILIKE on ``Project.name`` / ``slug_prefix`` within
      the active workspace. Up to 5, alphabetical.
    * ``nav`` — static list of top-level destinations (Dashboard, Inbox,
      My Work, etc.) filtered by case-insensitive substring on the
      label.

    An empty query returns the most-recent tasks, all accessible
    projects, and every nav entry — so the palette is useful the moment
    it opens, before the user types anything.
    """
    from apps.web.templatetags.lucide import lucide as _lucide

    q = (request.GET.get("q") or "").strip()
    workspace = resolve_active_workspace(request)

    task_items = []
    if workspace:
        # Palette tasks only need title / status / slug / project name —
        # no labels / blocks / blocked_by like ``_user_task_qs`` would
        # prefetch. Building a minimal queryset directly drops three
        # joins per palette request (negligible per-hit, but the
        # endpoint fires on every keystroke).
        qs = Task.objects.filter(
            project__workspace=workspace,
            project__workspace__memberships__user=request.user,
        ).select_related("project")
        if q:
            match = Q(title__icontains=q)
            upper = q.upper()
            if "-" in upper:
                prefix, _sep, num = upper.rpartition("-")
                if num.isdigit():
                    match |= Q(project__slug_prefix=prefix, number=int(num))
            elif q.isdigit():
                match |= Q(number=int(q))
            qs = qs.filter(match)
        for task in qs.order_by("-updated_at")[:8]:
            task_items.append(
                {
                    "slug": task.slug,
                    "title": task.title,
                    "status": task.status,
                    "project": task.project.name,
                    "url": reverse(
                        "web:task_detail",
                        kwargs={
                            "slug_prefix": task.project.slug_prefix,
                            "number": task.number,
                        },
                    ),
                },
            )

    project_items = []
    accessible_projects = []
    if workspace:
        base_pqs = (
            Project.objects.filter(
                workspace=workspace,
                workspace__memberships__user=request.user,
                archived=False,
            )
            .distinct()
            .order_by("name")
        )
        # Materialise once: same rows feed the projects section (filtered
        # by ``q``) and the "Create task in <project>" Quick Actions
        # (always full list, capped) so we don't run the lookup twice.
        accessible_projects = list(base_pqs[:25])
        pqs_for_section = accessible_projects
        if q:
            needle_upper = q.upper()
            pqs_for_section = [
                p for p in accessible_projects if q.lower() in p.name.lower() or needle_upper in p.slug_prefix
            ]
        for project in pqs_for_section[:5]:
            project_items.append(
                {
                    "name": project.name,
                    "slug_prefix": project.slug_prefix,
                    "icon_html": _lucide(project.icon or "folder", "w-3.5 h-3.5"),
                    "icon_color_class": project.icon_color_class,
                    "url": reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
                },
            )

    nav_targets = [
        ("layout-dashboard", _("Dashboard"), reverse("web:dashboard")),
        ("inbox", _("Inbox"), reverse("web:inbox")),
        ("briefcase", _("My Work"), reverse("web:my_work")),
        ("list-checks", _("All Tasks"), reverse("web:all_tasks")),
        ("folders", _("Projects"), reverse("web:project_list")),
        ("history", _("My activity"), reverse("web:my_activity")),
    ]
    if workspace and workspace.cycle_config()["enabled"]:
        nav_targets.append(("iteration-cw", _("Cycles"), reverse("web:cycles_overview")))
    nav_targets.append(("user", _("Account settings"), reverse("accounts:settings")))
    if workspace:
        nav_targets.append(
            (
                "settings",
                _("Workspace settings"),
                reverse("web:workspace_settings", kwargs={"slug": workspace.slug}),
            ),
        )

    needle = q.lower()
    nav_items = [
        {"icon_html": _lucide(icon, "w-4 h-4"), "label": str(label), "url": url}
        for icon, label, url in nav_targets
        if not needle or needle in str(label).lower()
    ]

    # Quick Actions — verbs the palette executes client-side instead of
    # navigating. Each carries ``action`` + optional ``payload`` so the
    # Alpine ``follow()`` handler can dispatch the right open-modal /
    # state change. We always offer "New task"; one entry per accessible
    # project (capped) pre-fills the create modal with that project.
    plus_icon = _lucide("plus", "w-4 h-4")
    action_items = [
        {"label": str(_("New task")), "icon_html": plus_icon, "action": "create_task"},
    ]
    for project in accessible_projects[:6]:
        action_items.append(
            {
                "label": str(_("New task in %(name)s")) % {"name": project.name},
                "icon_html": plus_icon,
                "action": "create_task",
                "payload": {"project": project.slug_prefix},
            },
        )
    filtered_actions = [a for a in action_items if not needle or needle in a["label"].lower()]

    return JsonResponse(
        {
            "q": q,
            "sections": [
                {"kind": "tasks", "label": str(_("Tasks")), "items": task_items},
                {"kind": "actions", "label": str(_("Quick actions")), "items": filtered_actions},
                {"kind": "projects", "label": str(_("Projects")), "items": project_items},
                {"kind": "nav", "label": str(_("Navigation")), "items": nav_items},
            ],
        },
    )
