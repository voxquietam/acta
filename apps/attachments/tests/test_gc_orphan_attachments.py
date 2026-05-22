"""GC of orphaned inline images (the gc_orphan_attachments command)."""

from django.core.management import call_command
from django.urls import reverse

import pytest

from apps.attachments.models import Attachment
from apps.attachments.tests.factories import AttachmentFactory
from apps.comments.tests.factories import CommentFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory


def _inline_image(project=None):
    """An inline-image attachment owned by a project (the create-modal case)."""
    project = project or ProjectFactory()
    return AttachmentFactory(
        task=None,
        project=project,
        workspace=project.workspace,
        kind=Attachment.KIND_INLINE_IMAGE,
        content_type="image/png",
    )


def _ref(att):
    return reverse("web:serve_attachment", kwargs={"pk": att.id})


@pytest.mark.django_db
class TestGcOrphanAttachments:
    def test_deletes_unreferenced_inline_image(self):
        att = _inline_image()
        storage, name = att.file.storage, att.file.name
        call_command("gc_orphan_attachments", "--older-than-hours=0")
        assert not Attachment.objects.filter(pk=att.pk).exists()
        assert not storage.exists(name)

    def test_keeps_referenced_in_task_description(self):
        task = TaskFactory()
        att = _inline_image(project=task.project)
        task.description = f"intro ![shot]({_ref(att)}) outro"
        task.save(update_fields=["description"])
        call_command("gc_orphan_attachments", "--older-than-hours=0")
        assert Attachment.objects.filter(pk=att.pk).exists()

    def test_keeps_referenced_in_project_description(self):
        project = ProjectFactory()
        att = _inline_image(project=project)
        project.description = f"![x]({_ref(att)})"
        project.save(update_fields=["description"])
        call_command("gc_orphan_attachments", "--older-than-hours=0")
        assert Attachment.objects.filter(pk=att.pk).exists()

    def test_keeps_referenced_in_comment(self):
        task = TaskFactory()
        att = _inline_image(project=task.project)
        CommentFactory(task=task, body=f"![x]({_ref(att)})")
        call_command("gc_orphan_attachments", "--older-than-hours=0")
        assert Attachment.objects.filter(pk=att.pk).exists()

    def test_ignores_file_attachments(self):
        att = AttachmentFactory()  # kind=file, task-owned panel attachment
        call_command("gc_orphan_attachments", "--older-than-hours=0")
        assert Attachment.objects.filter(pk=att.pk).exists()

    def test_dry_run_keeps_everything(self):
        att = _inline_image()
        call_command("gc_orphan_attachments", "--older-than-hours=0", "--dry-run")
        assert Attachment.objects.filter(pk=att.pk).exists()

    def test_grace_window_keeps_recent(self):
        att = _inline_image()
        call_command("gc_orphan_attachments", "--older-than-hours=24")
        assert Attachment.objects.filter(pk=att.pk).exists()
