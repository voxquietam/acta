import io

from django.core.files.uploadedfile import SimpleUploadedFile

from PIL import Image
import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.attachments.models import Attachment
from apps.tasks.tests.factories import TaskFactory


def png_upload(name: str = "shot.png", *, size=(64, 64), color=(200, 30, 30)) -> SimpleUploadedFile:
    """Return an ``UploadedFile`` holding a real PNG of the given size."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/png")


def text_upload(name: str = "notes.txt", data: bytes = b"hello world") -> SimpleUploadedFile:
    """Return an ``UploadedFile`` holding a small text payload."""
    return SimpleUploadedFile(name, data, content_type="text/plain")


def pdf_upload(name: str = "doc.pdf", data: bytes = b"%PDF-1.4 minimal") -> SimpleUploadedFile:
    """Return an ``UploadedFile`` whose bytes start with the PDF signature."""
    return SimpleUploadedFile(name, data, content_type="application/pdf")


class AttachmentFactory(DjangoModelFactory):
    class Meta:
        model = Attachment

    task = factory.SubFactory(TaskFactory)
    workspace = factory.LazyAttribute(lambda a: a.task.project.workspace)
    uploader = factory.SubFactory(UserFactory)
    kind = Attachment.KIND_FILE
    file = factory.django.FileField(filename="test.txt", data=b"hello")
    original_name = "test.txt"
    content_type = "text/plain"
    size = 5
