"""Template tags supporting the authentication pages."""

from django import template

register = template.Library()


@register.simple_tag
def google_login_available() -> bool:
    """Return whether a Google login can be offered on the login page.

    True only when a Google :class:`~allauth.socialaccount.models.SocialApp`
    has been configured (in Django admin), so the "Continue with Google"
    button is hidden on instances where OAuth credentials were never set
    up — clicking it there would only yield an allauth error page.

    Returns:
        ``True`` if at least one Google SocialApp row exists.
    """
    from allauth.socialaccount.models import SocialApp

    return SocialApp.objects.filter(provider="google").exists()
