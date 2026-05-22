"""Content-addressed dedup — identical bytes share one blob, ref-counted delete."""

import pytest

from apps.attachments.services import create_task_attachment
from apps.attachments.tests.factories import png_upload, text_upload
from apps.tasks.tests.factories import TaskFactory


@pytest.mark.django_db
class TestDedup:
    def _two(self, make):
        task = TaskFactory()
        owner = task.project.workspace.owner
        a = create_task_attachment(task=task, uploader=owner, uploaded_file=make())
        b = create_task_attachment(task=task, uploader=owner, uploaded_file=make())
        return a, b

    def test_identical_bytes_share_one_blob(self):
        a, b = self._two(lambda: text_upload(data=b"same content"))
        assert a.content_hash and a.content_hash == b.content_hash
        assert a.file.name == b.file.name
        assert a.file.storage.exists(a.file.name)

    def test_identical_images_dedup(self):
        a, b = self._two(lambda: png_upload(size=(60, 60)))
        assert a.content_hash == b.content_hash
        assert a.file.name == b.file.name

    def test_different_bytes_separate_blobs(self):
        task = TaskFactory()
        owner = task.project.workspace.owner
        a = create_task_attachment(task=task, uploader=owner, uploaded_file=text_upload(data=b"one"))
        b = create_task_attachment(task=task, uploader=owner, uploaded_file=text_upload(data=b"two"))
        assert a.content_hash != b.content_hash
        assert a.file.name != b.file.name

    def test_refcounted_delete_keeps_blob_until_last(self):
        a, b = self._two(lambda: text_upload(data=b"shared blob"))
        name, storage = a.file.name, a.file.storage
        a.delete()
        assert storage.exists(name)  # b still references the blob
        b.delete()
        assert not storage.exists(name)  # last reference gone → blob removed
