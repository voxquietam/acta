"""Development-only middleware for the ``web`` app."""


class NoBrowserCacheMiddleware:
    """Force the browser to revalidate every response while keeping bfcache alive.

    Wired up only in :mod:`acta.settings.dev` so browser cache never
    gets in the way of template / static-file iteration. Should NOT be
    added to production settings — production wants ordinary cache
    headers per asset type.

    We emit ``no-cache, max-age=0, must-revalidate`` instead of
    ``no-store``: that still requires the browser to revalidate with
    the server before reusing a cached response (so template edits show
    up on the next reload, the original goal of this middleware), but
    leaves the page eligible for the back/forward cache. Pages with
    ``no-store`` are blocked from bfcache entirely (Lighthouse audit
    "Page prevented back/forward cache restoration"), which would mean
    Back / Forward navigation re-fetches and re-renders every page.
    """

    def __init__(self, get_response):
        """Capture the next middleware in the chain.

        Args:
            get_response: The downstream callable provided by Django.
        """
        self.get_response = get_response

    def __call__(self, request):
        """Return the downstream response with revalidation forced.

        Args:
            request: The current :class:`HttpRequest`.

        Returns:
            The downstream response with ``Cache-Control`` set so the
            browser revalidates every navigation but bfcache stays
            enabled. ``Pragma`` mirrors the directive for legacy HTTP/1.0
            proxies; ``Expires: 0`` does the same for ancient clients.
        """
        response = self.get_response(request)
        response["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response
