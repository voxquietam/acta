"""Signal wiring for the labels app.

The lone receiver here seeds the default :class:`LabelGroup` set onto
every new :class:`~apps.workspaces.models.Workspace`. Existing
workspaces get the same treatment via the data migration ``0003_*``;
this handler covers everything created afterwards (admin, web form,
DRF, factory) without each call site having to remember.

Wired in :class:`apps.labels.apps.LabelsConfig.ready`.
"""

from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.workspaces.models import Workspace

from .defaults import seed_default_label_groups


@receiver(post_save, sender=Workspace, dispatch_uid="labels.seed_default_groups")
def _seed_default_groups_on_workspace_create(sender, instance, created, **_):
    """Drop the default group set onto every freshly created workspace.

    Idempotent and create-only — updates to an existing workspace skip
    the seeder, and a team that deleted one of the defaults won't see it
    reappear on the next save. See :func:`seed_default_label_groups`
    for the deduplication rule.
    """
    if not created:
        return
    seed_default_label_groups(instance)
