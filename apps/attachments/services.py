from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from . import images
from .models import Attachment

# Authoritative extension -> content type. The browser-supplied content
# type is never trusted; we derive the stored type from the (validated)
# extension, and image normalization overrides it with the real encoded
# type. Internal — kept in sync with ATTACHMENT_ALLOWED_TYPES in settings.
_EXT_CONTENT_TYPE = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
}

# Leading bytes we insist on for formats with a reliable signature, so a
# renamed payload (``script.js`` → ``report.pdf``) is rejected. The Office
# formats are ZIP containers, hence the PK signature. Text formats
# (txt/md/csv) have no signature and are accepted on extension alone.
_MAGIC = {
    "pdf": (b"%PDF",),
    "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "docx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "xlsx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "pptx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
}


def _extension(filename: str) -> str:
    """Return the lowercase extension of ``filename`` without the dot."""
    return Path(filename).suffix.lower().lstrip(".")


def categorize(uploaded_file) -> str:
    """Resolve and validate the category of an uploaded file.

    Maps the extension to a category in ATTACHMENT_ALLOWED_TYPES, enforces
    the per-category size cap, and sniffs the bytes so a file renamed to a
    whitelisted extension is rejected rather than trusted.

    Args:
        uploaded_file: The incoming ``UploadedFile``.

    Returns:
        The category key (``image`` / ``document`` / ``archive``).

    Raises:
        ValidationError: On a disallowed type, an oversized file, or a
            payload whose bytes don't match its claimed extension.
    """
    ext = _extension(uploaded_file.name)
    category = None
    for name, spec in settings.ATTACHMENT_ALLOWED_TYPES.items():
        if ext in spec["extensions"]:
            category = name
            break
    if category is None:
        raise ValidationError(_("Files of type “.%(ext)s” are not allowed.") % {"ext": ext or "?"})

    cap = settings.ATTACHMENT_MAX_UPLOAD_BYTES[category]
    if uploaded_file.size > cap:
        raise ValidationError(
            _("“%(name)s” is too large — the limit for this type is %(mb)s MB.")
            % {"name": uploaded_file.name, "mb": cap // (1024 * 1024)}
        )

    _sniff(uploaded_file, category, ext)
    return category


def _sniff(uploaded_file, category: str, ext: str) -> None:
    """Reject a file whose bytes don't match its claimed extension.

    Raises:
        ValidationError: If the leading bytes don't match a known
            signature, or a raster image fails to open.
    """
    expected = _MAGIC.get(ext)
    if expected is not None:
        uploaded_file.seek(0)
        head = uploaded_file.read(8)
        uploaded_file.seek(0)
        if not any(head.startswith(sig) for sig in expected):
            raise ValidationError(
                _("“%(name)s” does not look like a valid .%(ext)s file.") % {"name": uploaded_file.name, "ext": ext}
            )
    elif category == "image" and ext != "svg":
        from PIL import Image

        uploaded_file.seek(0)
        try:
            Image.open(uploaded_file).verify()
        except Exception as exc:
            raise ValidationError(_("“%(name)s” is not a readable image.") % {"name": uploaded_file.name}) from exc
        finally:
            uploaded_file.seek(0)


def create_task_attachment(*, task, uploader, uploaded_file) -> Attachment:
    """Validate, normalize, and store an uploaded file against a task.

    Runs inside the caller's ``transaction.atomic()``. Raster images are
    downscaled and stripped of metadata (see :func:`images.normalize_image`);
    documents and archives are stored as-is. The stored content type is
    derived from the validated extension, never the browser's claim.

    Args:
        task: The owning :class:`apps.tasks.models.Task`.
        uploader: The :class:`User` uploading the file.
        uploaded_file: The validated ``UploadedFile``.

    Returns:
        The created, persisted :class:`Attachment`.

    Raises:
        ValidationError: Propagated from :func:`categorize`.
    """
    category = categorize(uploaded_file)
    original_name = Path(uploaded_file.name).name[:255]
    ext = _extension(original_name)
    content_type = _EXT_CONTENT_TYPE.get(ext, "application/octet-stream")
    stored = uploaded_file

    if category == "image" and ext != "svg":
        result = images.normalize_image(
            uploaded_file,
            max_edge=settings.ATTACHMENT_IMAGE_MAX_EDGE,
            quality=settings.ATTACHMENT_IMAGE_QUALITY,
        )
        if result is None:
            uploaded_file.seek(0)
        else:
            stored, content_type = result

    attachment = Attachment(
        workspace=task.project.workspace,
        task=task,
        kind=Attachment.KIND_FILE,
        uploader=uploader,
        original_name=original_name,
        content_type=content_type,
    )
    attachment.clean()
    attachment.file.save(original_name, stored, save=False)
    attachment.size = attachment.file.size
    attachment.save()
    return attachment
