"""User avatars — normalization, the set service, and the web endpoints."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from PIL import Image
import pytest

from apps.accounts.tests.factories import UserFactory
from apps.attachments.images import normalize_avatar
from apps.attachments.services import set_user_avatar
from apps.attachments.tests.factories import png_upload


@pytest.mark.django_db
class TestNormalizeAvatar:
    def test_non_square_becomes_square_jpeg(self):
        result = normalize_avatar(png_upload("wide.png", size=(120, 40)), size=64)
        assert result is not None
        content, content_type = result
        assert content_type == "image/jpeg"
        image = Image.open(content)
        assert image.size == (64, 64)
        assert image.format == "JPEG"

    def test_non_image_returns_none(self):
        assert normalize_avatar(SimpleUploadedFile("x.txt", b"not an image"), size=64) is None


@pytest.mark.django_db
class TestSetUserAvatar:
    def test_sets_square_avatar(self):
        user = UserFactory()
        set_user_avatar(user=user, uploaded_file=png_upload(size=(100, 60)))
        assert user.avatar
        image = Image.open(user.avatar.open("rb"))
        assert image.size[0] == image.size[1]

    def test_rejects_non_image_extension(self):
        from django.core.exceptions import ValidationError

        user = UserFactory()
        with pytest.raises(ValidationError):
            set_user_avatar(user=user, uploaded_file=SimpleUploadedFile("doc.txt", b"hi"))

    def test_rejects_oversize(self, settings):
        from django.core.exceptions import ValidationError

        settings.ATTACHMENT_MAX_UPLOAD_BYTES = {"image": 8, "document": 8, "archive": 8, "avatar": 8}
        user = UserFactory()
        with pytest.raises(ValidationError):
            set_user_avatar(user=user, uploaded_file=png_upload(size=(200, 200)))

    def test_replacing_avatar_deletes_old_file(self):
        user = UserFactory()
        set_user_avatar(user=user, uploaded_file=png_upload(size=(80, 80)))
        storage, old_name = user.avatar.storage, user.avatar.name
        set_user_avatar(user=user, uploaded_file=png_upload(size=(80, 80)))
        assert user.avatar.name != old_name
        assert not storage.exists(old_name)


@pytest.mark.django_db
class TestAvatarViews:
    def test_upload_sets_avatar(self, client):
        user = UserFactory()
        client.force_login(user)
        resp = client.post(reverse("accounts:upload_avatar"), {"avatar": png_upload(size=(120, 90))})
        assert resp.status_code == 302
        user.refresh_from_db()
        assert user.avatar

    def test_remove_clears_avatar(self, client):
        user = UserFactory()
        set_user_avatar(user=user, uploaded_file=png_upload(size=(80, 80)))
        client.force_login(user)
        resp = client.post(reverse("accounts:remove_avatar"))
        assert resp.status_code == 302
        user.refresh_from_db()
        assert not user.avatar

    def test_serve_returns_bytes_for_authenticated(self, client):
        owner = UserFactory()
        set_user_avatar(user=owner, uploaded_file=png_upload(size=(80, 80)))
        viewer = UserFactory()
        client.force_login(viewer)
        resp = client.get(reverse("accounts:serve_avatar", kwargs={"user_id": owner.id}))
        assert resp.status_code == 200

    def test_serve_404_when_no_avatar(self, client):
        owner = UserFactory()
        viewer = UserFactory()
        client.force_login(viewer)
        resp = client.get(reverse("accounts:serve_avatar", kwargs={"user_id": owner.id}))
        assert resp.status_code == 404

    def test_serve_requires_login(self, client):
        owner = UserFactory()
        set_user_avatar(user=owner, uploaded_file=png_upload(size=(80, 80)))
        resp = client.get(reverse("accounts:serve_avatar", kwargs={"user_id": owner.id}))
        assert resp.status_code == 302
