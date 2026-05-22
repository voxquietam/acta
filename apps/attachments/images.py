from io import BytesIO

from django.core.files.base import ContentFile

from PIL import Image, ImageOps

# Raster formats we re-encode. Anything else (e.g. SVG, which Pillow can't
# open) is left untouched by ``normalize_image``.
_NORMALIZABLE = {
    "JPEG",
    "PNG",
    "WEBP",
    "GIF",
}

# Pillow format -> (pillow save format, content type). We keep the format
# family stable so the stored extension and content type stay truthful;
# the byte-size win comes from downscaling oversized images and dropping
# EXIF, not from transcoding.
_OUTPUT = {
    "JPEG": ("JPEG", "image/jpeg"),
    "PNG": ("PNG", "image/png"),
    "WEBP": ("WEBP", "image/webp"),
}


def normalize_image(uploaded_file, *, max_edge: int, quality: int):
    """Downscale, re-encode, and strip metadata from a raster image.

    Applies EXIF orientation, removes all other metadata, downscales so the
    long edge fits within ``max_edge`` (smaller images are left at their
    size), and re-encodes in the same format family. Animated images and
    formats Pillow cannot open (SVG) are returned unchanged so we never
    flatten an animation or corrupt a vector file.

    Args:
        uploaded_file: The incoming ``UploadedFile``.
        max_edge: Maximum length of the longer edge, in pixels.
        quality: Encoder quality for lossy formats (JPEG/WEBP).

    Returns:
        A tuple ``(ContentFile, content_type)`` with the normalized bytes,
        or ``None`` when the file is not a re-encodable raster (caller
        should store the original as-is).
    """
    uploaded_file.seek(0)
    try:
        image = Image.open(uploaded_file)
        image.load()
    except Exception:
        return None

    fmt = (image.format or "").upper()
    if fmt not in _NORMALIZABLE:
        return None
    if getattr(image, "is_animated", False):
        return None

    image = ImageOps.exif_transpose(image)

    out_fmt, content_type = _OUTPUT.get(fmt, ("PNG", "image/png"))
    if out_fmt == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    if max(image.size) > max_edge:
        image.thumbnail((max_edge, max_edge), Image.LANCZOS)

    buffer = BytesIO()
    save_kwargs = {"format": out_fmt}
    if out_fmt in ("JPEG", "WEBP"):
        save_kwargs["quality"] = quality
    if out_fmt == "JPEG":
        save_kwargs["optimize"] = True
        save_kwargs["progressive"] = True
    elif out_fmt == "PNG":
        save_kwargs["optimize"] = True
    image.save(buffer, **save_kwargs)
    buffer.seek(0)
    return ContentFile(buffer.read()), content_type
