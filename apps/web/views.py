"""Server-rendered page views.

Per docs/decisions/0014-frontend-architecture.md, page views return
rendered Django templates; HTMX handles inline updates from the same
endpoints (or from `/api/v1/...` for JSON-only consumers).
"""

import datetime

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView, TemplateView

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.comments.models import Comment
from apps.labels.models import Label
from apps.projects.models import Project, ProjectUpdate
from apps.tasks.events import emit_task_diff_events, snapshot_task
from apps.tasks.models import Task
from apps.web.filters import apply_task_filters, apply_task_ordering, filter_sidebar_context, resolve_show_archived
from apps.web.grouping import group_tasks
from apps.workspaces.models import WorkspaceMember

User = get_user_model()

_OPEN_STATUSES = [
    Task.STATUS_PLANNED,
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]


_VIEW_MODES = {"overview", "kanban", "table", "list"}


def _is_htmx_partial(request):
    """True when the response should be the *inner* partial.

    HTMX sends ``HX-Request: true`` on every AJAX swap. We narrow that
    down: when ``HX-Boosted: true`` is also set, the request comes from
    an ``hx-boost``-driven anchor (sidebar navigation), and we need the
    **full** template so HTMX can ``hx-select`` the ``#app-content``
    fragment from a shell-aware response. Only un-boosted HTMX requests
    (filter form submit, kanban sort, panel refresh on ``acta:*``
    events) want the inner-only partial.
    """
    return request.headers.get("HX-Request") == "true" and request.headers.get("HX-Boosted") != "true"


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
        .prefetch_related("labels")
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


