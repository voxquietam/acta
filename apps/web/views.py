"""Server-rendered page views.

Per docs/decisions/0014-frontend-architecture.md, page views return
rendered Django templates; HTMX handles inline updates from the same
endpoints (or from `/api/v1/...` for JSON-only consumers).
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.views.generic import DetailView, ListView, TemplateView

from apps.projects.models import Project, ProjectUpdate
from apps.tasks.models import Task
from apps.workspaces.models import WorkspaceMember

_OPEN_STATUSES = [
    Task.STATUS_PLANNED,
    Task.STATUS_TODO,
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
]


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
        """Return either the dashboard or the no-workspaces template.

        Returns:
            A list containing the chosen template path.
        """
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

        Returns:
            A queryset of :class:`Project` with extra ``open_task_count``
            and ``latest_health`` attributes per row.
        """
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
    """Project page with Kanban / Table view switching.

    The view mode is selected by ``?view=kanban`` (default) or
    ``?view=table``. Both modes render from the same prefetched task
    queryset so switching tabs does not re-query.
    """

    context_object_name = "project"

    def get_template_names(self):
        """Return the full page template, or just the tab panel for HTMX.

        HTMX tab switches set the ``HX-Request`` header; in that case we
        only render the view-panel fragment so the page chrome (sidebar,
        topbar, Tailwind CDN) does not reflow.

        Returns:
            A single-element list with the chosen template path.
        """
        if self.request.headers.get("HX-Request"):
            return ["web/projects/_view_panel_wrapper.html"]
        return ["web/projects/detail.html"]

    def get_object(self, queryset=None):
        """Resolve the project by ``slug_prefix`` and enforce membership.

        Args:
            queryset: Optional override; unused, kept for Django API.

        Returns:
            The :class:`Project` matching ``slug_prefix`` if the user
            has access. Raises 404 otherwise (no leak of existence).
        """
        slug_prefix = self.kwargs["slug_prefix"]
        return get_object_or_404(
            Project.objects.filter(
                slug_prefix=slug_prefix,
                workspace__memberships__user=self.request.user,
            ).select_related("workspace"),
        )

    def get_context_data(self, **kwargs):
        """Attach the prefetched task list and pick the active view mode.

        Returns:
            Context dict with ``project``, ``view_mode``, ``tasks``,
            ``columns`` (for kanban), and ``status_labels``.
        """
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
