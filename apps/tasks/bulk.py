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

from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.labels.models import Label
from apps.workspaces.models import WorkspaceMember

from .events import emit_task_diff_events, snapshot_task
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
    task's project's workspace.

    Args:
        user: The acting :class:`User`.
        ids: An iterable of task IDs to check.

    Returns:
        A queryset of :class:`Task` instances the user can act on.
    """
    return Task.objects.filter(
        id__in=ids,
        project__workspace__memberships__user=user,
    ).distinct()


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


def _apply_updates(task: Task, updates: dict[str, Any], add_labels, remove_labels):
    """Apply a validated bulk update payload to a single task.

    Scalar fields are assigned in memory; ``save()`` is called once. Label
    add/remove operations run after save to ensure the row exists.

    Args:
        task: The :class:`Task` instance to mutate.
        updates: The validated ``updates`` dict from the request.
        add_labels: List of :class:`Label` instances to attach.
        remove_labels: List of label IDs to detach.
    """
    if "status" in updates:
        task.status = updates["status"]
    if "assignee" in updates:
        task.assignee_id = updates["assignee"]
    if "due_date" in updates:
        task.due_date = updates["due_date"]
    if "priority" in updates:
        task.priority = updates["priority"]
    if "size" in updates:
        task.size = updates["size"]
    task.save()
    if add_labels:
        task.labels.add(*add_labels)
    if remove_labels:
        task.labels.remove(*remove_labels)


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
    accessible_qs = _accessible_task_qs(user, ids).select_related("project__workspace")
    accessible_ids = set(accessible_qs.values_list("id", flat=True))
    if accessible_ids != requested:
        raise PermissionError("inaccessible task(s) in batch")

    workspace_ids = set(t.project.workspace_id for t in accessible_qs)
    add_label_ids = _validate_labels_belong_to_workspaces(updates.get("labels_add", []), workspace_ids)
    remove_label_ids = _validate_labels_belong_to_workspaces(updates.get("labels_remove", []), workspace_ids)
    add_labels = list(Label.objects.filter(id__in=add_label_ids))

    bulk_id = uuid4()
    updated = 0
    with transaction.atomic():
        for task in accessible_qs.prefetch_related("labels"):
            old_state = snapshot_task(task)
            _apply_updates(task, updates, add_labels=add_labels, remove_labels=remove_label_ids)
            task.refresh_from_db()
            emit_task_diff_events(old_state=old_state, task=task, actor=user, bulk_id=bulk_id)
            updated += 1
    return bulk_id, updated


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
    deleted = 0
    with transaction.atomic():
        snapshots: list[tuple[int, dict[str, Any], Any, Any]] = []
        for task in accessible_qs:
            snapshots.append(
                (
                    task.id,
                    {
                        "title": task.title,
                        "project_id": task.project_id,
                        "number": task.number,
                        "status": task.status,
                    },
                    task.project.workspace,
                    task.project,
                ),
            )
        # Delete first, then write events. Activity rows survive the
        # delete because target_id is plain int, not a FK.
        accessible_qs.delete()
        for task_id, snapshot, workspace, project in snapshots:
            log_event(
                workspace=workspace,
                project=project,
                actor=user,
                event_type="task.deleted",
                target_type=ActivityLog.TARGET_TASK,
                target_id=task_id,
                payload={"snapshot": snapshot},
                bulk_id=bulk_id,
            )
            deleted += 1
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
