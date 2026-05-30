"""Custom middleware for the ``web`` app."""

from django.middleware.gzip import GZipMiddleware


class GZipSkipSseMiddleware(GZipMiddleware):
    """``GZipMiddleware`` that leaves ``text/event-stream`` responses alone.

    The stock ``GZipMiddleware`` wraps streaming responses with
    ``compress_sequence``, which routes every yielded chunk through a
    ``GzipFile`` writer. Gzip needs ~32 KB of input before its window
    flushes, so individual SSE events (typically a few hundred bytes
    each) sit in gzip's internal buffer and never reach the browser
    until enough later data accumulates — visible to the user as "SSE
    looks open but events only arrive on the next reload."

    We catch the SSE content type up-front (the response object reaches
    middleware with its headers already set by the view) and return it
    untouched. Every other response flows through gzip as before.

    Wire this in place of ``django.middleware.gzip.GZipMiddleware`` at
    the top of ``MIDDLEWARE``.
    """

    def process_response(self, request, response):
        """Skip gzip for SSE; delegate everything else to the parent.

        Args:
            request: Current :class:`HttpRequest`.
            response: Response produced downstream.

        Returns:
            The response unchanged when its ``Content-Type`` starts with
            ``text/event-stream``, otherwise the gzipped version from
            :meth:`GZipMiddleware.process_response`.
        """
        if response.get("Content-Type", "").startswith("text/event-stream"):
            return response
        return super().process_response(request, response)


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
