"""Daily auto-archive job for stale ``done`` tasks.

Walks every :class:`Workspace` that has ``auto_archive_done_after_days``
configured (non-null) and archives every task whose status is
``done``, whose ``archived_at`` is still null, and whose ``updated_at``
is older than the workspace's threshold. Emits a
``system.task.archived`` activity event per affected row with
``actor=None`` (the system is the actor; per
``apps/activity/tests/test_log_event.py::test_system_event_has_null_actor``).

Intended to be invoked from a daily cron (or a container-level
scheduler — see ``docs/decisions/0015-real-time.md`` for the deploy
shape). Idempotent: re-runs are a no-op once the cutoff window
passes the same set of tasks.

Usage::

    python manage.py archive_stale_done_tasks
    python manage.py archive_stale_done_tasks --dry-run
    python manage.py archive_stale_done_tasks --workspace acme
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.activity.models import ActivityLog
from apps.tasks.models import Task
from apps.workspaces.models import Workspace


class Command(BaseCommand):
    """Archive done tasks past each workspace's auto-archive threshold."""

    help = "Archive done tasks older than each workspace's auto_archive_done_after_days setting"

    def add_arguments(self, parser):
        """Register CLI flags.

        Args:
            parser: The :class:`argparse.ArgumentParser` Django passes in.
        """
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be archived without writing any changes",
        )
        parser.add_argument(
            "--workspace",
            type=str,
            default=None,
            help="Limit to a single workspace by slug (otherwise: all workspaces with auto-archive enabled)",
        )

    def handle(self, *args, **options):
        """Drive the per-workspace archive loop and report totals.

        Args:
            *args: Positional args from Django's command dispatcher (unused).
            **options: Parsed CLI flags — ``dry_run`` and ``workspace``.
        """
        dry_run = options["dry_run"]
        workspace_slug = options["workspace"]

        workspaces = Workspace.objects.filter(auto_archive_done_after_days__isnull=False)
        if workspace_slug:
            workspaces = workspaces.filter(slug=workspace_slug)

        total_archived = 0
        per_workspace: list[tuple[str, int]] = []
        for workspace in workspaces:
            count = self._archive_workspace(workspace, dry_run=dry_run)
            per_workspace.append((workspace.slug, count))
            total_archived += count

        verb = "would archive" if dry_run else "archived"
        for slug, count in per_workspace:
            self.stdout.write(f"  {slug}: {verb} {count}")
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb.capitalize()} {total_archived} task(s) across {len(per_workspace)} workspace(s)"
            ),
        )

    def _archive_workspace(self, workspace: Workspace, *, dry_run: bool) -> int:
        """Archive stale done tasks in one workspace.

        Args:
            workspace: The :class:`Workspace` to process. Must have a
                non-null ``auto_archive_done_after_days`` (caller filters).
            dry_run: When True, count the matching rows but do not write.

        Returns:
            Number of tasks that were (or would be) archived.
        """
        cutoff = timezone.now() - datetime.timedelta(days=workspace.auto_archive_done_after_days)
        stale = Task.objects.filter(
            project__workspace=workspace,
            status=Task.STATUS_DONE,
            archived_at__isnull=True,
            updated_at__lt=cutoff,
        )
        if dry_run:
            return stale.count()

        with transaction.atomic():
            # ``select_for_update(skip_locked=True)`` keeps the auto-archive
            # job from racing a concurrent manual archive: any row whose
            # transaction is mid-flight is silently skipped here and will
            # be picked up by the next daily run if still eligible.
            locked = list(
                stale.select_for_update(skip_locked=True).values_list("id", "project_id"),
            )
            if not locked:
                return 0
            stale_ids = locked
            now = timezone.now()
            Task.objects.filter(id__in=[tid for tid, _ in stale_ids]).update(
                archived_at=now,
                updated_at=now,
            )
            ActivityLog.objects.bulk_create(
                [
                    ActivityLog(
                        workspace=workspace,
                        project_id=project_id,
                        actor=None,
                        event_type="system.task.archived",
                        target_type=ActivityLog.TARGET_TASK,
                        target_id=task_id,
                        payload={
                            "source": "system",
                            "reason": "stale",
                            "after_days": workspace.auto_archive_done_after_days,
                        },
                    )
                    for task_id, project_id in stale_ids
                ],
            )
            return len(stale_ids)
