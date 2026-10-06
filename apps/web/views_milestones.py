"""Views for milestones — the workspace list, one date's page, and closing.

A milestone is a point: one target date, scoped to one or more projects
(ADR 0037). The model stores the date, the scope, the goal and the fact
that someone closed it; every reading on these pages comes from
``apps.milestones.services``, which the MCP tools read too, so the two
surfaces cannot disagree about the same date.

Who may do what: attaching work, creating, editing and closing are
member actions — planning is collaborative here and every write lands in
the activity log. Deleting is admin-only: it detaches whatever is
attached, and that is the one step in this module nobody can undo.
"""

import datetime
import json

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.milestones import services
from apps.milestones.models import Milestone
from apps.tasks.bulk import _run_bulk_update
from apps.tasks.models import Task, counted_q
from apps.web.nav import resolve_active_workspace
from apps.web.views import _user_accessible_projects, _user_is_workspace_admin


def _workspace_milestones(workspace):
    """Return the milestone queryset for a workspace, scope prefetched.

    Args:
        workspace: The workspace, or ``None`` for a user with none.

    Returns:
        A queryset ordered by target date, empty when there is no
        workspace in view.
    """
    if workspace is None:
        return Milestone.objects.none()
    return (
        Milestone.objects.filter(workspace=workspace)
        .select_related("owner")
        .prefetch_related("projects")
        .order_by(
            "target_date",
            "id",
        )
    )


def _get_milestone_or_404(request, pk):
    """Load a milestone in a workspace the user belongs to.

    404 and not 403 on a foreign one, for the reason the workspace
    resolver gives: a 403 confirms the milestone exists to anyone
    guessing ids.

    Args:
        request: The active request.
        pk: Milestone primary key.

    Returns:
        The :class:`~apps.milestones.models.Milestone`.
    """
    return get_object_or_404(
        Milestone.objects.select_related("owner", "workspace").prefetch_related("projects"),
        pk=pk,
        workspace__memberships__user=request.user,
    )


@login_required
def milestones_overview(request):
    """The Milestones tab — every date this workspace aims at.

    Grouped by month with the past folded away, because the question the
    page answers is "what is next", and a name tells you nothing about
    that. Each row carries its own progress, the projects in scope with
    their slice, and how much of its work will not make the date.
    """
    workspace = resolve_active_workspace(request)
    today = timezone.localdate()
    rows = services.workspace_rows(workspace, today) if workspace is not None else []
    upcoming = [row for row in rows if not row["is_past"]]
    return render(
        request,
        "web/milestones/overview.html",
        {
            "workspace": workspace,
            "rows": rows,
            "groups": services.group_rows(rows, today),
            "total_count": len(rows),
            "upcoming_count": len(upcoming),
            "shared_count": sum(1 for row in rows if len(row["projects"]) > 1),
            "today": today,
        },
    )


@login_required
def milestone_detail(request, pk):
    """One milestone's page: progress, burndown, risk, and its work.

    The three readings sit side by side on purpose. Progress says how
    much is done, the burndown says whether that pace lands before the
    date, and the at-risk list names the work that will not — a number
    alone has never moved anything.
    """
    milestone = _get_milestone_or_404(request, pk)
    today = timezone.localdate()
    context = services.detail_context(milestone, today)
    targets = _close_targets(milestone, context, today)
    fitting = next((row for row in targets if row["fits"]), None)
    context.update(
        {
            "workspace": milestone.workspace,
            "can_delete": _user_is_workspace_admin(request.user, milestone.workspace),
            "close_targets": targets,
            # The dialog opens on the soonest milestone that can actually
            # take the work, and on "detach" when none can.
            "close_default_target": fitting["milestone"].pk if fitting else "",
            "close_default_mode": "move" if fitting else "leave",
            "today": today,
        },
    )
    context.update(_burndown_json(context["burndown"]))
    return render(request, "web/milestones/detail.html", context)


