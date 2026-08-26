"""Views for meetings / calls — the ``/calls/`` page, CRUD, and the
per-task meetings panel.

A meeting is a first-class log of a call (see ADR 0030): when it happened,
how long it ran, who took part, free-form notes, and which tasks it
touched. The model lives in ``apps.meetings``; this module is the human
surface — a workspace-scoped list, a create/edit modal, delete, plus the
lazy fragment that renders the "Meetings" panel on a task's detail page and
a workspace-scoped task typeahead for the modal's link picker.

Meetings are scoped to the active workspace. Tasks they link to may come
from any project in that workspace, so the link picker searches the whole
workspace (mirroring ``task_link_search`` but not anchored to one task).
"""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.attachments.services import categorize, create_comment_attachment
from apps.comments.models import Comment
from apps.meetings.models import Meeting
from apps.notifications.services import notify_meeting_created
from apps.reactions.services import attach_reactions
from apps.tasks.search import search_tasks
from apps.web.nav import resolve_active_workspace
from apps.web.views import (
    _get_user_task_or_404,
    _is_htmx_partial,
    _is_workspace_admin,
    _user_accessible_projects,
    _user_task_qs,
)


def _comment_scope(request):
    """Return the comment-thread render scope ("page" or "m").

    The same meeting comment thread can be live in two places at once — the
    call detail page and the edit modal opened over it — so each render is
    namespaced to keep element ids unique. The triggering element echoes its
    scope back (a hidden ``scope`` POST field or a ``?scope=`` query) so the
    returned fragment's ids match the thread it belongs to. Anything other
    than the modal scope falls back to "page".
    """
    raw = request.POST.get("scope") or request.GET.get("scope") or ""
    return "m" if raw == "m" else "page"


def _accessible_meetings(request):
    """Return the meetings the user may see in the active workspace.

    Scoped to the active workspace. ``project`` / ``created_by`` are joined
    and ``participants`` / ``tasks`` prefetched so the list and rows render
    without an extra query per meeting.
    """
    active = resolve_active_workspace(request)
    if active is None:
        return Meeting.objects.none()
    return (
        Meeting.objects.filter(workspace=active)
        .select_related("project", "created_by")
        .prefetch_related("participants", "tasks__project")
        .order_by("-happened_at")
    )


def _changed_response():
    """``204`` that tells open lists/panels to re-fetch via ``acta:meeting-changed``.

    Every mutation (create / edit / delete) returns this; the calls list and
    each task's meetings panel listen for the event and refetch, so one path
    keeps every surface in sync and the modal closes.
    """
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = "acta:meeting-changed"
    return resp


def _list_context(request):
    """Filtered meetings + the project filter options and selected values."""
    active = resolve_active_workspace(request)
    base = _accessible_meetings(request)
    total_meetings = base.count()
    f_project = (request.GET.get("project") or "").strip()
    qs = base
    if f_project == "none":
        qs = qs.filter(project__isnull=True)
    elif f_project:
        qs = qs.filter(project__slug_prefix=f_project)
    projects = list(_user_accessible_projects(request.user, active)) if active else []
    return {
        "meetings": list(qs),
        "total_meetings": total_meetings,
        "filter_projects": projects,
        "f_project": f_project,
    }


@login_required
def calls_list(request):
    """List meetings for the active workspace.

    Full page on a cold load; the inner fragment on an HTMX request so the
    list refreshes live on a filter change or after a create / edit / delete
    (``acta:meeting-changed`` drives the re-fetch, carrying the active filter
    value via ``hx-include``).
    """
    ctx = _list_context(request)
    if _is_htmx_partial(request):
        return render(request, "web/meetings/_meetings_inner.html", ctx)
    return render(request, "web/meetings/meetings.html", ctx)


def _members(active):
    """Return the active workspace's members, name-ordered, or an empty list."""
    if active is None:
        return []
    return list(active.members.order_by("first_name", "last_name", "username"))


def _selected_tasks(request, meeting):
    """Return the tasks to pre-populate the editor's link picker.

    Edit mode: the meeting's already-linked tasks. Create mode: the task the
    modal was opened from (``?task=<id>``), when it resolves to one the user
    can access in the active workspace; otherwise empty.
    """
    if meeting is not None:
        return list(meeting.tasks.select_related("project").all())
    raw = (request.GET.get("task") or "").strip()
    if not raw.isdigit():
        return []
    task = _user_task_qs(request.user).select_related("project").filter(pk=int(raw)).first()
    return [task] if task is not None else []


