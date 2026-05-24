"""Backfill ``Task.completed_at`` for tasks already in the done status.

Source of truth is the activity log: the timestamp of the most recent
``task.status_changed`` event whose payload moved the task ``to`` done.
Tasks that are done but have no such event (e.g. created directly as
done, or pre-activity-log) fall back to ``updated_at``.
"""

from django.db import migrations


def backfill_completed_at(apps, schema_editor):
    """Set ``completed_at`` on every done task that lacks it."""
    Task = apps.get_model("tasks", "Task")
    ActivityLog = apps.get_model("activity", "ActivityLog")

    done_ids = list(
        Task.objects.filter(status="done", completed_at__isnull=True).values_list("id", flat=True),
    )
    if not done_ids:
        return

    # Most recent "became done" event per task, in one ordered pass.
    latest_done_at = {}
    events = (
        ActivityLog.objects.filter(
            event_type="task.status_changed",
            target_type="task",
            target_id__in=done_ids,
            payload__to="done",
        )
        .order_by("created_at")
        .values_list("target_id", "created_at")
    )
    for target_id, created_at in events.iterator():
        latest_done_at[target_id] = created_at  # later rows overwrite → keeps the latest

    to_update = []
    for task in Task.objects.filter(id__in=done_ids).only("id", "updated_at"):
        task.completed_at = latest_done_at.get(task.id, task.updated_at)
        to_update.append(task)
    Task.objects.bulk_update(to_update, ["completed_at"], batch_size=500)


def clear_completed_at(apps, schema_editor):
    """Reverse: drop all backfilled timestamps."""
    Task = apps.get_model("tasks", "Task")
    Task.objects.filter(completed_at__isnull=False).update(completed_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0010_task_completed_at"),
        ("activity", "0003_alter_activitylog_target_type"),
    ]

    operations = [
        migrations.RunPython(backfill_completed_at, clear_completed_at),
    ]