def _burndown_json(burndown) -> dict:
    """Serialise the burndown series for the chart in the template.

    Args:
        burndown: The series from
            :func:`apps.milestones.services.burndown`, or ``None``.

    Returns:
        A context dict of JSON blobs, empty when there is no chart to
        draw.
    """
    if burndown is None:
        return {}
    return {
        f"burndown_{key}_json": json.dumps(burndown[key])
        for key in (
            "labels",
            "scope",
            "remaining",
            "ideal",
            "projection",
        )
    }


def _close_targets(milestone, context, today) -> list[dict]:
    """Return the milestones the open work could move to on closing.

    A target is open, unfinished and ahead — closing into a milestone
    that is itself overdue only moves the problem. Each row says how
    much of the open work its scope actually covers, because a task can
    only sit in a milestone that covers its project, and a target that
    fits none of it is offered disabled rather than hidden: the absence
    is the answer.

    Args:
        milestone: The milestone being closed.
        context: Its detail context, for the open work.
        today: Reference date.

    Returns:
        Row dicts with ``milestone``, ``fits`` and ``late_there``.
    """
    open_tasks = [row["task"] for group in context["groups"] for row in group["tasks"] if _is_open(row["task"])]
    if not open_tasks:
        return []
    candidates = (
        Milestone.objects.filter(
            workspace_id=milestone.workspace_id,
            closed_at__isnull=True,
            target_date__gte=today,
        )
        .exclude(pk=milestone.pk)
        .prefetch_related("projects")
        .order_by(
            "target_date",
            "id",
        )[:10]
    )
    rows = []
    for candidate in candidates:
        scope = {project.id for project in candidate.projects.all()}
        fits = [task for task in open_tasks if task.project_id in scope]
        rows.append(
            {
                "milestone": candidate,
                "fits": len(fits),
                "total": len(open_tasks),
                "late_there": sum(1 for task in fits if task.due_date and task.due_date > candidate.target_date),
            },
        )
    return rows


def _is_open(task) -> bool:
    """Return whether a task is counted work that is not done yet.

    Args:
        task: The task to judge.

    Returns:
        ``True`` when it still has to happen.
    """
    if task.status in (Task.STATUS_DONE, Task.STATUS_CANCELLED):
        return False
    return task.archived_at is None


@require_POST
@login_required
def milestone_close(request, pk):
    """Close a milestone, saying where its unfinished work goes.

    Closing is the one stored state: *reached* either happened or it did
    not, and no amount of finished work decides it. Unfinished work does
    not vanish with the date — the caller says ``move_to`` (a milestone
    whose scope covers it) or leaves it empty to detach, and the move
    runs through the bulk path so each task's history records where it
    went and why.
    """
    milestone = _get_milestone_or_404(request, pk)
    if milestone.is_closed:
        return redirect(_detail_path(milestone))
    raw_target = (request.POST.get("move_to") or "").strip()
    target = None
    if raw_target:
        try:
            target = Milestone.objects.get(pk=int(raw_target), workspace_id=milestone.workspace_id)
        except (TypeError, ValueError, Milestone.DoesNotExist):
            return HttpResponseBadRequest("invalid target milestone")
    open_ids = list(
        milestone.tasks.filter(counted_q()).exclude(status=Task.STATUS_DONE).values_list("id", flat=True),
    )
    moved = 0
    with transaction.atomic():
        if open_ids:
            if target is not None:
                scope = set(target.projects.values_list("id", flat=True))
                fitting = list(
                    milestone.tasks.filter(id__in=open_ids, project_id__in=scope).values_list("id", flat=True),
                )
            else:
                fitting = open_ids
            if fitting:
                try:
                    _run_bulk_update(
                        user=request.user,
                        ids=fitting,
                        updates={"milestone": target.pk if target else None},
                    )
                except PermissionError:
                    return HttpResponseForbidden("some of this work is out of reach")
                moved = len(fitting)
        milestone.closed_at = timezone.now()
        milestone.save(update_fields=["closed_at", "updated_at"])
        log_event(
            workspace=milestone.workspace,
            actor=request.user,
            event_type="milestone.closed",
            target_type=ActivityLog.TARGET_MILESTONE,
            target_id=milestone.pk,
            payload={
                "name": milestone.name,
                "target_date": milestone.target_date.isoformat(),
                "open_work": len(open_ids),
                "moved": moved,
                "moved_to_id": target.pk if target else None,
                "moved_to_name": target.name if target else None,
            },
        )
    return redirect(_detail_path(milestone))


