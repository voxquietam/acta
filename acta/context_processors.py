"""Project-wide template context processors."""

from django.conf import settings

from acta.version import get_version


def app_version(request):
    """Expose the app version + changelog link to every template.

    Args:
        request: The active ``HttpRequest`` (unused; required by the
            context-processor contract).

    Returns:
        A dict with ``acta_version`` (e.g. ``"0.2.0"``) and
        ``changelog_url`` (the public CHANGELOG link).
    """
    return {
        "acta_version": get_version(),
        "changelog_url": settings.CHANGELOG_URL,
    }
