from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse, HttpResponse

# Raster types we render inline (we re-encode them on upload, so they carry
# no script). Everything else — SVG, PDF, Office docs, archives — is served
# as a download so attacker-controlled markup can never execute in our
# origin when opened as a top-level document.
_INLINE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}


def serve_attachment_response(attachment) -> HttpResponse:
    """Build the HTTP response that delivers an attachment's file.

    The caller must have already authorized the request (membership check).
    Honors ``ATTACHMENT_SENDFILE_BACKEND`` (ADR 0025):

    - ``simple`` streams the bytes through Django's ``FileResponse`` —
      correct on any proxy, including the Traefik-only prod stack and dev.
    - ``nginx`` returns an empty response with an ``X-Accel-Redirect``
      header so an nginx sidecar streams the file and frees the ASGI
      worker. Requires that sidecar.

    Raster images are sent ``inline`` (so the panel can preview them);
    everything else is sent as an ``attachment`` download. ``nosniff``
    stops the browser from second-guessing the declared type.

    Args:
        attachment: The :class:`Attachment` to serve.

    Returns:
        A streaming (``simple``) or header-only (``nginx``) response.
    """
    inline = attachment.content_type in _INLINE_TYPES
    disposition = "inline" if inline else "attachment"
    backend = getattr(settings, "ATTACHMENT_SENDFILE_BACKEND", "simple")

    if backend == "nginx":
        response = HttpResponse(content_type=attachment.content_type)
        location = settings.ATTACHMENT_SENDFILE_NGINX_LOCATION.rstrip("/") + "/" + attachment.file.name
        response["X-Accel-Redirect"] = location
    else:
        response = FileResponse(attachment.file.open("rb"), content_type=attachment.content_type)

    response["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(attachment.original_name)}"
    response["X-Content-Type-Options"] = "nosniff"
    return response