@require_POST
@login_required
def milestone_reopen(request, pk):
    """Reopen a closed milestone.

    Undoes the close, not the move: work that left on closing stays where
    it went, because it has been planned somewhere else since.
    """
    milestone = _get_milestone_or_404(request, pk)
    if milestone.is_closed:
        with transaction.atomic():
            milestone.closed_at = None
            milestone.save(update_fields=["closed_at", "updated_at"])
            log_event(
                workspace=milestone.workspace,
                actor=request.user,
                event_type="milestone.reopened",
                target_type=ActivityLog.TARGET_MILESTONE,
                target_id=milestone.pk,
                payload={
                    "name": milestone.name,
                    "target_date": milestone.target_date.isoformat(),
                },
            )
    return redirect(_detail_path(milestone))


@login_required
def milestone_editor(request, pk=None):
    """Create or edit a milestone in the modal the task dialog uses.

    Narrowing the scope of a milestone that already holds work from the
    dropped project is the one destructive edit here, so it is never
    silent: the form reports how many tasks each dropped project holds,
    and the caller picks what happens to them — ``scope_action_<prefix>``
    is ``move`` with a ``scope_move_<prefix>`` target, or ``detach``.

    Returns:
        The rendered modal on ``GET``, a ``204`` carrying
        ``acta:milestone-changed`` on a successful ``POST``, or the modal
        again with errors.
    """
    workspace = resolve_active_workspace(request)
    if workspace is None:
        return HttpResponseBadRequest("no workspace in view")
    milestone = _get_milestone_or_404(request, pk) if pk is not None else None
    projects = list(_user_accessible_projects(request.user, workspace).order_by("slug_prefix"))
    if request.method == "POST":
        return _save_milestone(request, workspace, milestone, projects)
    selected = (
        [project.id for project in milestone.projects.all()]
        if milestone is not None
        else _preselected_projects(request, projects)
    )
    return render(
        request,
        "web/milestones/_editor.html",
        _editor_context(
            request,
            workspace,
            milestone,
            projects,
            {
                "name": milestone.name if milestone else "",
                "goal": milestone.goal if milestone else "",
                "description": milestone.description if milestone else "",
                "target_date": (
                    milestone.target_date if milestone else timezone.localdate() + datetime.timedelta(days=30)
                ).isoformat(),
                "owner": milestone.owner_id if milestone else request.user.id,
                "projects": selected,
            },
        ),
    )


def _preselected_projects(request, projects) -> list[int]:
    """Return the scope a new milestone starts with.

    Opened from inside a project, that project is the scope — a local
    checkpoint is the common case, and the person is already standing in
    the project it belongs to.

    Args:
        request: The active request, for a ``?project=`` prefix.
        projects: The projects the user may pick from.

    Returns:
        A list of project ids, possibly empty.
    """
    prefix = (request.GET.get("project") or "").strip().upper()
    if not prefix:
        return []
    return [project.id for project in projects if project.slug_prefix == prefix]


def _editor_context(request, workspace, milestone, projects, form: dict, errors=None) -> dict:
    """Build the editor modal's context.

    Args:
        request: The active request.
        workspace: The workspace in view.
        milestone: The milestone being edited, or ``None`` on create.
        projects: Projects the user may put in scope.
        form: The values to render in the fields.
        errors: Field errors to show, if any.

    Returns:
        The context the editor template renders.
    """
    today = timezone.localdate()
    selected = set(form["projects"])
    attached = _attached_per_project(milestone)
    rows = [
        {
            "project": project,
            "selected": project.id in selected,
            "attached": attached.get(project.id, 0),
        }
        for project in projects
    ]
    return {
        "workspace": workspace,
        "milestone": milestone,
        "form": form,
        "errors": errors or {},
        "project_rows": rows,
        "members": list(
            get_user_model()
            .objects.filter(workspace_memberships__workspace=workspace)
            .order_by("first_name", "last_name", "username"),
        ),
        "conflicts": _scope_conflicts(milestone, attached, today) if milestone is not None else [],
        "today": today,
    }


