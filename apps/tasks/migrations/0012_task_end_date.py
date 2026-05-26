"""Add ``Task.end_date`` (planned finish) and seed it from ``due_date``.

Until now ``due_date`` did double duty: the hard deadline AND the right
edge of the timeline bar. ``end_date`` splits the "planned finish" out so
the Gantt bar is ``start_date → end_date`` and ``due_date`` is purely the
deadline marker. Existing tasks had their bar drawn to ``due_date``, so we
backfill ``end_date = due_date`` to preserve every current bar (end equals
the deadline → no task is suddenly "late").
"""

from django.db import migrations, models
from django.db.models import F


def backfill_end_date(apps, schema_editor):
    """Copy ``due_date`` into the new ``end_date`` wherever it is unset."""
    Task = apps.get_model("tasks", "Task")
    Task.objects.filter(
        due_date__isnull=False,
        end_date__isnull=True,
    ).update(end_date=F("due_date"))


def clear_seeded_end_date(apps, schema_editor):
    """Reverse: drop only the seeded values (``end_date`` still equal to due_date)."""
    Task = apps.get_model("tasks", "Task")
    Task.objects.filter(end_date=F("due_date")).update(end_date=None)


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0011_backfill_completed_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="end_date",
            field=models.DateField(
                blank=True,
                help_text="Planned finish date; drives the right edge of the timeline bar (start_date to end_date)",
                null=True,
            ),
        ),
        migrations.RunPython(backfill_end_date, clear_seeded_end_date),
    ]
