"""Development-only middleware for the ``web`` app."""


class NoBrowserCacheMiddleware:
    """Send aggressive no-cache headers on every response.

    Wired up only in :mod:`acta.settings.dev` so that browser cache
    never gets in the way of template / static-file iteration. Should
    NOT be added to production settings — production wants ordinary
    cache headers per asset type.
    """

    def __init__(self, get_response):
        """Capture the next middleware in the chain.

        Args:
            get_response: The downstream callable provided by Django.
        """
        self.get_response = get_response

    def __call__(self, request):
        """Return the downstream response with cache busted.

        Args:
            request: The current :class:`HttpRequest`.

        Returns:
            The downstream response with ``Cache-Control``, ``Pragma``
            and ``Expires`` set to forbid any browser caching.
        """
        response = self.get_response(request)
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response
