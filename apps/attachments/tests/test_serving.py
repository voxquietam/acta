"""Auth-gated serving: simple (FileResponse) vs nginx (X-Accel-Redirect)."""

from django.http import FileResponse

import pytest

from apps.attachments.serving import serve_attachment_response
from apps.attachments.tests.factories import AttachmentFactory


@pytest.mark.django_db
class TestSimpleBackend:
    def test_image_served_inline(self, settings):
        settings.ATTACHMENT_SENDFILE_BACKEND = "simple"
        att = AttachmentFactory(content_type="image/png", original_name="shot.png")
        resp = serve_attachment_response(att)
        assert isinstance(resp, FileResponse)
        assert resp["Content-Disposition"].startswith("inline;")
        assert "shot.png" in resp["Content-Disposition"]
        assert resp["X-Content-Type-Options"] == "nosniff"

    def test_document_served_as_download(self, settings):
        settings.ATTACHMENT_SENDFILE_BACKEND = "simple"
        att = AttachmentFactory(content_type="text/plain", original_name="notes.txt")
        resp = serve_attachment_response(att)
        assert resp["Content-Disposition"].startswith("attachment;")


@pytest.mark.django_db
class TestNginxBackend:
    def test_emits_x_accel_redirect(self, settings):
        settings.ATTACHMENT_SENDFILE_BACKEND = "nginx"
        settings.ATTACHMENT_SENDFILE_NGINX_LOCATION = "/media-internal/"
        att = AttachmentFactory(content_type="image/png")
        resp = serve_attachment_response(att)
        assert not isinstance(resp, FileResponse)
        assert resp["X-Accel-Redirect"] == "/media-internal/" + att.file.name
        assert resp["Content-Type"] == "image/png"
        assert resp.content == b""
