"""Server-rendered page views.

Per docs/decisions/0014-frontend-architecture.md, page views return
rendered Django templates; HTMX handles inline updates from the same
endpoints (or from `/api/v1/...` for JSON-only consumers).
"""

import datetime
import json
import re
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Exists, F, Max, OuterRef, Q, Subquery
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
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
from apps.labels.models import Label
from apps.notifications.models import Notification
from apps.notifications.services import notify_comment_created, notify_project_update_created
from apps.projects.models import Project, ProjectUpdate
from apps.reactions.services import TARGET_TYPES, attach_reactions, summarize_reactions, toggle_reaction
from apps.tasks.events import broadcast_link_change, broadcast_task_events, emit_task_diff_events, snapshot_task
from apps.tasks.metrics import compute_flow_metrics
from apps.tasks.models import Task
from apps.web.filters import (
    SORTABLE_COLUMNS,
    apply_task_filters,
    apply_task_ordering,
    filter_sidebar_context,
    resolve_show_archived,
)
from apps.web.grouping import group_tasks
from apps.web.nav import resolve_active_workspace, set_active_workspace
from apps.workspaces.models import Workspace, WorkspaceMember

User = get_user_model()

_OPEN_STATUSES = [
    Task.STATUS_PLANNED,
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]


_VIEW_MODES = {"overview", "kanban", "table", "list", "timeline"}


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
            t.due_date is None,
            t.due_date or datetime.date.max,
        ),
    )
    all_dates = [d for t in timeline_tasks for d in (t.start_date, t.due_date) if d]
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
}


def _list_axis_options(option_keys, active_key):
    """Render-ready axis tabs for the List view picker.

    Returns a list of ``{"key", "label", "active"}`` dicts in the
    requested order so the template can render them as a tab group.
    """
    return [{"key": key, "label": _LIST_AXIS_LABELS[key], "active": key == active_key} for key in option_keys]