def _editor_context(request, *, meeting=None):
    """Build the create/edit modal context."""
    active = resolve_active_workspace(request)
    selected_tasks = _selected_tasks(request, meeting)
    selected_participant_ids = list(meeting.participants.values_list("id", flat=True)) if meeting is not None else []
    return {
        "meeting": meeting,
        "obj": meeting or Meeting(),
        "projects": list(_user_accessible_projects(request.user, active)) if active else [],
        "members": _members(active),
        "selected_participant_ids": selected_participant_ids,
        "selected_tasks": selected_tasks,
        "comments": _meeting_comments(meeting, request.user) if meeting is not None else [],
        "now": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
    }


@login_required
def meeting_editor(request, pk=None):
    """Render the create/edit modal (GET) or persist the meeting (POST).

    GET: the ``_meeting_editor.html`` modal, blank for ``pk is None`` (or
    seeded from ``?task=`` to pre-link the originating task) or pre-filled
    from the meeting otherwise.

    POST: validate + create or update, then ``204`` with
    ``HX-Trigger: acta:meeting-changed`` so every open list/panel refreshes
    and the modal closes.
    """
    meeting = None
    if pk is not None:
        meeting = get_object_or_404(_accessible_meetings(request), pk=pk)
    if request.method == "POST":
        return _meeting_save(request, meeting)
    return render(request, "web/meetings/_meeting_editor.html", _editor_context(request, meeting=meeting))


