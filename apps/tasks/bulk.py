"""Bulk endpoints for :class:`Task`.

Implements the contract from docs/decisions/0012-bulk-operations.md:
a single universal ``PATCH /api/v1/tasks/bulk/`` plus a matching
``DELETE`` for batch removal. All operations are all-or-nothing inside
a single DB transaction and emit activity events grouped by a shared
``bulk_id``.

Bulk **project move** and **parent reparenting** are deliberately not
included in this first cut: they require cross-project counter
allocation and subtask cascade rules that warrant their own pass.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone

from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.activity.models import ActivityLog
from apps.labels.models import Label
from apps.workspaces.models import WorkspaceMember

from .events import build_diff_events, snapshot_task
from .models import Task

BULK_LIMIT = 500

ALLOWED_UPDATE_FIELDS = {
    "status",
    "assignee",
    "due_date",
    "priority",
    "size",
    "labels_add",
    "labels_remove",
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
            raise serializers.ValidationError(f"Unknown update fields: {sorted(unknown)}")
        if "status" in updates and updates["status"] not in Task.STATUS_VALUES:
            raise serializers.ValidationError(
                {"status": f"Unknown status: {updates['status']!r}. Allowed: {list(Task.STATUS_VALUES)}"},
            )
        if "size" in updates and updates["size"] is not None and updates["size"] not in Task.SIZE_VALUES:
            raise serializers.ValidationError(
                {"size": f"Invalid size: {updates['size']}. Allowed: {list(Task.SIZE_VALUES)} or null"},
            )
        if "priority" in updates and updates["priority"] not in {0, 1, 2, 3, 4}:
            raise serializers.ValidationError({"priority": "Must be 0..4"})
        for key in ("labels_add", "labels_remove"):
            if key in updates and not isinstance(updates[key], list):
                raise serializers.ValidationError({key: "Must be a list of label IDs"})
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
            {"labels": f"Labels not found: {sorted(missing)}"},
        )
    bad = [row["id"] for row in found if row["workspace_id"] not in workspace_ids]
    if bad:
        raise serializers.ValidationError(
            {"labels": f"Labels not in affected workspaces: {sorted(bad)}"},
        )
    return list(found_ids)


def _bulk_apply_scalars(ids: list[int], updates: dict[str, Any]) -> None:
    """Apply scalar field updates to all rows in a single SQL UPDATE.

    Bypasses :meth:`Task.save` so this stays O(1) in query count regardless
    of batch size. ``updated_at`` is set explicitly because ``auto_now``
    only fires on ``save()``.

    Scalar fields handled: ``status``, ``due_date``, ``priority``, ``size``,
    ``assignee`` (mapped to ``assignee_id``). Label add/remove are M2M and
    handled separately in :func:`_bulk_apply_labels`.

    Args:
        ids: List of task primary keys to update.
        updates: Validated ``updates`` dict from the request.
    """
    payload: dict[str, Any] = {}
    if "status" in updates:
        payload["status"] = updates["status"]
    if "due_date" in updates:
        payload["due_date"] = updates["due_date"]
    if "priority" in updates:
        payload["priority"] = updates["priority"]
    if "size" in updates:
        payload["size"] = updates["size"]
    if "assignee" in updates:
        payload["assignee_id"] = updates["assignee"]
    if not payload:
        return
    payload["updated_at"] = timezone.now()
    Task.objects.filter(id__in=ids).update(**payload)


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
    pre_tasks = list(accessible_qs)
    accessible_ids = {t.id for t in pre_tasks}
    if accessible_ids != requested:
        raise PermissionError("inaccessible task(s) in batch")

    workspace_ids = {t.project.workspace_id for t in pre_tasks}
    add_label_ids = _validate_labels_belong_to_workspaces(updates.get("labels_add", []), workspace_ids)
    remove_label_ids = _validate_labels_belong_to_workspaces(updates.get("labels_remove", []), workspace_ids)

    bulk_id = uuid4()
    valid_ids = list(accessible_ids)
    with transaction.atomic():
        snapshots = {t.id: snapshot_task(t) for t in pre_tasks}
        _bulk_apply_scalars(valid_ids, updates)
        _bulk_apply_labels(valid_ids, add_label_ids, remove_label_ids)
        # Refetch with eager loads so event building does not refetch
        # project/workspace/labels per task.
        post_tasks = (
            Task.objects.filter(id__in=valid_ids).select_related("project__workspace").prefetch_related("labels")
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
    return bulk_id, len(valid_ids)


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
                {"detail": "Permission denied for one or more tasks in the batch."},
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
                {"detail": "Permission denied for one or more tasks in the batch."},
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