def _attached_per_project(milestone) -> dict[int, int]:
    """Count a milestone's attached work per project in one query.

    Args:
        milestone: The milestone, or ``None`` on create.

    Returns:
        ``{project_id: attached}``, empty when there is no milestone yet.
    """
    if milestone is None:
        return {}
    rows = milestone.tasks.values("project_id").annotate(n=Count("id"))
    return {row["project_id"]: row["n"] for row in rows}


def _scope_conflicts(milestone, attached: dict[int, int], today) -> list[dict]:
    """Prepare the question each in-scope project would raise if unticked.

    A task may only sit in a milestone that covers its project, so
    dropping a project detaches its work. The form asks before the save
    rather than reporting after it, and the asking is live — the block
    for a project appears the moment its tick comes off — so every
    in-scope project that holds work gets one prepared here, whether or
    not it is currently ticked.

    Args:
        milestone: The milestone being edited.
        attached: Attached work per project, from
            :func:`_attached_per_project`.
        today: Reference date.

    Returns:
        One dict per in-scope project that holds attached work.
    """
    conflicts = []
    for project in milestone.projects.all():
        held = attached.get(project.id, 0)
        if not held:
            continue
        targets = list(
            Milestone.objects.filter(
                workspace_id=milestone.workspace_id,
                projects=project,
                closed_at__isnull=True,
                target_date__gte=today,
            )
            .exclude(pk=milestone.pk)
            .order_by(
                "target_date",
                "id",
            )[:10],
        )
        conflicts.append(
            {
                "project": project,
                "attached": held,
                "targets": targets,
            },
        )
    return conflicts


def _save_milestone(request, workspace, milestone, projects):
    """Validate and write the editor's form.

    Args:
        request: The ``POST`` request.
        workspace: The workspace in view.
        milestone: The milestone being edited, or ``None`` on create.
        projects: Projects the user may put in scope.

    Returns:
        A ``204`` with the refresh trigger, or the modal re-rendered with
        errors.
    """
    allowed = {project.id: project for project in projects}
    form = {
        "name": (request.POST.get("name") or "").strip(),
        "goal": (request.POST.get("goal") or "").strip(),
        "description": (request.POST.get("description") or "").strip(),
        "target_date": (request.POST.get("target_date") or "").strip(),
        "owner": _int_or_none(request.POST.get("owner")),
        "projects": [pid for pid in _int_list(request.POST.getlist("projects")) if pid in allowed],
    }
    errors = {}
    if not form["name"]:
        errors["name"] = _("A milestone needs a name.")
    target_date = None
    try:
        target_date = datetime.date.fromisoformat(form["target_date"])
    except ValueError:
        errors["target_date"] = _("Pick the date this has to be true by.")
    if not form["projects"]:
        errors["projects"] = _("Pick at least one project. Scope is what lets tasks attach.")
    if milestone is None and target_date is not None and target_date < timezone.localdate():
        errors["target_date"] = _(
            "A milestone is a date ahead. Recording one already reached? Create it, then close it.",
        )
    if errors:
        # 200 and not 422: HTMX drops the body of an error response, so a
        # 4xx here would leave the modal standing with nothing said.
        return render(
            request,
            "web/milestones/_editor.html",
            _editor_context(request, workspace, milestone, projects, form, errors),
        )
    owner = None
    if form["owner"] is not None:
        owner = get_user_model().objects.filter(pk=form["owner"], workspace_memberships__workspace=workspace).first()
    with transaction.atomic():
        created = milestone is None
        if created:
            milestone = Milestone(workspace=workspace)
        milestone.name = form["name"]
        milestone.goal = form["goal"]
        milestone.description = form["description"]
        milestone.target_date = target_date
        milestone.owner = owner
        milestone.save()
        dropped = _apply_scope(request, milestone, form["projects"])
        log_event(
            workspace=workspace,
            actor=request.user,
            event_type="milestone.created" if created else "milestone.updated",
            target_type=ActivityLog.TARGET_MILESTONE,
            target_id=milestone.pk,
            payload={
                "name": milestone.name,
                "target_date": milestone.target_date.isoformat(),
                "projects": sorted(allowed[pid].slug_prefix for pid in form["projects"]),
                "detached": dropped,
            },
        )
    response = HttpResponse(status=204)
    response["HX-Trigger"] = "acta:milestone-changed"
    response["HX-Redirect"] = _detail_path(milestone)
    return response