def _parse_happened_at(raw):
    """Parse a ``datetime-local`` value to an aware datetime, or ``None``.

    The browser sends ``YYYY-MM-DDTHH:MM`` with no timezone; a naive result
    is interpreted in the current timezone so it stores the wall-clock time
    the user picked.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    parsed = parse_datetime(raw)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _resolve_project(request, active):
    """Resolve the optional project to one in the active workspace.

    Returns ``(project, ok)``: ``project`` is ``None`` when blank (allowed —
    meetings are workspace-level), and ``ok`` is ``False`` only when a
    non-blank slug doesn't belong to the workspace.
    """
    slug = (request.POST.get("project") or "").strip()
    if not slug:
        return None, True
    project = next(
        (p for p in _user_accessible_projects(request.user, active) if p.slug_prefix == slug),
        None,
    )
    return project, project is not None


def _resolve_participants(request, active):
    """Validate posted participant ids against the workspace roster.

    Returns the list of member ids, or the ``HttpResponseBadRequest`` class
    sentinel when any id is not a workspace member.
    """
    member_ids = {m.id for m in _members(active)}
    ids = []
    for raw in request.POST.getlist("participants"):
        if not raw.isdigit() or int(raw) not in member_ids:
            return HttpResponseBadRequest
        ids.append(int(raw))
    return ids


def _resolve_tasks(request):
    """Validate posted task ids against tasks the user can access.

    Returns the list of task ids, or the ``HttpResponseBadRequest`` class
    sentinel when any id is foreign / inaccessible.
    """
    raw_ids = []
    for raw in request.POST.getlist("tasks"):
        if not raw.isdigit():
            return HttpResponseBadRequest
        raw_ids.append(int(raw))
    if not raw_ids:
        return []
    valid = set(_user_task_qs(request.user).filter(id__in=raw_ids).values_list("id", flat=True))
    if valid != set(raw_ids):
        return HttpResponseBadRequest
    return list(valid)


def _meeting_save(request, meeting):
    """Validate the editor form and create or update a meeting.

    Returns ``400`` (modal stays open) on any invalid field, else ``204``
    with the ``acta:meeting-changed`` trigger.
    """
    active = resolve_active_workspace(request)
    if active is None:
        return HttpResponseBadRequest("no active workspace")

    title = (request.POST.get("title") or "").strip()
    if not title:
        return HttpResponseBadRequest("title required")
    if len(title) > 200:
        return HttpResponseBadRequest("title too long")

    happened_at = _parse_happened_at(request.POST.get("happened_at"))
    if happened_at is None:
        return HttpResponseBadRequest("happened_at required")

    raw_duration = (request.POST.get("duration_minutes") or "").strip()
    if not raw_duration.isdigit() or int(raw_duration) < 1:
        return HttpResponseBadRequest("invalid duration")
    duration_minutes = int(raw_duration)

    project, project_ok = _resolve_project(request, active)
    if not project_ok:
        return HttpResponseBadRequest("project not in workspace")

    participant_ids = _resolve_participants(request, active)
    if participant_ids is HttpResponseBadRequest:
        return HttpResponseBadRequest("participant not in workspace")

    task_ids = _resolve_tasks(request)
    if task_ids is HttpResponseBadRequest:
        return HttpResponseBadRequest("task not accessible")

    is_create = meeting is None
    with transaction.atomic():
        old_participant_ids = set() if is_create else set(meeting.participants.values_list("pk", flat=True))
        if is_create:
            meeting = Meeting(created_by=request.user)
        meeting.workspace = active
        meeting.project = project
        meeting.title = title
        meeting.happened_at = happened_at
        meeting.duration_minutes = duration_minutes
        meeting.notes = request.POST.get("notes") or ""
        meeting.save()
        meeting.participants.set(participant_ids)
        meeting.tasks.set(task_ids)
        # Notify only participants newly added in this save (everyone on
        # create); the actor is dropped by notify()'s self-suppression.
        new_recipients = set(participant_ids) - old_participant_ids
        if new_recipients:
            notify_meeting_created(meeting=meeting, actor=request.user, recipient_ids=new_recipients)
        log_event(
            workspace=active,
            project=project,
            actor=request.user,
            event_type="meeting.created" if is_create else "meeting.updated",
            target_type=ActivityLog.TARGET_MEETING,
            target_id=meeting.id,
            payload={
                "title": meeting.title,
                "duration_minutes": meeting.duration_minutes,
                "task_ids": task_ids,
            },
        )
    return _changed_response()


def _meeting_comments(meeting, user):
    """Return the meeting's top-level comments, fully decorated for rendering.

    Replies + attachments prefetched, authors joined, and each node (top-level
    and reply) decorated with ``can_modify`` (author or a workspace admin) and
    ``reaction_summary`` (one batched query, no N+1) — the same treatment task
    comments get, so the shared comment card renders identically.
    """
    is_admin = _is_workspace_admin(user.id, meeting.workspace_id)
    comments = list(
        meeting.comments.filter(parent__isnull=True)
        .select_related("author")
        .prefetch_related(
            "attachments",
            "replies__author",
            "replies__attachments",
        )
        .order_by("created_at")
    )
    decorated = []
    for c in comments:
        decorated.append(c)
        decorated.extend(c.replies.all())
    for item in decorated:
        item.can_modify = is_admin or item.author_id == user.id
    attach_reactions(objs=decorated, target_field="comment", user_id=user.id)
    return comments


@login_required
def call_detail(request, pk):
    """Render the full-page view of one meeting.

    The shareable, fullscreen counterpart to the editor modal — reached via
    the modal's "Open full page" button and the copied call link. Edit /
    delete here open the same modal / post to the same endpoints, so every
    surface stays consistent. Includes the meeting's comment thread.
    """
    meeting = get_object_or_404(_accessible_meetings(request), pk=pk)
    return render(
        request,
        "web/meetings/call_detail.html",
        {
            "meeting": meeting,
            "comments": _meeting_comments(meeting, request.user),
        },
    )


@login_required
@require_POST
def post_meeting_comment(request, pk):
    """Create a comment (or one-level reply) on a meeting.

    Reads a Markdown ``body`` plus optional file attachments and an optional
    ``parent`` (a top-level comment id). Returns just the new node so HTMX
    appends it without disturbing other in-progress inputs: a whole thread
    card for a top-level comment, or a single reply block for a reply.
    Mirrors the task ``post_comment`` (attachments + reactions), minus the
    activity-log event (meeting comments are off the activity log).
    """
    meeting = get_object_or_404(_accessible_meetings(request), pk=pk)
    body = (request.POST.get("body") or "").strip()
    files = request.FILES.getlist("file")
    if not body and not files:
        return HttpResponseBadRequest("body or file required")
    parent = None
    parent_raw = (request.GET.get("parent") or request.POST.get("parent") or "").strip()
    if parent_raw:
        if not parent_raw.isdigit():
            return HttpResponseBadRequest("invalid parent")
        parent = Comment.objects.filter(meeting=meeting, parent__isnull=True, pk=int(parent_raw)).first()
        if parent is None:
            return HttpResponseBadRequest("invalid parent")
    try:
        for upload in files:
            categorize(upload)
    except ValidationError as exc:
        return HttpResponseBadRequest("; ".join(exc.messages))
    with transaction.atomic():
        comment = Comment.objects.create(meeting=meeting, author=request.user, parent=parent, body=body)
        for upload in files:
            create_comment_attachment(comment=comment, uploader=request.user, uploaded_file=upload)
    comment.reaction_summary = []
    comment.can_modify = True
    scope = _comment_scope(request)
    template = "web/meetings/_comment_reply.html" if parent else "web/meetings/_comment.html"
    return HttpResponse(render_to_string(template, {"comment": comment, "scope": scope}, request=request))


@login_required
def meeting_comment_reply_form(request, pk, comment_id):
    """Render the lazy TipTap reply composer for one meeting comment.

    Loaded on demand via ``hx-get`` when the user clicks "Reply", mirroring
    the task ``comment_reply_form`` — the editor bundle mounts the returned
    fragment on ``htmx:afterSwap``.
    """
    meeting = get_object_or_404(_accessible_meetings(request), pk=pk)
    parent = get_object_or_404(meeting.comments, parent__isnull=True, pk=comment_id)
    return HttpResponse(
        render_to_string(
            "web/meetings/_comment_reply_form.html",
            {"meeting": meeting, "parent": parent, "scope": _comment_scope(request)},
            request=request,
        ),
    )


@login_required
@require_POST
def meeting_delete(request, pk):
    """Delete a meeting. Linked tasks survive (the M2M rows are dropped)."""
    meeting = get_object_or_404(_accessible_meetings(request), pk=pk)
    workspace = meeting.workspace
    project = meeting.project
    title = meeting.title
    meeting_id = meeting.id
    with transaction.atomic():
        meeting.delete()
        log_event(
            workspace=workspace,
            project=project,
            actor=request.user,
            event_type="meeting.deleted",
            target_type=ActivityLog.TARGET_MEETING,
            target_id=meeting_id,
            payload={"title": title},
        )
    return _changed_response()


@login_required
def task_meetings_fragment(request, slug_prefix, number):
    """Render the "Meetings" panel for one task (lazy fragment).

    The task detail rail includes a thin shell that ``hx-get``s this on
    ``load`` and on ``acta:meeting-changed``, so the panel paints (and stays
    in sync) without pushing a meetings join into the detail/meta querysets.
    Computes the rollup total here: the sum of linked meetings' durations —
    a "time spent around this task" signal, double-counted by design across
    tasks, never summed across tasks for a workspace total.
    """
    task = _get_user_task_or_404(request.user, slug_prefix, number)
    meetings = list(task.meetings.select_related("project").prefetch_related("participants").order_by("-happened_at"))
    total_minutes = sum(m.duration_minutes for m in meetings)
    return render(
        request,
        "web/projects/_meetings_panel_inner.html",
        {
            "task": task,
            "meetings": meetings,
            "total_minutes": total_minutes,
        },
    )


@login_required
def meeting_task_search(request):
    """Workspace-scoped task typeahead for the meeting editor's link picker.

    Shares its matching rules with :func:`apps.web.views.task_link_search` via
    :mod:`apps.tasks.search`, but is scoped to the active workspace rather than
    anchored to one task — a meeting may link any task in the workspace. JSON
    payload feeds the Alpine autocomplete in the modal.
    """
    active = resolve_active_workspace(request)
    if active is None:
        return JsonResponse({"results": []})
    q = (request.GET.get("q") or "").strip()
    qs = _user_task_qs(request.user).filter(project__workspace=active).select_related("project", "assignee")

    results = []
    for t in search_tasks(qs, q):
        results.append(
            {
                "id": t.id,
                "slug": t.slug,
                "title": t.title,
                "status": t.status,
            }
        )
    return JsonResponse({"results": results})
