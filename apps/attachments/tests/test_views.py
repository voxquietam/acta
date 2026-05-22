"""Web endpoints: upload, delete (uploader/admin), and auth-gated serving."""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.attachments.models import Attachment
from apps.attachments.tests.factories import AttachmentFactory, png_upload
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def task_setup(db):
    """Workspace + project + task; owner is a member.

    Returns:
        Tuple ``(workspace, task)``.
    """
    ws = WorkspaceFactory()
    task = TaskFactory(project=ProjectFactory(workspace=ws), reporter=ws.owner)
    return ws, task


def _upload_url(task):
    return reverse(
        "web:upload_task_attachment",
        kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
    )


@pytest.mark.django_db
class TestUpload:
    def test_anonymous_redirected(self, client, task_setup):
        _, task = task_setup
        resp = client.post(_upload_url(task), {"file": png_upload()})
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_member_can_upload(self, client, task_setup):
        ws, task = task_setup
        client.force_login(ws.owner)
        resp = client.post(_upload_url(task), {"file": png_upload()})
        assert resp.status_code == 200
        att = Attachment.objects.get(task=task)
        assert att.uploader_id == ws.owner.id
        assert att.is_image
        assert att.file.storage.exists(att.file.name)

    def test_non_member_gets_404(self, client, task_setup):
        _, task = task_setup
        outsider = UserFactory()
        client.force_login(outsider)
        resp = client.post(_upload_url(task), {"file": png_upload()})
        assert resp.status_code == 404
        assert not Attachment.objects.filter(task=task).exists()

    def test_disallowed_type_shows_error_no_row(self, client, task_setup):
        ws, task = task_setup
        client.force_login(ws.owner)
        from django.core.files.uploadedfile import SimpleUploadedFile

        resp = client.post(_upload_url(task), {"file": SimpleUploadedFile("evil.exe", b"MZ")})
        assert resp.status_code == 200  # panel re-rendered with inline error
        assert not Attachment.objects.filter(task=task).exists()


@pytest.mark.django_db
class TestDelete:
    def test_uploader_can_delete(self, client, task_setup):
        ws, task = task_setup
        att = AttachmentFactory(task=task, workspace=ws, uploader=ws.owner)
        name = att.file.name
        client.force_login(ws.owner)
        resp = client.post(reverse("web:delete_attachment", kwargs={"pk": att.pk}))
        assert resp.status_code == 200
        assert not Attachment.objects.filter(pk=att.pk).exists()
        assert not att.file.storage.exists(name)

    def test_other_member_forbidden(self, client, task_setup):
        ws, task = task_setup
        att = AttachmentFactory(task=task, workspace=ws, uploader=ws.owner)
        member = UserFactory()
        WorkspaceMember.objects.create(user=member, workspace=ws, role=WorkspaceMember.MEMBER)
        client.force_login(member)
        resp = client.post(reverse("web:delete_attachment", kwargs={"pk": att.pk}))
        assert resp.status_code == 403
        assert Attachment.objects.filter(pk=att.pk).exists()

    def test_admin_can_delete_others(self, client, task_setup):
        ws, task = task_setup
        att = AttachmentFactory(task=task, workspace=ws, uploader=ws.owner)
        admin = UserFactory()
        WorkspaceMember.objects.create(user=admin, workspace=ws, role=WorkspaceMember.ADMIN)
        client.force_login(admin)
        resp = client.post(reverse("web:delete_attachment", kwargs={"pk": att.pk}))
        assert resp.status_code == 200
        assert not Attachment.objects.filter(pk=att.pk).exists()


@pytest.mark.django_db
class TestServe:
    def test_member_gets_bytes(self, client, task_setup):
        ws, task = task_setup
        att = AttachmentFactory(task=task, workspace=ws, uploader=ws.owner)
        client.force_login(ws.owner)
        resp = client.get(reverse("web:serve_attachment", kwargs={"pk": att.pk}))
        assert resp.status_code == 200

    def test_non_member_gets_404(self, client, task_setup):
        ws, task = task_setup
        att = AttachmentFactory(task=task, workspace=ws, uploader=ws.owner)
        outsider = UserFactory()
        client.force_login(outsider)
        resp = client.get(reverse("web:serve_attachment", kwargs={"pk": att.pk}))
        assert resp.status_code == 404
