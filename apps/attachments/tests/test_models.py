"""Attachment model — ownership constraint, helpers, file cleanup."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

import pytest

from apps.attachments.models import Attachment
from apps.attachments.tests.factories import AttachmentFactory, text_upload
from apps.comments.tests.factories import CommentFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestOwnershipConstraint:
    def test_exactly_one_owner_passes_clean(self):
        task = TaskFactory()
        att = Attachment(
            workspace=task.project.workspace,
            task=task,
            uploader=task.project.workspace.owner,
            original_name="x.txt",
            content_type="text/plain",
            size=1,
        )
        att.clean()  # no raise

    def test_zero_owners_rejected_by_clean(self):
        ws = WorkspaceFactory()
        att = Attachment(workspace=ws, original_name="x.txt", content_type="text/plain", size=1)
        with pytest.raises(ValidationError):
            att.clean()

    def test_two_owners_rejected_by_clean(self):
        task = TaskFactory()
        comment = CommentFactory(task=task)
        att = Attachment(
            workspace=task.project.workspace,
            task=task,
            comment=comment,
            original_name="x.txt",
            content_type="text/plain",
            size=1,
        )
        with pytest.raises(ValidationError):
            att.clean()

    def test_two_owners_rejected_by_db(self):
        task = TaskFactory()
        comment = CommentFactory(task=task)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Attachment.objects.create(
                    workspace=task.project.workspace,
                    task=task,
                    comment=comment,
                    file=text_upload(),
                    original_name="x.txt",
                    content_type="text/plain",
                    size=1,
                )


@pytest.mark.django_db
class TestHelpers:
    def test_owner_ref_task(self):
        att = AttachmentFactory()
        assert att.owner_ref == ("task", att.task_id)

    def test_owner_ref_project(self):
        project = ProjectFactory()
        att = AttachmentFactory(task=None, project=project, workspace=project.workspace)
        assert att.owner_ref == ("project", project.id)

    def test_is_image(self):
        assert AttachmentFactory(content_type="image/png").is_image is True
        assert AttachmentFactory(content_type="text/plain").is_image is False

    def test_upload_path_is_workspace_owner_scoped(self):
        att = AttachmentFactory()
        assert att.file.name.startswith(f"attachments/{att.workspace_id}/task/{att.task_id}/")


@pytest.mark.django_db
class TestFileCleanup:
    def test_file_removed_on_delete(self):
        att = AttachmentFactory()
        storage, name = att.file.storage, att.file.name
        assert storage.exists(name)
        att.delete()
        assert not storage.exists(name)

    def test_file_removed_on_cascade_from_task(self):
        att = AttachmentFactory()
        storage, name = att.file.storage, att.file.name
        att.task.delete()
        assert not Attachment.objects.filter(pk=att.pk).exists()
        assert not storage.exists(name)
