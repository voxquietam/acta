"""GC of orphaned inline images (the gc_orphan_attachments command)."""

from datetime import timedelta

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.attachments.models import Attachment
from apps.attachments.tests.factories import AttachmentFactory
from apps.comments.tests.factories import CommentFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory


def _inline_image(project=None, age_hours=48):
    """An inline-image attachment owned by a project, optionally backdated.

    Backdating ``created_at`` (rather than passing ``--older-than-hours=0``)
    keeps the grace-window comparison deterministic — a fresh row racing the
    command's ``now()`` could fall on either side of the cutoff.
    """
    project = project or ProjectFactory()
    att = AttachmentFactory(
        task=None,
        project=project,
        workspace=project.workspace,
        kind=Attachment.KIND_INLINE_IMAGE,
        content_type="image/png",
    )
    if age_hours:
        Attachment.objects.filter(pk=att.pk).update(created_at=timezone.now() - timedelta(hours=age_hours))
        att.refresh_from_db()
    return att


def _ref(att):
    return reverse("web:serve_attachment", kwargs={"pk": att.id})


@pytest.mark.django_db
class TestGcOrphanAttachments:
    def test_deletes_unreferenced_inline_image(self):
        att = _inline_image()
        storage, name = att.file.storage, att.file.name
        call_command("gc_orphan_attachments")
        assert not Attachment.objects.filter(pk=att.pk).exists()
        assert not storage.exists(name)

    def test_keeps_referenced_in_task_description(self):
        task = TaskFactory()
        att = _inline_image(project=task.project)
        task.description = f"intro ![shot]({_ref(att)}) outro"
        task.save(update_fields=["description"])
        call_command("gc_orphan_attachments")
        assert Attachment.objects.filter(pk=att.pk).exists()

    def test_keeps_referenced_in_project_description(self):
        project = ProjectFactory()
        att = _inline_image(project=project)
        project.description = f"![x]({_ref(att)})"
        project.save(update_fields=["description"])
        call_command("gc_orphan_attachments")
        assert Attachment.objects.filter(pk=att.pk).exists()

    def test_keeps_referenced_in_comment(self):
        task = TaskFactory()
        att = _inline_image(project=task.project)
        CommentFactory(task=task, body=f"![x]({_ref(att)})")
        call_command("gc_orphan_attachments")
        assert Attachment.objects.filter(pk=att.pk).exists()

    def test_ignores_file_attachments(self):
        att = AttachmentFactory()  # kind=file, task-owned panel attachment
        Attachment.objects.filter(pk=att.pk).update(created_at=timezone.now() - timedelta(hours=48))
        call_command("gc_orphan_attachments")
        assert Attachment.objects.filter(pk=att.pk).exists()

    def test_dry_run_keeps_everything(self):
        att = _inline_image()
        call_command("gc_orphan_attachments", "--dry-run")
        assert Attachment.objects.filter(pk=att.pk).exists()

    def test_grace_window_keeps_recent(self):
        att = _inline_image(age_hours=0)  # just uploaded → within the 24h window
        call_command("gc_orphan_attachments")
        assert Attachment.objects.filter(pk=att.pk).exists()
