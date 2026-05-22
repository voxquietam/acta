"""Inline description images — service, upload endpoints, markdown render."""

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

import pytest

from apps.attachments.models import Attachment
from apps.attachments.services import create_inline_image
from apps.attachments.tests.factories import png_upload, text_upload
from apps.common.markdown import render_markdown
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    task = TaskFactory(project=project, reporter=ws.owner)
    return ws, project, task


@pytest.mark.django_db
class TestService:
    def test_creates_inline_image_on_task(self, setup):
        ws, _, task = setup
        att = create_inline_image(
            owner_field="task",
            owner=task,
            workspace=ws,
            uploader=ws.owner,
            uploaded_file=png_upload(size=(80, 80)),
        )
        assert att.kind == Attachment.KIND_INLINE_IMAGE
        assert att.task_id == task.id
        assert att.is_image

    def test_rejects_non_image(self, setup):
        ws, _, task = setup
        with pytest.raises(ValidationError):
            create_inline_image(
                owner_field="task", owner=task, workspace=ws, uploader=ws.owner, uploaded_file=text_upload()
            )

    def test_rejects_svg(self, setup):
        ws, _, task = setup
        svg = SimpleUploadedFile("x.svg", b"<svg></svg>", content_type="image/svg+xml")
        with pytest.raises(ValidationError):
            create_inline_image(owner_field="task", owner=task, workspace=ws, uploader=ws.owner, uploaded_file=svg)


@pytest.mark.django_db
class TestUploadViews:
    def test_task_member_uploads(self, client, setup):
        ws, _, task = setup
        client.force_login(ws.owner)
        url = reverse(
            "web:upload_task_inline_image",
            kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
        )
        resp = client.post(url, {"image": png_upload(size=(64, 64))})
        assert resp.status_code == 200
        assert "/attachments/" in resp.json()["url"]
        assert Attachment.objects.filter(task=task, kind=Attachment.KIND_INLINE_IMAGE).count() == 1

    def test_task_non_member_404(self, client, setup):
        from apps.accounts.tests.factories import UserFactory

        ws, _, task = setup
        client.force_login(UserFactory())
        url = reverse(
            "web:upload_task_inline_image",
            kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
        )
        resp = client.post(url, {"image": png_upload()})
        assert resp.status_code == 404

    def test_task_non_image_400(self, client, setup):
        ws, _, task = setup
        client.force_login(ws.owner)
        url = reverse(
            "web:upload_task_inline_image",
            kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
        )
        resp = client.post(url, {"image": text_upload()})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_project_member_uploads(self, client, setup):
        ws, project, _ = setup
        client.force_login(ws.owner)
        url = reverse("web:upload_project_inline_image", kwargs={"slug_prefix": project.slug_prefix})
        resp = client.post(url, {"image": png_upload(size=(64, 64))})
        assert resp.status_code == 200
        assert Attachment.objects.filter(project=project, kind=Attachment.KIND_INLINE_IMAGE).count() == 1


class TestMarkdownRoundTrip:
    def test_relative_image_src_survives_sanitizer(self):
        html = render_markdown("![shot](/attachments/5/)")
        assert "<img" in html
        assert 'src="/attachments/5/"' in html