def _detail_path(milestone) -> str:
    """Return the URL of a milestone's page, workspace-scoped when possible.

    Args:
        request: The active request.
        milestone: The milestone to link to.

    Returns:
        The path of its detail page.
    """
    from django.urls import reverse

    return reverse(
        "web_ws:milestone_detail",
        kwargs={
            "workspace": milestone.workspace.slug,
            "pk": milestone.pk,
        },
    )


def _apply_scope(request, milestone, project_ids: list[int]) -> int:
    """Set the milestone's scope, handling the work a narrowing drops.

    Args:
        request: The ``POST`` request, carrying the per-project choice.
        milestone: The milestone being saved.
        project_ids: The project ids that stay in scope.

    Returns:
        How many tasks left the milestone because their project did.
    """
    keeping = set(project_ids)
    dropped_projects = [project for project in milestone.projects.all() if project.id not in keeping]
    detached = 0
    for project in dropped_projects:
        ids = list(milestone.tasks.filter(project=project).values_list("id", flat=True))
        if not ids:
            continue
        action = (request.POST.get(f"scope_action_{project.slug_prefix}") or "detach").strip()
        target_id = _int_or_none(request.POST.get(f"scope_move_{project.slug_prefix}")) if action == "move" else None
        if (
            target_id is not None
            and not Milestone.objects.filter(
                pk=target_id,
                workspace_id=milestone.workspace_id,
                projects=project,
            ).exists()
        ):
            target_id = None
        _run_bulk_update(user=request.user, ids=ids, updates={"milestone": target_id})
        detached += len(ids)
    milestone.projects.set(project_ids)
    return detached


@require_POST
@login_required
def milestone_delete(request, pk):
    """Delete a milestone; its tasks keep everything but the milestone.

    Admin-only, and the one irreversible action here. ``SET_NULL`` on the
    task side is the whole policy: deleting a date must never delete the
    work that aimed at it.
    """
    milestone = _get_milestone_or_404(request, pk)
    if not _user_is_workspace_admin(request.user, milestone.workspace):
        return HttpResponseForbidden("admin only")
    with transaction.atomic():
        log_event(
            workspace=milestone.workspace,
            actor=request.user,
            event_type="milestone.deleted",
            target_type=ActivityLog.TARGET_MILESTONE,
            target_id=milestone.pk,
            payload={
                "name": milestone.name,
                "target_date": milestone.target_date.isoformat(),
                "detached": milestone.tasks.count(),
            },
        )
        milestone.delete()
    return redirect(_overview_path(milestone.workspace))


def _overview_path(workspace) -> str:
    """Return the milestone list's URL for a workspace.

    Args:
        workspace: The workspace to link to.

    Returns:
        The path of its Milestones tab.
    """
    from django.urls import reverse

    return reverse("web_ws:milestones_overview", kwargs={"workspace": workspace.slug})


def _int_or_none(raw):
    """Parse an optional integer form field.

    Args:
        raw: The raw form value.

    Returns:
        The integer, or ``None`` when absent or unparsable.
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _int_list(values) -> list[int]:
    """Parse a repeated integer form field, dropping what will not parse.

    Args:
        values: The raw form values.

    Returns:
        The integers that parsed.
    """
    parsed = []
    for raw in values:
        value = _int_or_none(raw)
        if value is not None:
            parsed.append(value)
    return parsed
