"""Server-rendered page views.

Per docs/decisions/0014-frontend-architecture.md, page views return
rendered Django templates; HTMX handles inline updates from the same
endpoints (or from `/api/v1/...` for JSON-only consumers).
"""

import datetime

from django.contrib.auth import get_user_model
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
from apps.labels.models import Label
from apps.projects.models import Project, ProjectUpdate
from apps.tasks.events import emit_task_diff_events, snapshot_task
from apps.tasks.models import Task
from apps.workspaces.models import WorkspaceMember

User = get_user_model()

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
        ctx["workspace_members"] = _workspace_members(task)
        ctx["workspace_labels"] = _workspace_labels(task)
        ctx["attached_label_ids"] = set(task.labels.values_list("id", flat=True))
        return ctx


def _task_activity(task, limit=25):
    """Return the recent activity events relevant to a single task.

    Includes:
        * Events whose ``target_type='task'`` and ``target_id=task.id``.
        * ``comment.*`` events whose ``payload.task_id`` matches.
          Using the payload (instead of joining through the comments
          table) means an event remains visible on the task even after
          the underlying comment row is deleted.

    Attaches ``assigned_from_username`` and ``assigned_to_username`` to
    every ``task.assigned`` event, resolving the user ids in a single
    batched query so the template can show ``X → Y`` without per-row
    lookups.

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
    usernames = {}
    if user_ids:
        usernames = dict(User.objects.filter(id__in=user_ids).values_list("id", "username"))
    label_names = {}
    if label_ids:
        label_names = dict(Label.objects.filter(id__in=label_ids).values_list("id", "name"))
    for e in events:
        if e.event_type == "task.assigned" and e.payload:
            e.assigned_from_username = usernames.get(e.payload.get("from_user_id"))
            e.assigned_to_username = usernames.get(e.payload.get("to_user_id"))
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
    if not request.user.is_authenticated:
        return HttpResponseBadRequest("auth required")
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
    if not request.user.is_authenticated:
        return HttpResponseBadRequest("auth required")
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    new_title = (request.POST.get("title") or "").strip()
    if not new_title:
        return HttpResponseBadRequest("title required")
    if len(new_title) > 200:
        return HttpResponseBadRequest("title too long")
    _apply_task_field_change(task, "title", new_title, request.user)
    return _inline_edit_response(
        request,
        task,
        "web/projects/_title_cell.html",
        {"task": task},
    )


@require_POST
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
    from django.db import transaction

    if not request.user.is_authenticated:
        return HttpResponseBadRequest("auth required")
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
            "activity": _task_activity(task),
            "status_labels": Task.STATUS_LABELS,
            "priority_labels": dict(Task.PRIORITY_CHOICES),
        },
        request=request,
    )
    return HttpResponse(trigger_html + dropdown_html + activity_html)


@require_POST
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
    if not request.user.is_authenticated:
        return HttpResponseBadRequest("auth required")
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
    if not request.user.is_authenticated:
        return HttpResponseBadRequest("auth required")
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
