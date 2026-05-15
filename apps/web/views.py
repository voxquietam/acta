"""Server-rendered page views.

Per docs/decisions/0014-frontend-architecture.md, page views return
rendered Django templates; HTMX handles inline updates from the same
endpoints (or from `/api/v1/...` for JSON-only consumers).
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, OuterRef, Q, Subquery
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView, TemplateView

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.comments.models import Comment
from apps.projects.models import Project, ProjectUpdate
from apps.tasks.events import emit_task_diff_events, snapshot_task
from apps.tasks.models import Task
from apps.workspaces.models import WorkspaceMember

_OPEN_STATUSES = [
    Task.STATUS_PLANNED,
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]


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

    def get_context_data(self, **kwargs):
        """Attach the prefetched task list and pick the active view mode."""
        ctx = super().get_context_data(**kwargs)
        view_mode = self.request.GET.get("view", "kanban")
        if view_mode not in {"kanban", "table"}:
            view_mode = "kanban"
        ctx["view_mode"] = view_mode

        tasks = list(
            Task.objects.filter(project=self.object)
            .select_related("assignee", "reporter", "parent", "project")
            .prefetch_related("labels")
            .order_by("status", "-priority", "-updated_at"),
        )
        ctx["tasks"] = tasks
        ctx["status_labels"] = Task.STATUS_LABELS
        ctx["priority_labels"] = dict(Task.PRIORITY_CHOICES)

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
        return ctx


def _task_activity(task, limit=25):
    """Return the recent activity events relevant to a single task.

    Includes:
        * Events whose ``target_type='task'`` and ``target_id=task.id``.
        * ``comment.*`` events whose ``payload.task_id`` matches.
          Using the payload (instead of joining through the comments
          table) means an event remains visible on the task even after
          the underlying comment row is deleted.

    Args:
        task: The :class:`Task` whose feed to load.
        limit: Maximum number of events to return.

    Returns:
        A list of :class:`ActivityLog` rows, newest first.
    """
    return list(
        ActivityLog.objects.filter(
            Q(target_type=ActivityLog.TARGET_TASK, target_id=task.id)
            | Q(target_type=ActivityLog.TARGET_COMMENT, payload__task_id=task.id),
        )
        .select_related("actor")
        .order_by("-created_at")[:limit],
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
        {"activity": _task_activity(task)},
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
    from django.db import transaction

    with transaction.atomic():
        old = snapshot_task(task)
        setattr(task, field, value)
        task.save()
        emit_task_diff_events(old_state=old, task=task, actor=actor)


@require_POST
def set_task_status(request, slug_prefix, number):
    """Inline status change; returns the new status badge fragment.

    Args:
        request: DRF/Django request carrying a ``status`` form field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_status_cell.html`` with the updated task.
    """
    if not request.user.is_authenticated:
        return HttpResponseBadRequest("auth required")
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
def set_task_priority(request, slug_prefix, number):
    """Inline priority change; returns the priority cell fragment.

    Args:
        request: Django request carrying a ``priority`` form field.
        slug_prefix: Project slug prefix from the URL.
        number: Task number within the project.

    Returns:
        Rendered ``_priority_cell.html`` with the updated task.
    """
    if not request.user.is_authenticated:
        return HttpResponseBadRequest("auth required")
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
    if not request.user.is_authenticated:
        return HttpResponseBadRequest("auth required")
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