def _resolve_view_mode(request, *, default, allow_overview=False):
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

    Returns:
        One of ``"overview"``, ``"kanban"``, ``"table"``.
    """
    allowed = _VIEW_MODES if allow_overview else _VIEW_MODES - {"overview"}
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
        Task.objects.filter(project__workspace__memberships__user=user)
        .select_related(
            "project__workspace",
            "assignee",
        )
        .prefetch_related("labels", "blocks", "blocked_by")
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
            Q(status__in=_OPEN_STATUSES) | Q(status=Task.STATUS_DONE, updated_at__gte=done_cutoff),
        )
        .select_related("project__workspace", "assignee", "reporter")
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
        if self.request.GET.get("panel") == "list":
            return ["web/projects/_list_panel.html"]
        if self.request.GET.get("panel") == "timeline":
            return ["web/projects/_timeline.html"]
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
        qs = apply_task_filters(qs, params, request_user=self.request.user)
        return apply_task_ordering(qs, params)

    def render_to_response(self, context, **response_kwargs):
        """Persist ``view_mode`` + ``show_archived`` + ``list_axis`` cookies."""
        response = super().render_to_response(context, **response_kwargs)
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
        return response

    def get_context_data(self, **kwargs):
        """Attach filter sidebar context + kanban columns when needed.

        Assignee lives in the top strip, not in the sidebar.
        """
        ctx = super().get_context_data(**kwargs)
        view_mode = _resolve_view_mode(self.request, default="table")
        ctx["view_mode"] = view_mode
        ctx["view_panel_target"] = "#task-list-wrapper"
        ctx["show_project"] = True
        ctx["show_labels"] = True
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

        # Table-only HTMX swap (column sort header): we only render
        # ``_table.html`` so the kanban sort + five list-axis groupings
        # below would be wasted work. Skip them — sort latency drops
        # from "rebuild every view" to "ORDER BY + table partial".
        table_only = self.request.headers.get("HX-Target") == "task-table-root"
        # ``?panel=list`` is the lazy-load fetch for just the list view
        # body — we still need the list axes, but skip kanban columns
        # and the filter sidebar context.
        panel = self.request.GET.get("panel")
        list_only = panel == "list"
        if list_only:
            list_axis_keys = ("deadline", "status", "priority", "assignee", "project")
            list_axis = _resolve_list_axis(self.request, default="project", options=list_axis_keys)
            ctx["list_axis"] = list_axis
            ctx["list_axis_options"] = _list_axis_options(list_axis_keys, list_axis)
            ctx["list_sections_by_axis"] = {
                key: group_tasks(table_tasks, key, request_user=self.request.user) for key in list_axis_keys
            }
            return ctx
        if not table_only:
            kanban_tasks = sorted(
                table_tasks,
                key=lambda t: (
                    Task.STATUS_VALUES.index(t.status) if t.status in Task.STATUS_VALUES else 99,
                    -(t.priority or 0),
                    -t.updated_at.timestamp(),
                ),
            )
            ctx["columns"] = _build_kanban_columns(kanban_tasks)
            list_axis_keys = ("deadline", "status", "priority", "assignee", "project")
            list_axis = _resolve_list_axis(self.request, default="project", options=list_axis_keys)
            ctx["list_axis"] = list_axis
            ctx["list_axis_options"] = _list_axis_options(list_axis_keys, list_axis)
            ctx["list_sections_by_axis"] = {
                key: group_tasks(table_tasks, key, request_user=self.request.user) for key in list_axis_keys
            }
        ctx.update(
            filter_sidebar_context(
                self.request,
                hide_assignee=True,
                extra_preserved={"view": view_mode},
                effective_params=_params_with_archive_cookie(self.request),
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
        list_axis_keys = ("deadline", "status", "priority", "project")
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
}

_INBOX_FILTER_KINDS = {
    "mentions": Notification.Kind.MENTION,
    "assigned": Notification.Kind.ASSIGNED,
    "due": Notification.Kind.DUE,
    "comments": Notification.Kind.COMMENT,
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
        ``due`` / ``comments`` integer counts over active notifications.
    """
    return _inbox_base_qs(user).aggregate(
        all=Count("id"),
        unread=Count("id", filter=Q(is_read=False)),
        mentions=Count("id", filter=Q(kind=Notification.Kind.MENTION)),
        assigned=Count("id", filter=Q(kind=Notification.Kind.ASSIGNED)),
        due=Count("id", filter=Q(kind=Notification.Kind.DUE)),
        comments=Count("id", filter=Q(kind=Notification.Kind.COMMENT)),
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

    def get_template_names(self):
        """Return either the dashboard or the no-workspaces template."""
        has_membership = WorkspaceMember.objects.filter(user=self.request.user).exists()
        return ["web/dashboard.html"] if has_membership else ["web/no_workspaces.html"]


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
        return (
            Project.objects.filter(workspace=active)
            .select_related("workspace", "lead")
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
        return ctx


class ProjectDetailView(LoginRequiredMixin, DetailView):
    """Project page with Kanban / Table view switching."""

    context_object_name = "project"

    def get_template_names(self):
        """Full page on cold load; only the panel fragment for HTMX swaps.

        ``HX-Target=task-table-root`` short-circuits to the table-only
        partial so a column sort doesn't repaint kanban + the five
        list-view group axes (which the panel partial rebuilds even
        when the user only wants the rows re-sorted).
        """
        if self.request.headers.get("HX-Target") == "task-table-root":
            return ["web/projects/_table.html"]
        if self.request.GET.get("panel") == "list":
            return ["web/projects/_list_panel.html"]
        if self.request.GET.get("panel") == "timeline":
            return ["web/projects/_timeline.html"]
        if _is_htmx_partial(self.request):
            return ["web/projects/_view_panel_wrapper.html"]
        return ["web/projects/detail.html"]

    def get_object(self, queryset=None):
        """Resolve the project by slug_prefix and enforce membership.

        Annotates ``is_favourite`` via an ``Exists`` subquery so the
        overview star renders without a separate favourites lookup
        (keeps the page query count constant). Viewing a project also
        pulls its workspace into focus (active-workspace switch) so the
        sidebar and the scoped views stay consistent with what's on screen.
        """
        slug_prefix = self.kwargs["slug_prefix"]
        favourited = self.request.user.favourite_projects.filter(pk=OuterRef("pk"))
        project = get_object_or_404(
            Project.objects.filter(
                slug_prefix=slug_prefix,
                workspace__memberships__user=self.request.user,
            )
            .select_related("workspace", "lead")
            .annotate(is_favourite=Exists(favourited)),
        )
        set_active_workspace(self.request, project.workspace)
        return project

    def render_to_response(self, context, **response_kwargs):
        """Persist ``view_mode`` + ``show_archived`` + ``list_axis`` cookies."""
        response = super().render_to_response(context, **response_kwargs)
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
        view_mode = _resolve_view_mode(self.request, default="kanban", allow_overview=True)
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
        # Both bodies render in the DOM; table honors ``?order=``,
        # kanban keeps the fixed status grouping. We sort once per
        # body — the difference is small enough not to need separate
        # querysets, but mixing orderings on a single list would
        # confuse one of the two views.
        table_tasks = list(
            apply_task_ordering(
                base,
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
        # ``?panel=list`` — lazy fetch of just the list view body.
        panel = self.request.GET.get("panel")
        if panel == "list":
            list_axis_keys = ("deadline", "status", "priority", "assignee")
            list_axis = _resolve_list_axis(self.request, default="status", options=list_axis_keys)
            ctx["list_axis"] = list_axis
            ctx["list_axis_options"] = _list_axis_options(list_axis_keys, list_axis)
            ctx["list_sections_by_axis"] = {
                key: group_tasks(table_tasks, key, request_user=self.request.user) for key in list_axis_keys
            }
            return ctx
        # ``?panel=timeline`` — lazy fetch of just the Gantt body. Skip
        # the kanban columns + list axes + filter sidebar build below.
        if panel == "timeline":
            ctx.update(_timeline_context(table_tasks, today))
            return ctx
        if not table_only:
            # When the user hasn't picked a custom ``?order=`` the table
            # falls back to the same ordering kanban uses (status,
            # -priority, -updated_at), so the two lists are identical.
            # Reuse ``table_tasks`` instead of evaluating the queryset
            # a second time — that double-fetch was the source of a
            # +6-query N+1 regression caught by
            # ``test_project_detail_constant_queries``.
            table_order_key = (self.request.GET.get("order") or "").strip().lstrip("-")
            if table_order_key in SORTABLE_COLUMNS:
                kanban_tasks = list(base.order_by("status", "-priority", "-updated_at"))
            else:
                kanban_tasks = table_tasks
            ctx["tasks"] = table_tasks if view_mode == "table" else kanban_tasks
            ctx["columns"] = _build_kanban_columns(kanban_tasks, today=today, wip_limits=project.wip_limits)
            list_axis_keys = ("deadline", "status", "priority", "assignee")
            list_axis = _resolve_list_axis(self.request, default="status", options=list_axis_keys)
            ctx["list_axis"] = list_axis
            ctx["list_axis_options"] = _list_axis_options(list_axis_keys, list_axis)
            ctx["list_sections_by_axis"] = {
                key: group_tasks(table_tasks, key, request_user=self.request.user) for key in list_axis_keys
            }
        else:
            ctx["tasks"] = table_tasks
            ctx["show_labels"] = True

        # Per-project page: scope project + workspace filters away.
        # Show labels in the table view (matches All Tasks layout).
        ctx.update(
            filter_sidebar_context(
                self.request,
                hide_assignee=True,
                hide_project=True,
                hide_status=(view_mode == "kanban"),
                htmx_target="#project-view-panel",
                extra_preserved={"view": view_mode},
                effective_params=params,
                available_labels=list(
                    Label.objects.filter(workspace=self.object.workspace).order_by("name"),
                ),
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
        ctx["workspace_projects"] = _workspace_projects(task)
        ctx["attached_label_ids"] = set(task.labels.values_list("id", flat=True))
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
                "workspace_projects": _workspace_projects(task),
                "attached_label_ids": set(task.labels.values_list("id", flat=True)),
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
                "workspace_projects": _workspace_projects(task),
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
    """Return the workspace's labels ordered by name.

    Used by the labels picker to populate its dropdown.

    Args:
        task: The :class:`Task` whose workspace's labels to fetch.

    Returns:
        A queryset of :class:`Label` rows.
    """
    return Label.objects.filter(workspace=task.project.workspace).order_by("name")


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
        task.save()
        emit_task_diff_events(old_state=old, task=task, actor=request.user)
    return _inline_edit_response(
        request,
        task,
        "web/projects/_status_cell.html",
        {
            "task": task,
            "status_labels": Task.STATUS_LABELS,
        },
    )


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
            task.labels.add(label)
        emit_task_diff_events(old_state=old, task=task, actor=request.user)
    ctx = {
        "task": task,
        "workspace_labels": _workspace_labels(task),
        "attached_label_ids": set(task.labels.values_list("id", flat=True)),
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
                    reverse("accounts:serve_avatar", kwargs={"user_id": t.assignee_id}) if t.assignee.avatar else None
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
    return _inline_edit_response(
        request,
        task,
        "web/projects/_due_date_cell.html",
        {"task": task},
    )


@require_POST
@login_required
def set_task_start_date(request, slug_prefix, number):
    """Inline start-date change; used by the timeline drag-resize handler.

    Accepts ``start_date`` as an ISO-8601 date string or empty string
    (clears the field). Returns 200 with no body on success — the
    timeline bar already moved optimistically on the client.

    Args:
        request: Django request with a ``start_date`` POST field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        ``HttpResponse(status=200)`` on success.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    raw = (request.POST.get("start_date") or "").strip()
    if raw == "":
        new_start_date = None
    else:
        try:
            new_start_date = datetime.date.fromisoformat(raw)
        except ValueError:
            return HttpResponseBadRequest("invalid start_date")
    _apply_task_field_change(task, "start_date", new_start_date, request.user)
    return HttpResponse(status=200)


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
def set_wip_limit(request, slug_prefix):
    """Set or clear a kanban column's WIP limit for a project.

    Reads ``status`` (one of ``Task.KANBAN_STATUS_VALUES``) and ``limit``
    (a positive integer; empty / ``0`` clears the limit). Stored in
    ``Project.wip_limits`` (a status→limit map). Replies ``204`` with
    ``HX-Trigger: acta:task-changed`` so the board panel refetches and
    the column header re-renders the new fraction + capacity bar.
    """
    project = _get_user_project_or_404(request.user, slug_prefix)
    status = request.POST.get("status", "")
    if status not in Task.KANBAN_STATUS_VALUES:
        return HttpResponseBadRequest("invalid status")
    raw = (request.POST.get("limit") or "").strip()
    limits = dict(project.wip_limits or {})
    if raw in ("", "0"):
        limits.pop(status, None)
    else:
        try:
            n = int(raw)
        except ValueError:
            return HttpResponseBadRequest("invalid limit")
        if n < 0:
            return HttpResponseBadRequest("invalid limit")
        limits[status] = n
    project.wip_limits = limits
    project.save(update_fields=["wip_limits"])
    response = HttpResponse(status=204)
    response["HX-Trigger"] = "acta:task-changed"
    return response


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
    throughput = metrics["throughput"]
    avg_throughput = round(sum(p["count"] for p in throughput) / len(throughput), 1) if throughput else 0

    def fmt(hours):
        if hours is None:
            return "—"
        if hours < 24:
            return f"{hours:.0f}h"
        return f"{hours / 24:.1f}d"

    ctx = {
        "project": project,
        "metrics": metrics,
        "avg_throughput": avg_throughput,
        "cycle_median_fmt": fmt(metrics["cycle_median"]),
        "cycle_p85_fmt": fmt(metrics["cycle_p85"]),
        "lead_median_fmt": fmt(metrics["lead_median"]),
        "throughput_labels_json": json.dumps([p["label"] for p in throughput]),
        "throughput_data_json": json.dumps([p["count"] for p in throughput]),
        "cycle_hist_json": json.dumps(_cycle_histogram(metrics["cycle_times"])),
    }
    return render(request, "web/projects/insights.html", ctx)


@require_POST
@login_required
def set_project_lead(request, slug_prefix):
    """Inline lead change on the project overview.

    Accepts an integer ``lead_id`` form field, or an empty value to
    clear the lead. The chosen user must be a member of the project's
    workspace; non-member ids 400. Returns the rendered lead cell
    fragment so HTMX can swap it in place.
    """
    project = _get_user_project_or_404(request.user, slug_prefix)
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
            "workspace_projects": _workspace_projects(task),
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
            "workspace_projects": _workspace_projects(task),
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
                "workspace_labels": _workspace_labels(task),
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
    if workspace:
        members = list(
            WorkspaceMember.objects.filter(workspace=workspace).select_related("user").order_by("user__username"),
        )
        projects = list(Project.objects.filter(workspace=workspace).order_by("name"))
        labels = list(Label.objects.filter(workspace=workspace).order_by("name"))
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
    """Labels available in ``project``'s workspace.

    Args:
        project: The :class:`Project` whose workspace labels to fetch.

    Returns:
        A queryset of :class:`Label` rows ordered by name.
    """
    return Label.objects.filter(workspace=project.workspace).order_by("name")


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
    return HttpResponse(
        render_to_string(
            "web/_create_task_modal.html",
            {
                "projects": projects,
                "selected_project": selected_project,
                "members": members,
                "labels": labels,
                "pre_status": pre_status,
                "pre_priority": pre_priority,
                "pre_assignee_id": pre_assignee_id,
                "pre_title": pre_title,
                "pre_description": pre_description,
                "pre_due_date": pre_due_date,
                "pre_label_ids": pre_label_ids,
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
        task.save()
        if label_ids:
            task.labels.set(label_ids)
        log_event(
            workspace=project.workspace,
            project=project,
            actor=request.user,
            event_type="task.created",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"title": task.title, "status": task.status},
        )
    detail_url = f"/projects/{project.slug_prefix}/{task.number}/"
    response = HttpResponse(status=204)
    open_after = request.POST.get("open_after_create") == "1"
    if open_after:
        # Boosted client-side navigation (``HX-Location``) instead of a
        # full-page ``HX-Redirect`` — the new task opens by swapping
        # ``#app-content`` only, no reload / loader flash. ``acta:task-created``
        # closes the modal; the panel-refetch it also triggers on the
        # current page is harmless here since ``#app-content`` (panels
        # included) is being replaced wholesale by the swap.
        response["HX-Trigger"] = "acta:task-created"
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
        # No redirect: stay on the current page, but tell the page to
        # refresh its task list. Connected HTMX listeners on
        # ``acta:task-created`` re-fetch their fragment; the modal
        # picks up the same event and closes itself.
        response["HX-Trigger"] = "acta:task-created"
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


def _build_kanban_columns(tasks, today=None, wip_limits=None):
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

    Args:
        tasks: Materialised task list (one board's worth).
        today: Date anchor for overdue / done-this-week buckets.
        wip_limits: Optional ``{status_key: max_cards}`` map
            (``Project.wip_limits``); a positive limit adds ``limit`` /
            ``over_limit`` to that column so the header can render the
            ``N/limit`` fraction + capacity bar.
    """
    wip_limits = wip_limits or {}
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
        t.age_days = None
        since = getattr(t, "status_since", None)
        if status != Task.STATUS_DONE and since is not None:
            t.age_days = (today - since.date()).days

    def _limit_for(status):
        try:
            return int(wip_limits.get(status) or 0)
        except (TypeError, ValueError):
            return 0

    columns = []
    for status in Task.KANBAN_STATUS_VALUES:
        count = len(buckets[status])
        limit = _limit_for(status)
        columns.append(
            {
                "key": status,
                "label": Task.STATUS_LABELS[status],
                "tasks": buckets[status],
                "overdue_count": overdue[status],
                "active_avatars": list(avatars[status].values()),
                "done_this_week": done_this_week if status == Task.STATUS_DONE else 0,
                "limit": limit,
                "over_limit": bool(limit) and count > limit,
                "at_limit": bool(limit) and count == limit,
                "fill_pct": min(100, round(count * 100 / limit)) if limit else 0,
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
    member_user_ids = {m.user_id for m in memberships}
    candidates = list(
        User.objects.exclude(pk__in=member_user_ids).order_by(
            "first_name",
            "last_name",
            "username",
        )
    )
    viewer_membership = _workspace_member_or_none(viewer, workspace)
    return {
        "workspace": workspace,
        "memberships": memberships,
        "candidates": candidates,
        "role_choices": WorkspaceMember.ROLE_CHOICES,
        "viewer_membership": viewer_membership,
        "viewer_is_admin": (
            viewer_membership is not None and viewer_membership.role in (WorkspaceMember.OWNER, WorkspaceMember.ADMIN)
        ),
    }


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

    # Boosted client-side nav (no full reload): swap ``#app-content`` to
    # the new workspace's settings and close the modal via
    # ``acta:workspace-created``.
    response = HttpResponse(status=204)
    response["HX-Trigger"] = "acta:workspace-created"
    response["HX-Location"] = json.dumps(
        {
            "path": f"/workspaces/{workspace.slug}/settings/",
            "target": "#app-content",
            "select": "#app-content",
            "swap": "outerHTML show:top",
            "headers": {"HX-Boosted": "true"},
        }
    )
    return response
