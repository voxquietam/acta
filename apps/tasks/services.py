"""Operations on a task that are more than a field write.

Each function here changes several rows under one transaction and logs
what it did. They live in the app that owns the model rather than in a
view, because the web and the MCP tools both perform them and a rule
that exists twice is a rule that drifts — the conversions below are
refused on conditions the UI and the tool have to agree on exactly.

See docs/decisions/0036-epics.md for why an epic is a task with a
``kind`` rather than a model of its own.
"""

from django.db import transaction
from django.utils import timezone

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.tasks.events import emit_task_diff_events, snapshot_task
from apps.tasks.models import Task


def turn_task_into_epic(task, *, actor):
    """Turn a plain task into an epic.

    The caller checks whether it may: see
    ``apps.web.views._turn_into_epic_blockers``. Three things change
    together, which is why this is one transaction rather than three
    saves: the subtasks become the epic's first tasks, everything an
    epic derives from its tasks is dropped, and the status restarts at
    planned because from here on it is computed.

    Args:
        task: The :class:`Task` to convert.
        actor: The user to credit in the activity log.

    Returns:
        The same task, saved.
    """
    subtasks = list(task.subtasks.order_by("number"))
    with transaction.atomic():
        before = snapshot_task(task)
        task.kind = Task.KIND_EPIC
        task.due_date = None
        task.size = None
        task.cycle = None
        task.epic = None
        task.status = Task.STATUS_PLANNED
        task.save(
            update_fields=["kind", "due_date", "size", "cycle", "epic", "status", "updated_at"],
        )
        # The hierarchy it had is exactly the work it collects.
        if subtasks:
            Task.objects.filter(pk__in=[s.pk for s in subtasks]).update(
                parent=None,
                epic=task,
                updated_at=timezone.now(),
            )
        emit_task_diff_events(task=task, old_state=before, actor=actor)
        log_event(
            workspace=task.project.workspace,
            project=task.project,
            actor=actor,
            event_type="task.turned_into_epic",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"title": task.title, "tasks": len(subtasks)},
        )
    return task


def turn_epic_into_task(epic, *, actor):
    """Turn an epic back into a plain task.

    The caller checks whether it may: see
    ``apps.web.views._turn_into_task_blockers``. The tasks it collected
    become its subtasks again. It is a conversion back and not an undo —
    the deadline, size and cycle the first conversion dropped were not
    kept anywhere. The status it lands on is the one the epic was
    showing, since that is what its work adds up to.

    Args:
        epic: The epic :class:`Task` to convert.
        actor: The user to credit in the activity log.

    Returns:
        The same task, saved.
    """
    members = list(epic.epic_members().order_by("number"))
    with transaction.atomic():
        before = snapshot_task(epic)
        status = epic.epic_status
        epic.kind = Task.KIND_TASK
        epic.status = status
        epic.save(update_fields=["kind", "status", "updated_at"])
        if members:
            Task.objects.filter(pk__in=[m.pk for m in members]).update(
                parent=epic,
                epic=None,
                updated_at=timezone.now(),
            )
        emit_task_diff_events(task=epic, old_state=before, actor=actor)
        log_event(
            workspace=epic.project.workspace,
            project=epic.project,
            actor=actor,
            event_type="task.turned_into_task",
            target_type=ActivityLog.TARGET_TASK,
            target_id=epic.id,
            payload={"title": epic.title, "tasks": len(members)},
        )
    return epic
