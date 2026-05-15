"""Authentication-aware middleware for the ``accounts`` app.

Registered in ``acta/settings/base.py`` MIDDLEWARE list. See
docs/decisions/0018-i18n.md for the language-resolution policy.
"""

from django.utils import translation


class UserLanguageMiddleware:
    """Activate the user's stored language preference when set.

    Runs after ``django.middleware.locale.LocaleMiddleware`` and overrides
    its choice when the authenticated user has ``User.language`` set.
    Anonymous users and users with no preference fall through to
    LocaleMiddleware's cookie / Accept-Language resolution.
    """

    def __init__(self, get_response):
        """Capture the next middleware in the chain.

        Args:
            get_response: The downstream callable provided by Django.
        """
        self.get_response = get_response

    def __call__(self, request):
        """Activate user-preferred language for this request, if any.

        Args:
            request: The current :class:`HttpRequest`.

        Returns:
            The downstream response, after restoring the previously
            activated language so the next request on this thread starts
            clean.
        """
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            lang = getattr(user, "language", "") or ""
            if lang:
                translation.activate(lang)
                request.LANGUAGE_CODE = lang
        return self.get_response(request)
