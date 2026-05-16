"""Shared task-list filter logic.

Used by AllTasksView, MyWorkView, and ProjectDetailView — they all
filter the same kind of Task queryset by the same set of querystring
params. This module centralises the parsing + filter-application so
the three views share one canonical implementation.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from apps.labels.models import Label
from apps.projects.models import Project
from apps.tasks.models import Task


def apply_task_filters(qs, params, *, request_user, default_show_done=False):
    """Apply querystring filters to a Task queryset.

    Args:
        qs: Base Task queryset (already scoped).
        params: ``request.GET``-like mapping with ``getlist``.
        request_user: For the ``assignee=me`` filter shortcut.
        default_show_done: When True, done tasks are NOT excluded by
            default — caller has to opt out via explicit ``status``.
            All Tasks defaults False (hide done unless an explicit
            ``?status=done`` is chosen); per-project Kanban and My Work
            default True (the page's structure already shows done by
            design).
    """
    statuses = params.getlist("status")
    if statuses:
        qs = qs.filter(status__in=statuses)
    elif not default_show_done:
        qs = qs.exclude(status=Task.STATUS_DONE)

    priorities = params.getlist("priority")
    if priorities:
        try:
            qs = qs.filter(priority__in=[int(p) for p in priorities])
        except (TypeError, ValueError):
            pass

    project_ids = params.getlist("project")
    if project_ids:
        try:
            qs = qs.filter(project_id__in=[int(p) for p in project_ids])
        except (TypeError, ValueError):
            pass

    workspace_ids = params.getlist("workspace")
    if workspace_ids:
        try:
            qs = qs.filter(project__workspace_id__in=[int(w) for w in workspace_ids])
        except (TypeError, ValueError):
            pass

    assignees = params.getlist("assignee")
    if assignees:
        q_assignee = Q()
        user_ids = []
        for a in assignees:
            if a == "me":
                q_assignee |= Q(assignee=request_user)
            elif a == "unassigned":
                q_assignee |= Q(assignee__isnull=True)
            else:
                try:
                    user_ids.append(int(a))
                except (TypeError, ValueError):
                    pass
        if user_ids:
            q_assignee |= Q(assignee_id__in=user_ids)
        qs = qs.filter(q_assignee)

    label_ids = params.getlist("label")
    if label_ids:
        try:
            qs = qs.filter(labels__id__in=[int(i) for i in label_ids]).distinct()
        except (TypeError, ValueError):
            pass

    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))

    return qs


def filter_sidebar_context(
    request,
    *,
    available_projects=None,
    available_workspaces=None,
    available_labels=None,
    available_assignees=None,
    hide_assignee=False,
    hide_workspace=False,
    hide_project=False,
    preserved_params=None,
    form_url=None,
    htmx_target=None,
):
    """Build the context dict the ``_filters_sidebar.html`` partial expects.

    Args:
        request: The active ``HttpRequest``.
        available_projects / workspaces / labels: Optional querysets /
            lists. If ``None``, computed from the user's accessible
            data.
        hide_assignee / hide_workspace / hide_project: Sections the
            sidebar should not render (e.g. assignee on My Work,
            project on per-project view).
        form_url: Action URL for the filter form. Defaults to the
            current path.
        htmx_target: CSS selector for the HTMX swap target.

    Returns:
        Dict that should be merged into the view's context.
    """
    user = request.user
    params = request.GET

    if available_projects is None:
        available_projects = list(
            Project.objects.filter(workspace__memberships__user=user)
            .select_related("workspace")
            .order_by("workspace__name", "name")
            .distinct(),
        )
    if available_workspaces is None:
        available_workspaces = list(user.workspaces.order_by("name").distinct())
    if available_labels is None:
        available_labels = list(
            Label.objects.filter(workspace__memberships__user=user).order_by("name").distinct(),
        )
    if available_assignees is None:
        User = get_user_model()
        available_assignees = list(
            User.objects.filter(
                workspace_memberships__workspace__memberships__user=user,
            )
            .order_by("username")
            .distinct(),
        )

    selected_statuses = set(params.getlist("status"))
    selected_priorities = {int(p) for p in params.getlist("priority") if p.isdigit()}
    selected_projects = {int(p) for p in params.getlist("project") if p.isdigit()}
    selected_workspaces = {int(w) for w in params.getlist("workspace") if w.isdigit()}
    selected_labels = {int(i) for i in params.getlist("label") if i.isdigit()}
    selected_assignees = set(params.getlist("assignee"))
    q = params.get("q", "")

    active_filter_count = (
        (1 if q else 0)
        + len(selected_assignees)
        + len(selected_statuses)
        + len(selected_priorities)
        + len(selected_workspaces)
        + len(selected_projects)
        + len(selected_labels)
    )

    preserved_pairs = []
    for key in preserved_params or ():
        for value in params.getlist(key):
            preserved_pairs.append((key, value))

    return {
        "filter_form_url": form_url or request.path,
        "filter_htmx_target": htmx_target or "#task-list-wrapper",
        "filter_preserved_pairs": preserved_pairs,
        "filter_hide_assignee": hide_assignee,
        "filter_hide_workspace": hide_workspace,
        "filter_hide_project": hide_project,
        "selected_statuses": selected_statuses,
        "selected_priorities": selected_priorities,
        "selected_projects": selected_projects,
        "selected_workspaces": selected_workspaces,
        "selected_labels": selected_labels,
        "selected_assignees": selected_assignees,
        "q": q,
        "available_projects": available_projects,
        "available_workspaces": available_workspaces,
        "available_labels": available_labels,
        "available_assignees": available_assignees,
        "active_filter_count": active_filter_count,
        "status_labels": Task.STATUS_LABELS,
        "priority_labels": dict(Task.PRIORITY_CHOICES),
        "today": timezone.localdate(),
    }
