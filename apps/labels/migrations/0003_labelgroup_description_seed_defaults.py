"""Add ``LabelGroup.description`` and seed the default groups on every workspace.

Two-step migration:

1. Add the nullable-default ``description`` column.
2. For every existing workspace, create the three default groups
   (Type / Area / Layer) via :func:`apps.labels.defaults.seed_default_label_groups`.
   New workspaces created afterwards get the same treatment through the
   ``post_save`` signal in :mod:`apps.labels.signals`.

The data step is idempotent (per-workspace ``name`` dedup) so a re-run
won't duplicate groups, and the reverse migration only drops the column —
seeded rows stay because teams may have filled them with labels by then.
"""

from django.db import migrations, models


def _seed_defaults_for_existing(apps, schema_editor):
    """Bulk-seed the three default groups onto every existing workspace."""
    from apps.labels.defaults import seed_default_label_groups

    Workspace = apps.get_model("workspaces", "Workspace")
    LabelGroup = apps.get_model("labels", "LabelGroup")
    for workspace in Workspace.objects.all():
        seed_default_label_groups(workspace, group_model=LabelGroup)


def _noop(apps, schema_editor):
    """Reverse data step is a no-op — seeded rows persist (teams may have used them)."""


class Migration(migrations.Migration):

    dependencies = [
        ("labels", "0002_label_color_validator"),
        ("workspaces", "0006_workspace_allow_member_announcements_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="labelgroup",
            name="description",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Short guidance shown to the team — what kind of labels belong in this "
                    "group. Surfaces in admin and the future label-management UI."
                ),
            ),
        ),
        migrations.RunPython(_seed_defaults_for_existing, reverse_code=_noop),
    ]
