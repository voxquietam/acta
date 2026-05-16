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
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView, TemplateView

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.comments.models import Comment
from apps.labels.models import Label
from apps.projects.models import Project, ProjectUpdate
from apps.tasks.events import emit_task_diff_events, snapshot_task
from apps.tasks.models import Task
from apps.web.filters import apply_task_filters, apply_task_ordering, filter_sidebar_context
from apps.workspaces.models import WorkspaceMember

User = get_user_model()

_OPEN_STATUSES = [
    Task.STATUS_PLANNED,
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]


def _resolve_view_mode(request, *, default):
    """Resolve view_mode in the canonical order.

    Order: ``?view=`` querystring → ``acta_view_mode`` cookie → page
    default. Anything that doesn't validate falls back to ``default``.
    Used by AllTasksView (default ``table``) and ProjectDetailView
    (default ``kanban``) so both share the same persistence flow.

    Args:
        request: The active ``HttpRequest``.
        default: Fallback mode for the page when neither querystring
            nor cookie carries a valid value.

    Returns:
        Either ``"kanban"`` or ``"table"``.
    """
    view_mode = request.GET.get("view")
    if view_mode in {"kanban", "table"}:
        return view_mode
    cookie_pref = request.COOKIES.get("acta_view_mode")
    return cookie_pref if cookie_pref in {"kanban", "table"} else default


