"""Garbage-collect inline-image attachments no longer referenced anywhere.

Inline editor images (``kind=inline_image``) are uploaded the moment they're
pasted/dropped — before the comment / description is necessarily saved, and
they stay behind if the user removes the image from the text or abandons the
create-task modal. There's no in-app scheduler (see the deployment notes),
so this is a management command an operator runs on a cron:

    docker compose exec web python manage.py gc_orphan_attachments

It deletes inline images, older than a grace window, whose serve URL no
longer appears in any task description, project description, or comment body.
The ``post_delete`` signal removes the file blob too. File attachments
(``kind=file``) are never touched — they're owned by a row and cascade with
it.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import timezone

from apps.attachments.models import Attachment
from apps.comments.models import Comment
from apps.projects.models import Project
from apps.tasks.models import Task


class Command(BaseCommand):
    help = "Delete inline-image attachments no longer referenced by any description or comment."

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than-hours",
            type=int,
            default=24,
            help="Only consider inline images uploaded more than this many hours ago (default 24).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        """Delete (or, with ``--dry-run``, count) the unreferenced inline images."""
        cutoff = timezone.now() - timedelta(hours=options["older_than_hours"])
        candidates = Attachment.objects.filter(
            kind=Attachment.KIND_INLINE_IMAGE,
            created_at__lt=cutoff,
        )
        deleted = 0
        for attachment in candidates.iterator():
            ref = reverse("web:serve_attachment", kwargs={"pk": attachment.id})
            referenced = (
                Task.objects.filter(description__contains=ref).exists()
                or Project.objects.filter(description__contains=ref).exists()
                or Comment.objects.filter(body__contains=ref).exists()
            )
            if referenced:
                continue
            deleted += 1
            if not options["dry_run"]:
                attachment.delete()
        verb = "Would delete" if options["dry_run"] else "Deleted"
        self.stdout.write(self.style.SUCCESS(f"{verb} {deleted} orphaned inline image(s)."))
