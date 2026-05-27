"""Bulk endpoints for :class:`Task`.

Implements the contract from docs/decisions/0012-bulk-operations.md:
a single universal ``PATCH /api/v1/tasks/bulk/`` plus a matching
``DELETE`` for batch removal. All operations are all-or-nothing inside
a single DB transaction and emit activity events grouped by a shared
``bulk_id``.

Supports cross-project ``project`` moves within a workspace, including
subtask cascade (a top-level task being moved drags its subtasks with
it) and parent-clear for subtasks moved without their parent. Bulk
``parent`` reparenting is deliberately not included in this cut: setting
``parent`` in bulk needs same-project / depth-limit checks per row,
which is a separate pass.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.activity.models import ActivityLog
from apps.labels.models import Label
from apps.projects.models import Project
from apps.workspaces.models import WorkspaceMember

from .events import broadcast_task_events, build_diff_events, snapshot_task
from .models import Task

BULK_LIMIT = 500

ALLOWED_UPDATE_FIELDS = {
    "status",
    "assignee",
    "start_date",
    "end_date",
    "due_date",
    "priority",
    "size",
    "labels_add",
    "labels_remove",
    "project",
    "cycle",
    "archived",
}

SCALAR_UPDATE_KEYS = {
    "status",
    "start_date",
    "end_date",
    "due_date",
    "priority",
    "size",
    "assignee",
    "cycle",
    "archived",
}

# Sentinel for "do not touch this field"; distinct from None which means
# explicit clear.
_UNSET: Any = object()


class BulkUpdateSerializer(serializers.Serializer):
    """Input validator for ``PATCH /api/v1/tasks/bulk/``."""

    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=BULK_LIMIT,
    )
    updates = serializers.DictField()

    def validate_updates(self, updates):
        """Reject unknown fields and out-of-range scalar values.

        Args:
            updates: Raw ``updates`` dict from the request body.

        Returns:
            The validated dict, unchanged in shape.

        Raises:
            serializers.ValidationError: If any field is unknown or
                carries an invalid value.
        """
        unknown = set(updates.keys()) - ALLOWED_UPDATE_FIELDS
        if unknown:
            raise serializers.ValidationError(
                _("Unknown update fields: %(fields)s") % {"fields": sorted(unknown)},
            )
        if "status" in updates and updates["status"] not in Task.STATUS_VALUES:
            raise serializers.ValidationError(
                {
                    "status": _("Unknown status: %(value)s. Allowed: %(allowed)s")
                    % {"value": updates["status"], "allowed": list(Task.STATUS_VALUES)},
                },
            )
        if "size" in updates and updates["size"] is not None and updates["size"] not in Task.SIZE_VALUES:
            raise serializers.ValidationError(
                {
                    "size": _("Invalid size: %(value)s. Allowed: %(allowed)s or null")
                    % {"value": updates["size"], "allowed": list(Task.SIZE_VALUES)},
                },
            )
        if "priority" in updates and updates["priority"] not in {0, 1, 2, 3, 4}:
            raise serializers.ValidationError({"priority": _("Must be 0..4")})
        for key in ("labels_add", "labels_remove"):
            if key in updates and not isinstance(updates[key], list):
                raise serializers.ValidationError({key: _("Must be a list of label IDs")})
        if "project" in updates and not isinstance(updates["project"], int):
            raise serializers.ValidationError({"project": _("Must be a project ID (int)")})
        if "cycle" in updates and updates["cycle"] is not None and not isinstance(updates["cycle"], int):
            raise serializers.ValidationError({"cycle": _("Must be a cycle ID (int) or null")})
        if "archived" in updates and not isinstance(updates["archived"], bool):
            raise serializers.ValidationError(
                {"archived": _("Must be a boolean (true to archive, false to unarchive)")}
            )
        return updates


class BulkDeleteSerializer(serializers.Serializer):
    """Input validator for ``DELETE /api/v1/tasks/bulk/``."""

    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=BULK_LIMIT,
    )


def _accessible_task_qs(user, ids):
    """Return the queryset of tasks among ``ids`` accessible to ``user``.

    Access is granted via :class:`WorkspaceMember` membership in the
    task's project's workspace. Eagerly loads the project (and its
    workspace) plus the labels M2M so the per-task loop in bulk
    operations does not regress into N+1.

    Args:
        user: The acting :class:`User`.
        ids: An iterable of task IDs to check.

    Returns:
        A queryset of :class:`Task` instances the user can act on.
    """
    return (
        Task.objects.filter(
            id__in=ids,
            project__workspace__memberships__user=user,
        )
        .select_related(
            "project__workspace",
            "cycle",
        )
        .prefetch_related(
            "labels",
        )
        .distinct()
    )


def _validate_labels_belong_to_workspaces(label_ids, workspace_ids):
    """Ensure every label ID is in one of the given workspaces.

    Args:
        label_ids: Iterable of label primary keys requested for add/remove.
        workspace_ids: Iterable of workspace IDs the affected tasks live in.

    Returns:
        A list of label IDs that exist and pass the workspace check.

    Raises:
        serializers.ValidationError: If any label is missing or lives in
            a workspace not in ``workspace_ids``.
    """
    if not label_ids:
        return []
    found = list(Label.objects.filter(id__in=label_ids).values("id", "workspace_id"))
    found_ids = {row["id"] for row in found}
    missing = set(label_ids) - found_ids
    if missing:
        raise serializers.ValidationError(
            {"labels": _("Labels not found: %(ids)s") % {"ids": sorted(missing)}},
        )
    bad = [row["id"] for row in found if row["workspace_id"] not in workspace_ids]
    if bad:
        raise serializers.ValidationError(
            {"labels": _("Labels not in affected workspaces: %(ids)s") % {"ids": sorted(bad)}},
        )
    return list(found_ids)


def _bulk_apply_scalars(ids: list[int], updates: dict[str, Any]) -> None:
    """Apply scalar field updates to all rows in a single SQL UPDATE.

    Bypasses :meth:`Task.save` so this stays O(1) in query count regardless
    of batch size. ``updated_at`` is set explicitly because ``auto_now``
    only fires on ``save()``.

    Scalar fields handled: ``status``, ``start_date``, ``end_date``,
    ``due_date``, ``priority``, ``size``, ``assignee`` (mapped to
    ``assignee_id``). Label add/remove are M2M and handled separately in
    :func:`_bulk_apply_labels`.

    Args:
        ids: List of task primary keys to update.
        updates: Validated ``updates`` dict from the request.
    """
    now = timezone.now()
    payload: dict[str, Any] = {}
    if "status" in updates:
        payload["status"] = updates["status"]
        # ``Task.save`` maintains ``completed_at`` + ``end_date`` on status
        # transitions, but the bulk path bypasses it, so mirror that here:
        # stamp the done timestamp + the actual finish date (overwriting any
        # prior one is acceptable for a bulk move) and clear completed_at when
        # leaving done. An explicit ``end_date`` in the same payload wins (it
        # is applied just below, after this).
        if updates["status"] == Task.STATUS_DONE:
            payload["completed_at"] = now
            payload["end_date"] = now.date()
        else:
            payload["completed_at"] = None
    if "start_date" in updates:
        payload["start_date"] = updates["start_date"]
    if "end_date" in updates:
        payload["end_date"] = updates["end_date"]
    if "due_date" in updates:
        payload["due_date"] = updates["due_date"]
    if "priority" in updates:
        payload["priority"] = updates["priority"]
    if "size" in updates:
        payload["size"] = updates["size"]
    if "assignee" in updates:
        payload["assignee_id"] = updates["assignee"]
    # ``cycle`` is intentionally NOT applied here — it's handled by
    # :func:`_bulk_apply_cycle` so an explicit assignment can skip planned
    # (backlog) tasks, which the cadence policy keeps cycle-free.
    if "archived" in updates:
        # ``archived`` is a request-side bool; the column is a timestamp
        # so unarchive clears it and archive stamps "now". The diff
        # builder ([[apps.tasks.events]]) reads ``archived_at`` from the
        # snapshot and emits task.archived / task.unarchived accordingly.
        payload["archived_at"] = now if updates["archived"] else None
    if not payload:
        return
    payload["updated_at"] = now
    Task.objects.filter(id__in=ids).update(**payload)


def _resolve_target_project(target_id: int, user) -> Project:
    """Load the target project for a bulk move and check user access.

    Args:
        target_id: Primary key of the project tasks are being moved into.
        user: The acting :class:`User`.

    Returns:
        The :class:`Project` instance with workspace eagerly loaded.

    Raises:
        serializers.ValidationError: If the project does not exist.
        PermissionError: If the user is not a member of the target
            project's workspace.
    """
    try:
        project = Project.objects.select_related("workspace").get(pk=target_id)
    except Project.DoesNotExist as exc:
        raise serializers.ValidationError(
            {"project": _("Project %(id)s not found") % {"id": target_id}},
        ) from exc
    if not WorkspaceMember.objects.filter(user=user, workspace=project.workspace).exists():
        raise PermissionError("inaccessible target project")
    return project


def _resolve_target_cycle(target_id: int):
    """Load the target cycle for a bulk cycle assignment.

    Args:
        target_id: Primary key of the cycle tasks are being committed to.

    Returns:
        The :class:`~apps.cycles.models.Cycle` instance.

    Raises:
        serializers.ValidationError: If the cycle does not exist. The
            workspace-match check (a cycle only applies to its own
            workspace's tasks) is enforced by the caller against the
            affected tasks.
    """
    from apps.cycles.models import Cycle

    try:
        return Cycle.objects.get(pk=target_id)
    except Cycle.DoesNotExist as exc:
        raise serializers.ValidationError(
            {"cycle": _("Cycle %(id)s not found") % {"id": target_id}},
        ) from exc


def _expand_move_set(
    requested_ids: set[int],
    target_project_id: int,
) -> tuple[list[int], set[int]]:
    """Compute the full task ID set affected by a bulk project move.

    Resolves two derived sets:

    * **Cascade**: subtasks of every top-level task being moved must move
      with their parent so the ``subtask.project == parent.project``
      invariant from docs/decisions/0007-data-model-task-project.md
      holds.
    * **Parent clear**: subtasks that appear in ``requested_ids`` without
      their parent (and whose parent is not being moved either) must
      have their ``parent_id`` cleared, otherwise they would dangle as
      cross-project references.

    Args:
        requested_ids: The explicit IDs from the request body.
        target_project_id: ID of the destination project.

    Returns:
        A tuple ``(full_ids, parent_clear_ids)``: the full set of tasks
        the move affects, and the subset whose ``parent_id`` must be
        nulled.
    """
    requested_tasks = list(Task.objects.filter(id__in=requested_ids).only("id", "parent_id", "project_id"))
    top_level_moving = {t.id for t in requested_tasks if t.parent_id is None and t.project_id != target_project_id}
    cascade_ids = (
        set(
            Task.objects.filter(parent_id__in=top_level_moving).values_list("id", flat=True),
        )
        - requested_ids
    )

    full_ids = list(requested_ids | cascade_ids)
    full_id_set = set(full_ids)
    parent_clear_ids = {t.id for t in requested_tasks if t.parent_id is not None and t.parent_id not in full_id_set}
    return full_ids, parent_clear_ids


def _bulk_apply_project_move(
    target_project: Project,
    pre_tasks: list[Task],
    parent_clear_ids: set[int],
) -> dict[int, int]:
    """Move tasks to ``target_project`` with freshly allocated numbers.

    Skips tasks already in the target project (no-op renumber). Uses
    :meth:`Project.allocate_task_numbers` to reserve numbers in a single
    locked counter step, then issues one ``UPDATE`` via
    :meth:`QuerySet.bulk_update` to write project, number, parent_id,
    and updated_at for the whole batch.

    Args:
        target_project: Project tasks are being moved into.
        pre_tasks: Source task instances loaded before the move.
        parent_clear_ids: IDs whose ``parent_id`` must be nulled (subtask
            moved without its parent).

    Returns:
        A map ``{task_id: new_number}`` for tasks actually moved.
    """
    to_move = [t for t in pre_tasks if t.project_id != target_project.id]
    if not to_move:
        return {}
    # Order so top-level tasks get lower numbers than their cascaded
    # subtasks — keeps the human-facing slug sequence sensible.
    to_move.sort(key=lambda t: (t.parent_id is not None, t.id))
    numbers = target_project.allocate_task_numbers(len(to_move))
    number_map = dict(zip([t.id for t in to_move], numbers))
    now = timezone.now()
    for task in to_move:
        task.project_id = target_project.id
        task.number = number_map[task.id]
        if task.id in parent_clear_ids:
            task.parent_id = None
        task.updated_at = now
    Task.objects.bulk_update(
        to_move,
        [
            "project_id",
            "number",
            "parent_id",
            "updated_at",
        ],
    )
    return number_map


def _bulk_apply_cycle(ids: list[int], cycle_value) -> None:
    """Apply an explicit bulk ``cycle`` set, honouring the backlog rule.

    Clearing (``cycle_value is None``) applies to every task. Assigning a
    cycle skips ``planned`` tasks — they are the backlog and the cadence
    policy keeps them cycle-free (mirrors the per-task endpoint guard).

    Args:
        ids: Task primary keys in the batch.
        cycle_value: Target cycle id, or ``None`` to clear to backlog.
    """
    now = timezone.now()
    if cycle_value is None:
        Task.objects.filter(id__in=ids).update(cycle_id=None, updated_at=now)
        return
    Task.objects.filter(id__in=ids).exclude(
        status__in=(Task.STATUS_PLANNED, Task.STATUS_READY),
    ).update(
        cycle_id=cycle_value,
        updated_at=now,
    )


def _bulk_apply_cycle_policy(ids: list[int], new_status: str) -> None:
    """Reconcile cycles after a bulk **status** change (cadence policy).

    Mirrors :func:`apps.cycles.services.apply_cycle_policy` for the bulk
    path: a move to ``planned`` clears the cycle on every task; a move
    into committed work (``to-do`` / ``in-progress`` / ``in-review``)
    pulls each still-backlogged task into its workspace's active cycle.
    Grouped by workspace so a multi-workspace batch resolves the right
    cycle. Other target statuses leave cycles untouched.

    Args:
        ids: Task primary keys that just had their status set.
        new_status: The status value applied to the whole batch.
    """
    now = timezone.now()
    if new_status in (Task.STATUS_PLANNED, Task.STATUS_READY):
        Task.objects.filter(id__in=ids).exclude(cycle__isnull=True).update(cycle_id=None, updated_at=now)
        return
    if new_status not in (Task.STATUS_TODO, Task.STATUS_IN_PROGRESS, Task.STATUS_IN_REVIEW):
        return
    from apps.cycles.services import current_cycle, ensure_cycles
    from apps.workspaces.models import Workspace

    by_workspace: dict[int, list[int]] = {}
    rows = Task.objects.filter(id__in=ids, cycle__isnull=True).values_list("id", "project__workspace_id")
    for task_id, workspace_id in rows:
        by_workspace.setdefault(workspace_id, []).append(task_id)
    for workspace_id, task_ids in by_workspace.items():
        workspace = Workspace.objects.get(pk=workspace_id)
        if not workspace.cycle_config()["enabled"]:
            continue
        ensure_cycles(workspace)
        active = current_cycle(workspace)
        if active is not None:
            Task.objects.filter(id__in=task_ids).update(cycle_id=active.id, updated_at=now)


def _bulk_apply_labels(ids: list[int], add_label_ids: list[int], remove_label_ids: list[int]) -> None:
    """Bulk add and remove labels on a set of tasks via the through table.

    Skips Django's per-row M2M descriptor (which would run 1 query per
    task). The through model is accessed directly so the whole batch
    commits in two queries at most (one bulk_create, one delete).

    Args:
        ids: List of task primary keys.
        add_label_ids: Label IDs to attach to each task.
        remove_label_ids: Label IDs to detach from each task.
    """
    through = Task.labels.through
    if add_label_ids:
        through.objects.bulk_create(
            [through(task_id=tid, label_id=lid) for tid in ids for lid in add_label_ids],
            ignore_conflicts=True,
        )
    if remove_label_ids:
        through.objects.filter(task_id__in=ids, label_id__in=remove_label_ids).delete()


def _run_bulk_update(*, user, ids: list[int], updates: dict[str, Any]) -> tuple[UUID, int]:
    """Execute the bulk update inside a single transaction.

    Args:
        user: The acting :class:`User`.
        ids: List of task primary keys to update.
        updates: Validated ``updates`` dict.

    Returns:
        A tuple ``(bulk_id, updated_count)``.

    Raises:
        PermissionError: If any requested ID is inaccessible to ``user``.
        serializers.ValidationError: If labels reference workspaces the
            user has no business touching.
    """
    requested = set(ids)
    accessible_qs = _accessible_task_qs(user, ids)
    pre_requested = list(accessible_qs)
    accessible_ids = {t.id for t in pre_requested}
    if accessible_ids != requested:
        raise PermissionError("inaccessible task(s) in batch")

    # Start / end dates are the assignee's to schedule (an unassigned task
    # is open to anyone). All-or-nothing: reject the whole batch if it
    # would move those dates on a task assigned to someone else.
    if "start_date" in updates or "end_date" in updates:
        bad = [t.id for t in pre_requested if t.assignee_id is not None and t.assignee_id != user.id]
        if bad:
            raise serializers.ValidationError(
                {
                    "start_date": _("Only the assignee can change the start/end date — tasks: %(ids)s")
                    % {"ids": sorted(bad)},
                },
            )

    target_project: Project | None = None
    if "project" in updates:
        target_project = _resolve_target_project(updates["project"], user)
        target_workspace_id = target_project.workspace_id
        bad = [t.id for t in pre_requested if t.project.workspace_id != target_workspace_id]
        if bad:
            raise serializers.ValidationError(
                {
                    "project": _("Cross-workspace bulk move not allowed for tasks: %(ids)s") % {"ids": sorted(bad)},
                },
            )

    if updates.get("cycle") is not None:
        target_cycle = _resolve_target_cycle(updates["cycle"])
        bad = [t.id for t in pre_requested if t.project.workspace_id != target_cycle.workspace_id]
        if bad:
            raise serializers.ValidationError(
                {
                    "cycle": _("Cycle not in the affected workspace for tasks: %(ids)s") % {"ids": sorted(bad)},
                },
            )

    if target_project is not None:
        full_ids, parent_clear_ids = _expand_move_set(requested, target_project.id)
    else:
        full_ids = list(requested)
        parent_clear_ids = set()

    workspace_ids = {t.project.workspace_id for t in pre_requested}
    add_label_ids = _validate_labels_belong_to_workspaces(updates.get("labels_add", []), workspace_ids)
    remove_label_ids = _validate_labels_belong_to_workspaces(updates.get("labels_remove", []), workspace_ids)

    bulk_id = uuid4()
    with transaction.atomic():
        # Snapshot full set (requested + cascaded) so cascaded subtasks
        # also get their project/number change recorded in activity log.
        pre_all_tasks = list(
            Task.objects.filter(id__in=full_ids)
            .select_related("project__workspace", "cycle")
            .prefetch_related("labels"),
        )
        snapshots = {t.id: snapshot_task(t) for t in pre_all_tasks}

        if target_project is not None:
            _bulk_apply_project_move(target_project, pre_all_tasks, parent_clear_ids)

        # Scalar and label updates apply only to explicitly requested IDs;
        # cascaded subtasks ride along on the project move only.
        scalar_updates = {k: v for k, v in updates.items() if k in SCALAR_UPDATE_KEYS}
        if scalar_updates:
            _bulk_apply_scalars(list(requested), scalar_updates)
        # Cycle: explicit set first, then the status-driven cadence policy
        # (a status change reconciles cycles for tasks that didn't get an
        # explicit cycle in the same request). Both run before the
        # post-state reload so the diff captures task.cycle_changed.
        if "cycle" in updates:
            _bulk_apply_cycle(list(requested), updates["cycle"])
        elif "status" in updates:
            _bulk_apply_cycle_policy(list(requested), updates["status"])
        _bulk_apply_labels(list(requested), add_label_ids, remove_label_ids)

        # ``select_related('assignee')`` matters for the SSE card
        # render below — ``_task_card.html`` reads ``task.assignee.*``.
        # Without it the broadcast loop would fire one SELECT per task.
        post_tasks = list(
            Task.objects.filter(id__in=full_ids)
            .select_related("project__workspace", "assignee", "cycle")
            .prefetch_related("labels", "blocks", "blocked_by"),
        )
        all_events: list[ActivityLog] = []
        for task in post_tasks:
            all_events.extend(
                build_diff_events(
                    old_state=snapshots[task.id],
                    task=task,
                    actor=user,
                    bulk_id=bulk_id,
                ),
            )
        if all_events:
            ActivityLog.objects.bulk_create(all_events)
            # SSE broadcast — fan the bulk diff out to connected
            # workspace streams. ``post_tasks`` is already
            # ``select_related/prefetch_related``'d for the diff
            # build so the card render reuses that data.
            tasks_by_id = {t.pk: t for t in post_tasks}
            broadcast_task_events(all_events, tasks_by_id, user)
    return bulk_id, len(full_ids)


def _run_bulk_delete(*, user, ids: list[int]) -> tuple[UUID, int]:
    """Execute the bulk delete inside a single transaction.

    Args:
        user: The acting :class:`User`.
        ids: List of task primary keys to delete.

    Returns:
        A tuple ``(bulk_id, deleted_count)``.

    Raises:
        PermissionError: If any requested ID is inaccessible to ``user``.
    """
    requested = set(ids)
    accessible_qs = _accessible_task_qs(user, ids).select_related("project__workspace")
    accessible_ids = set(accessible_qs.values_list("id", flat=True))
    if accessible_ids != requested:
        raise PermissionError("inaccessible task(s) in batch")

    bulk_id = uuid4()
    with transaction.atomic():
        events_to_create: list[ActivityLog] = []
        for task in accessible_qs:
            events_to_create.append(
                ActivityLog(
                    workspace=task.project.workspace,
                    project=task.project,
                    actor=user,
                    event_type="task.deleted",
                    target_type=ActivityLog.TARGET_TASK,
                    target_id=task.id,
                    payload={
                        "snapshot": {
                            "title": task.title,
                            "project_id": task.project_id,
                            "number": task.number,
                            "status": task.status,
                        },
                    },
                    bulk_id=bulk_id,
                ),
            )
        deleted = len(events_to_create)
        # Delete first, then write events. Activity rows survive the
        # delete because target_id is plain int, not a FK.
        accessible_qs.delete()
        if events_to_create:
            ActivityLog.objects.bulk_create(events_to_create)
            # SSE broadcast — deletion has no surviving task to
            # render, so the empty mapping means each event's payload
            # carries no ``card_html`` and clients simply remove the
            # matching kanban card.
            broadcast_task_events(events_to_create, {}, user)
    return bulk_id, deleted


class TaskBulkView(APIView):
    """``/api/v1/tasks/bulk/`` — atomic batch update and delete for tasks.

    See docs/decisions/0012-bulk-operations.md for the full contract.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        """Apply ``updates`` to every task in ``ids`` atomically.

        Args:
            request: DRF request carrying a JSON body with ``ids`` and
                ``updates`` keys.

        Returns:
            ``200 OK`` with ``{"updated_count", "bulk_id"}`` on success,
            ``400`` on validation failure, ``403`` if any task is
            inaccessible.
        """
        serializer = BulkUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        updates = serializer.validated_data["updates"]
        try:
            bulk_id, updated_count = _run_bulk_update(user=request.user, ids=ids, updates=updates)
        except PermissionError:
            return Response(
                {"detail": _("Permission denied for one or more tasks in the batch.")},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(
            {"updated_count": updated_count, "bulk_id": str(bulk_id)},
            status=status.HTTP_200_OK,
        )

    def delete(self, request):
        """Delete every task in ``ids`` atomically.

        Args:
            request: DRF request carrying a JSON body with an ``ids`` key.

        Returns:
            ``200 OK`` with ``{"deleted_count", "bulk_id"}`` on success,
            ``400`` on validation failure, ``403`` if any task is
            inaccessible.
        """
        serializer = BulkDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        try:
            bulk_id, deleted_count = _run_bulk_delete(user=request.user, ids=ids)
        except PermissionError:
            return Response(
                {"detail": _("Permission denied for one or more tasks in the batch.")},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(
            {"deleted_count": deleted_count, "bulk_id": str(bulk_id)},
            status=status.HTTP_200_OK,
        )


def membership_check(user, workspace_id) -> bool:
    """Return whether ``user`` is a member of ``workspace_id``.

    Args:
        user: The acting :class:`User`.
        workspace_id: The target workspace primary key.

    Returns:
        ``True`` if the membership row exists, ``False`` otherwise.
    """
    return WorkspaceMember.objects.filter(user=user, workspace_id=workspace_id).exists()