def _user_task_qs(user):
    """Return the base queryset of tasks the request user can access.

    All web endpoints scoped to a single task funnel through this so the
    membership check and select/prefetch are consistent.

    Args:
        user: The acting :class:`User`.

    Returns:
        A queryset filtered to the user's workspaces, eager-loading
        project/workspace, assignee, reporter, parent and labels.
    """
    return (
        Task.objects.filter(project__workspace__memberships__user=user)
        .select_related(
            "project__workspace",
            "assignee",
            "reporter",
            "parent",
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


def _my_work_sections(user, params):
    """Group tasks assigned to ``user`` into deadline-aware buckets.

    Querystring filters (``params``) narrow the base queryset before
    bucketing — except the assignee filter, which is implicit (``me``).
    Done tasks reach the queryset via the page's own
    ``Q(status=DONE, updated_at>=cutoff)`` clause and are not stripped
    by :func:`apply_task_filters` (``default_show_done=True``). If the
    user picks specific statuses in the sidebar, those override.
    """
    today = timezone.localdate()
    week_end = today + datetime.timedelta(days=6)
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
    tasks = list(
        base.order_by(
            F("due_date").asc(nulls_last=True),
            "-priority",
            "-updated_at",
        ),
    )
    buckets = {
        "overdue": [],
        "today": [],
        "week": [],
        "later": [],
        "no_deadline": [],
        "recently_done": [],
    }
    for task in tasks:
        if task.status == Task.STATUS_DONE:
            buckets["recently_done"].append(task)
            continue
        if task.due_date is None:
            buckets["no_deadline"].append(task)
        elif task.due_date < today:
            buckets["overdue"].append(task)
        elif task.due_date == today:
            buckets["today"].append(task)
        elif task.due_date <= week_end:
            buckets["week"].append(task)
        else:
            buckets["later"].append(task)
    return [
        {"key": "overdue", "label": _("Overdue"), "tone": "rose", "tasks": buckets["overdue"]},
        {"key": "today", "label": _("Today"), "tone": "amber", "tasks": buckets["today"]},
        {"key": "week", "label": _("This week"), "tone": "violet", "tasks": buckets["week"]},
        {"key": "later", "label": _("Later"), "tone": "zinc", "tasks": buckets["later"]},
        {"key": "no_deadline", "label": _("No deadline"), "tone": "zinc", "tasks": buckets["no_deadline"]},
        {
            "key": "recently_done",
            "label": _("Recently done"),
            "tone": "emerald",
            "tasks": buckets["recently_done"],
        },
    ]


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
        """Full page on cold load, inner fragment for HTMX filter swaps."""
        if self.request.headers.get("HX-Request"):
            return ["web/_all_tasks_inner.html"]
        return ["web/all_tasks.html"]

    def get_queryset(self):
        """Filter the user's accessible tasks by querystring params.

        Kanban view groups by status and uses the fixed ordering
        ``(status, -priority, -updated_at)`` so columns are coherent;
        table view respects the ``?order=`` column click.
        """
        qs = _user_task_qs(self.request.user)
        qs = apply_task_filters(qs, self.request.GET, request_user=self.request.user)
        if _resolve_view_mode(self.request, default="table") == "kanban":
            return qs.order_by("status", "-priority", "-updated_at")
        return apply_task_ordering(qs, self.request.GET)

    def render_to_response(self, context, **response_kwargs):
        """Persist the resolved ``view_mode`` to a long-lived cookie."""
        response = super().render_to_response(context, **response_kwargs)
        response.set_cookie(
            "acta_view_mode",
            context.get("view_mode", "kanban"),
            max_age=60 * 60 * 24 * 365,
            samesite="Lax",
        )
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
        if view_mode == "kanban":
            tasks = list(ctx["tasks"])
            ctx["tasks"] = tasks
            ctx["columns"] = [
                {
                    "key": status,
                    "label": Task.STATUS_LABELS[status],
                    "tasks": [t for t in tasks if t.status == status],
                }
                for status in Task.STATUS_VALUES
            ]
        ctx.update(
            filter_sidebar_context(
                self.request,
                hide_assignee=True,
                hide_status=(view_mode == "kanban"),
                extra_preserved={"view": view_mode},
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
        if self.request.headers.get("HX-Request"):
            return ["web/_my_work_inner.html"]
        return ["web/my_work.html"]

    def get_context_data(self, **kwargs):
        """Build the bucketed sections + filter sidebar context."""
        ctx = super().get_context_data(**kwargs)
        ctx["sections"] = _my_work_sections(self.request.user, self.request.GET)
        ctx["has_any_tasks"] = any(section["tasks"] for section in ctx["sections"])
        ctx.update(
            filter_sidebar_context(
                self.request,
                hide_assignee=True,
                hide_project=True,
                htmx_target="#my-work-content",
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
        """Return user-accessible projects with annotated stats."""
        latest = ProjectUpdate.objects.filter(project=OuterRef("pk")).order_by("-created_at").values("health")[:1]
        return (
            Project.objects.filter(workspace__memberships__user=self.request.user)
            .select_related("workspace")
            .annotate(
                open_task_count=Count(
                    "tasks",
                    filter=Q(tasks__status__in=_OPEN_STATUSES),
                ),
                latest_health=Subquery(latest),
            )
            .order_by("archived", "workspace__name", "name")
            .distinct()
        )


class ProjectDetailView(LoginRequiredMixin, DetailView):
    """Project page with Kanban / Table view switching."""

    context_object_name = "project"

    def get_template_names(self):
        """Full page on cold load; only the panel fragment for HTMX swaps."""
        if self.request.headers.get("HX-Request"):
            return ["web/projects/_view_panel_wrapper.html"]
        return ["web/projects/detail.html"]

    def get_object(self, queryset=None):
        """Resolve the project by slug_prefix and enforce membership."""
        slug_prefix = self.kwargs["slug_prefix"]
        return get_object_or_404(
            Project.objects.filter(
                slug_prefix=slug_prefix,
                workspace__memberships__user=self.request.user,
            ).select_related("workspace"),
        )

    def render_to_response(self, context, **response_kwargs):
        """Persist the resolved ``view_mode`` to a long-lived cookie."""
        response = super().render_to_response(context, **response_kwargs)
        response.set_cookie(
            "acta_view_mode",
            context.get("view_mode", "kanban"),
            max_age=60 * 60 * 24 * 365,
            samesite="Lax",
        )
        return response

    def get_context_data(self, **kwargs):
        """Attach the filtered task list, columns, and filter sidebar context.

        View mode resolution order:
        1. ``?view=`` querystring — explicit user click on the toggle.
        2. ``acta_view_mode`` cookie — remembered choice from the
           previous project the user looked at.
        3. ``kanban`` — first-time default.

        The cookie is refreshed in :meth:`render_to_response` so every
        toggle sticks for the next project switch.
        """
        ctx = super().get_context_data(**kwargs)
        view_mode = _resolve_view_mode(self.request, default="kanban")
        ctx["view_mode"] = view_mode

        base = (
            Task.objects.filter(project=self.object)
            .select_related("assignee", "reporter", "parent", "project")
            .prefetch_related("labels")
        )
        base = apply_task_filters(base, self.request.GET, request_user=self.request.user)
        # Table view honors the ``?order=`` column click; kanban keeps a
        # fixed status grouping so cards inside each column stay sorted
        # by priority + recency regardless of the URL param.
        if view_mode == "table":
            base = apply_task_ordering(
                base,
                self.request.GET,
                default_ordering=("status", "-priority", "-updated_at"),
            )
        else:
            base = base.order_by("status", "-priority", "-updated_at")
        tasks = list(base)
        ctx["tasks"] = tasks

        columns = []
        for status in Task.STATUS_VALUES:
            columns.append(
                {
                    "key": status,
                    "label": Task.STATUS_LABELS[status],
                    "tasks": [t for t in tasks if t.status == status],
                },
            )
        ctx["columns"] = columns

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
                available_labels=list(
                    Label.objects.filter(workspace=self.object.workspace).order_by("name"),
                ),
                available_assignees=list(
                    self.object.workspace.members.exclude(pk=self.request.user.pk).order_by("username"),
                ),
            )
        )
        ctx["show_labels"] = True
        return ctx


class TaskDetailView(LoginRequiredMixin, DetailView):
    """Single-task page at ``/projects/<slug_prefix>/<number>/``."""

    context_object_name = "task"
    template_name = "web/projects/task_detail.html"

    def get_object(self, queryset=None):
        """Resolve the task by slug_prefix + number, 404 if foreign."""
        return _get_user_task_or_404(
            self.request.user,
            self.kwargs["slug_prefix"],
            self.kwargs["number"],
        )

    def get_context_data(self, **kwargs):
        """Attach subtasks, comments, and activity timeline."""
        ctx = super().get_context_data(**kwargs)
        task = self.object
        ctx["subtasks"] = list(
            task.subtasks.select_related("assignee").order_by("number"),
        )
        ctx["comments"] = list(
            task.comments.select_related("author").order_by("created_at"),
        )
        ctx["activity"] = _task_activity(task)
        ctx["status_labels"] = Task.STATUS_LABELS
        ctx["priority_labels"] = dict(Task.PRIORITY_CHOICES)
        ctx["workspace_members"] = _workspace_members(task)
        ctx["workspace_labels"] = _workspace_labels(task)
        ctx["attached_label_ids"] = set(task.labels.values_list("id", flat=True))
        return ctx


@login_required
def task_title_fragment(request, slug_prefix, number):
    """Render the title cell HTML for one task — SSE-triggered refresh."""
    task = _get_user_task_or_404(request.user, slug_prefix, number)
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
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
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
        .select_related("actor")
        .order_by("-created_at")[:limit],
    )
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
            "activity": _task_activity(task),
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
        },
        request=request,
    )
    return HttpResponse(primary_html + activity_html)


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
    """Inline description change; returns the description-cell fragment.

    Description is optional — an empty string is allowed (clears the
    description). No length cap beyond the model's ``TextField``. The
    delta is captured by the ``task.updated`` event under
    ``payload.changes.description`` (handled by
    :func:`build_diff_events`, which only stores the old/new lengths
    — not the full text — to keep activity payloads bounded).

    Args:
        request: Django request carrying a ``description`` form field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_description_cell.html`` with the updated task.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    # Empty string is valid (clears the description). No strip — the
    # editor produces canonical markdown and trailing whitespace can
    # be meaningful inside code blocks.
    new_description = request.POST.get("description", "")
    _apply_task_field_change(task, "description", new_description, request.user)
    return _inline_edit_response(
        request,
        task,
        "web/projects/_description_cell.html",
        {"task": task},
    )


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
            "activity": _task_activity(task),
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