def _my_work_tasks(user, params):
    """Resolve the My Work task queryset for ``user``.

    Querystring filters (``params``) narrow the base queryset — except
    the assignee filter, which is implicit (``me``). Done tasks reach
    the queryset via the page-specific
    ``Q(status=DONE, updated_at>=cutoff)`` clause so the "Recently done"
    bucket stays populated without showing ancient done rows. If the
    user picks specific statuses in the sidebar, those override the
    open/recently-done split (``apply_task_filters`` honours the
    selection). Grouping into sections is delegated to
    :func:`apps.web.grouping.group_tasks`.
    """
    done_cutoff = timezone.now() - datetime.timedelta(days=7)
    base = (
        Task.objects.filter(assignee=user)
        .filter(
            Q(status__in=_OPEN_STATUSES) | Q(status=Task.STATUS_DONE, updated_at__gte=done_cutoff),
        )
        .select_related("project__workspace", "assignee", "reporter")
        .prefetch_related("labels")
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
            ctx["columns"] = [
                {
                    "key": status,
                    "label": Task.STATUS_LABELS[status],
                    "tasks": [t for t in kanban_tasks if t.status == status],
                }
                for status in Task.STATUS_VALUES
            ]
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
        tasks = _my_work_tasks(self.request.user, params)
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
        ctx.update(
            filter_sidebar_context(
                self.request,
                hide_assignee=True,
                hide_project=True,
                htmx_target="#my-work-content",
                effective_params=params,
            )
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
        member JOIN from inflating the open_task_count.
        """
        latest = ProjectUpdate.objects.filter(project=OuterRef("pk")).order_by("-created_at").values("health")[:1]
        return (
            Project.objects.filter(workspace__memberships__user=self.request.user)
            .select_related("workspace", "lead")
            .annotate(
                open_task_count=Count(
                    "tasks",
                    filter=Q(tasks__status__in=_OPEN_STATUSES),
                    distinct=True,
                ),
                member_count=Count("members", distinct=True),
                latest_health=Subquery(latest),
            )
            .order_by("archived", "workspace__name", "name")
            .distinct()
        )

    def get_context_data(self, **kwargs):
        """Expose the user's favourite project ids so each card can
        render its star toggle in the correct (starred / unstarred)
        state without an N+1 lookup."""
        ctx = super().get_context_data(**kwargs)
        ctx["favourite_project_ids"] = set(
            self.request.user.favourite_projects.values_list("id", flat=True),
        )
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
        if _is_htmx_partial(self.request):
            return ["web/projects/_view_panel_wrapper.html"]
        return ["web/projects/detail.html"]

    def get_object(self, queryset=None):
        """Resolve the project by slug_prefix and enforce membership."""
        slug_prefix = self.kwargs["slug_prefix"]
        return get_object_or_404(
            Project.objects.filter(
                slug_prefix=slug_prefix,
                workspace__memberships__user=self.request.user,
            ).select_related("workspace", "lead"),
        )

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
        ctx["description_html"] = render_markdown(project.description) if project.description else ""
        ctx["members"] = list(
            project.members.order_by("first_name", "last_name", "username"),
        )
        ctx["workspace_members"] = _project_workspace_members(project, exclude_user=None)
        ctx["picker_icons"] = PROJECT_ICONS
        ctx["picker_icon_colors"] = PROJECT_ICON_COLORS

        base = (
            Task.objects.filter(project=project)
            .select_related("assignee", "reporter", "parent", "project__workspace")
            .prefetch_related("labels")
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
        if not table_only:
            kanban_tasks = list(base.order_by("status", "-priority", "-updated_at"))
            ctx["tasks"] = table_tasks if view_mode == "table" else kanban_tasks
            columns = []
            for status in Task.STATUS_VALUES:
                columns.append(
                    {
                        "key": status,
                        "label": Task.STATUS_LABELS[status],
                        "tasks": [t for t in kanban_tasks if t.status == status],
                    },
                )
            ctx["columns"] = columns
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
                hide_workspace=True,
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
            _user_task_qs(self.request.user).select_related("reporter", "parent"),
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
        ctx["comments"] = list(
            task.comments.select_related("author").order_by("created_at"),
        )
        ctx["activity"] = _task_activity(task)
        non_comment_activity = [e for e in ctx["activity"] if not (e.event_type or "").startswith("comment.")]
        ctx["timeline"] = sorted(
            [("comment", c) for c in ctx["comments"]] + [("event", e) for e in non_comment_activity],
            key=lambda kv: kv[1].created_at,
        )
        ctx["status_labels"] = Task.STATUS_LABELS
        ctx["priority_labels"] = dict(Task.PRIORITY_CHOICES)
        ctx["workspace_members"] = _workspace_members(task)
        ctx["workspace_labels"] = _workspace_labels(task)
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
    comments = list(task.comments.select_related("author").order_by("created_at"))
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
    comments = list(task.comments.select_related("author").order_by("created_at"))
    activity = _task_activity(task)
    non_comment_activity = [e for e in activity if not (e.event_type or "").startswith("comment.")]
    timeline = sorted(
        [("comment", c) for c in comments] + [("event", e) for e in non_comment_activity],
        key=lambda kv: kv[1].created_at,
    )
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
            | Q(target_type=ActivityLog.TARGET_COMMENT, payload__task_id=task.id),
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
    label_names = {}
    if label_ids:
        label_names = dict(Label.objects.filter(id__in=label_ids).values_list("id", "name"))
    for e in events:
        if e.event_type == "task.assigned" and e.payload:
            e.assigned_from_name = user_names.get(e.payload.get("from_user_id"))
            e.assigned_to_name = user_names.get(e.payload.get("to_user_id"))
        elif e.event_type == "task.labels_changed" and e.payload:
            e.added_label_names = [label_names.get(lid, f"#{lid}") for lid in (e.payload.get("added_ids") or [])]
            e.removed_label_names = [label_names.get(lid, f"#{lid}") for lid in (e.payload.get("removed_ids") or [])]
    return events


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
            "timeline": _build_timeline(task),
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
        },
        request=request,
    )
    return HttpResponse(primary_html + activity_html)


def _build_timeline(task):
    """Merge comments + non-comment activity events into the unified
    timeline tuple list consumed by ``_task_detail_timeline.html``."""
    comments = list(task.comments.select_related("author").order_by("created_at"))
    activity = _task_activity(task)
    non_comment = [e for e in activity if not (e.event_type or "").startswith("comment.")]
    return sorted(
        [("comment", c) for c in comments] + [("event", e) for e in non_comment],
        key=lambda kv: kv[1].created_at,
    )


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
    _apply_task_field_change(task, "status", new_status, request.user)
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
            "timeline": _build_timeline(task),
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
            "timeline": _build_timeline(task),
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
        },
        request=request,
    )
    return HttpResponse(trigger_html + dropdown_html + activity_html)


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
        {"project": project, "is_favourite": is_favourite},
        request=request,
    )
    from apps.web.nav import get_nav_workspaces

    nav = get_nav_workspaces(user)
    sidebar_oob = render_to_string(
        "web/_sidebar_favourites_oob.html",
        {
            "nav_workspaces": nav,
            "nav_has_favourites": any(ws.favourite_projects for ws in nav),
        },
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
            "attached_label_ids": set(task.labels.values_list("id", flat=True)),
        },
    )


@require_POST
@login_required
def post_comment(request, slug_prefix, number):
    """Create a comment on the task and return the new comment fragment.

    Designed for HTMX append-only flow: the response renders a single
    ``<li>`` that gets inserted at the end of the comments list. Emits
    a ``comment.created`` activity event via :func:`log_event`.

    Args:
        request: Django request carrying a ``body`` form field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_comment.html`` for the new comment, or 400 if the
        body is empty.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    body = (request.POST.get("body") or "").strip()
    if not body:
        return HttpResponseBadRequest("body required")
    comment = Comment.objects.create(task=task, author=request.user, body=body)
    log_event(
        workspace=task.project.workspace,
        project=task.project,
        actor=request.user,
        event_type="comment.created",
        target_type=ActivityLog.TARGET_COMMENT,
        target_id=comment.id,
        payload={"task_id": task.id, "body_preview": body[:120]},
    )
    return _inline_edit_response(
        request,
        task,
        "web/projects/_comment.html",
        {"comment": comment},
    )


def _user_accessible_projects(user):
    """Return projects the user can post tasks to, with workspace eager-loaded.

    Args:
        user: The acting :class:`User`.

    Returns:
        A queryset of :class:`Project` rows ordered by workspace name,
        then project name. Empty workspaces don't surface (no projects).
    """
    return (
        Project.objects.filter(workspace__memberships__user=user)
        .select_related("workspace")
        .order_by("workspace__name", "name")
        .distinct()
    )


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
    projects = list(_user_accessible_projects(request.user))
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
        _user_accessible_projects(request.user),
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
        # ``HX-Redirect`` alone — no companion ``acta:task-created``
        # event. Firing the event in the same response causes panel
        # listeners on the current page (kanban / table / my-work) to
        # refetch their fragment in the brief moment before the browser
        # navigates, which flashes a stale-then-fresh view through the
        # closing modal. The real fix for "open task feels slow" lives
        # in [[project-todo-task-modal]] — partial swap, no reload.
        response["HX-Redirect"] = detail_url
    else:
        # No redirect: stay on the current page, but tell the page to
        # refresh its task list. Connected HTMX listeners on
        # ``acta:task-created`` re-fetch their fragment; the modal
        # picks up the same event and closes itself.
        response["HX-Trigger"] = "acta:task-created"
    return response
