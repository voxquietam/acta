"""Comment attachments — service + posting a comment with files."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

import pytest

from apps.attachments.models import Attachment
from apps.attachments.services import create_comment_attachment
from apps.attachments.tests.factories import png_upload, text_upload
from apps.comments.models import Comment
from apps.comments.tests.factories import CommentFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def task_setup(db):
    """Workspace + project + task; owner is a member."""
    ws = WorkspaceFactory()
    task = TaskFactory(project=ProjectFactory(workspace=ws), reporter=ws.owner)
    return ws, task


@pytest.mark.django_db
class TestService:
    def test_creates_comment_attachment_with_derived_workspace(self, task_setup):
        ws, task = task_setup
        comment = CommentFactory(task=task, author=ws.owner)
        att = create_comment_attachment(comment=comment, uploader=ws.owner, uploaded_file=text_upload())
        assert att.comment_id == comment.id
        assert att.task_id is None
        assert att.workspace_id == task.project.workspace_id


@pytest.mark.django_db
class TestPostCommentWithFiles:
    def _url(self, task):
        return reverse(
            "web:post_comment",
            kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
        )

    def test_comment_with_file(self, client, task_setup):
        ws, task = task_setup
        client.force_login(ws.owner)
        resp = client.post(self._url(task), {"body": "see attached", "file": png_upload("shot.png")})
        assert resp.status_code == 200
        comment = Comment.objects.get(task=task)
        assert comment.attachments.count() == 1
        assert comment.attachments.first().comment_id == comment.id

    def test_file_only_comment_allowed(self, client, task_setup):
        ws, task = task_setup
        client.force_login(ws.owner)
        resp = client.post(self._url(task), {"body": "", "file": text_upload("notes.txt")})
        assert resp.status_code == 200
        comment = Comment.objects.get(task=task)
        assert comment.body == ""
        assert comment.attachments.count() == 1

    def test_multiple_files(self, client, task_setup):
        ws, task = task_setup
        client.force_login(ws.owner)
        resp = client.post(self._url(task), {"body": "two", "file": [png_upload("a.png"), text_upload("b.txt")]})
        assert resp.status_code == 200
        assert Comment.objects.get(task=task).attachments.count() == 2

    def test_invalid_file_rejected_no_comment(self, client, task_setup):
        ws, task = task_setup
        client.force_login(ws.owner)
        resp = client.post(self._url(task), {"body": "x", "file": SimpleUploadedFile("evil.exe", b"MZ")})
        assert resp.status_code == 400
        assert not Comment.objects.filter(task=task).exists()
        assert not Attachment.objects.filter(task__isnull=True).exists()

    def test_empty_comment_no_file_rejected(self, client, task_setup):
        ws, task = task_setup
        client.force_login(ws.owner)
        resp = client.post(self._url(task), {"body": ""})
        assert resp.status_code == 400
        assert not Comment.objects.filter(task=task).exists()
