"""Upload validation, content sniffing, and image normalization."""

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from PIL import Image
import pytest

from apps.attachments.models import Attachment
from apps.attachments.services import categorize, create_task_attachment
from apps.attachments.tests.factories import pdf_upload, png_upload, text_upload
from apps.tasks.tests.factories import TaskFactory


@pytest.mark.django_db
class TestCategorize:
    def test_accepts_known_types(self):
        assert categorize(png_upload()) == "image"
        assert categorize(text_upload()) == "document"
        assert categorize(pdf_upload()) == "document"

    def test_rejects_disallowed_extension(self):
        with pytest.raises(ValidationError):
            categorize(SimpleUploadedFile("evil.exe", b"MZ", content_type="application/octet-stream"))

    def test_rejects_oversize(self, settings):
        settings.ATTACHMENT_MAX_UPLOAD_BYTES = {
            "image": 8,
            "document": 8,
            "archive": 8,
            "avatar": 8,
        }
        with pytest.raises(ValidationError):
            categorize(text_upload(data=b"way past eight bytes"))

    def test_rejects_pdf_with_wrong_magic(self):
        with pytest.raises(ValidationError):
            categorize(SimpleUploadedFile("report.pdf", b"<html>not a pdf", content_type="application/pdf"))

    def test_rejects_image_that_is_not_an_image(self):
        with pytest.raises(ValidationError):
            categorize(SimpleUploadedFile("photo.png", b"definitely not png bytes", content_type="image/png"))


@pytest.mark.django_db
class TestCreateTaskAttachment:
    def test_document_stored_as_is(self):
        task = TaskFactory()
        att = create_task_attachment(
            task=task, uploader=task.project.workspace.owner, uploaded_file=text_upload(data=b"hello world")
        )
        assert att.task_id == task.id
        assert att.kind == Attachment.KIND_FILE
        assert att.content_type == "text/plain"
        assert att.size == len(b"hello world")
        assert att.file.open("rb").read() == b"hello world"

    def test_large_image_is_downscaled(self, settings):
        settings.ATTACHMENT_IMAGE_MAX_EDGE = 2048
        task = TaskFactory()
        att = create_task_attachment(
            task=task,
            uploader=task.project.workspace.owner,
            uploaded_file=png_upload("big.png", size=(3000, 1500)),
        )
        assert att.is_image
        assert att.content_type == "image/png"
        stored = Image.open(att.file.open("rb"))
        assert max(stored.size) <= 2048

    def test_small_image_kept_within_bounds(self):
        task = TaskFactory()
        att = create_task_attachment(
            task=task,
            uploader=task.project.workspace.owner,
            uploaded_file=png_upload("small.png", size=(40, 40)),
        )
        stored = Image.open(att.file.open("rb"))
        assert stored.size == (40, 40)

    def test_size_reflects_stored_bytes(self):
        task = TaskFactory()
        att = create_task_attachment(
            task=task,
            uploader=task.project.workspace.owner,
            uploaded_file=png_upload(size=(64, 64)),
        )
        assert att.size == att.file.size
