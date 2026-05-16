"""Account-related page views."""

from django.conf import settings
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.urls import reverse
from django.utils import translation
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext
from django.views.decorators.http import require_POST


@require_POST
def set_language(request):
    """Persist the user's chosen UI language and redirect back.

    The form is expected to POST a ``language`` field whose value must be
    in ``settings.LANGUAGES``. For authenticated users the choice is also
    written to :attr:`User.language` so it survives logout. The
    ``django_language`` cookie is set in all cases so anonymous users get
    a sticky choice too. See docs/decisions/0018-i18n.md.

    The post-redirect target is taken from the ``Referer`` header but
    validated with ``url_has_allowed_host_and_scheme`` against the
    request's own host — without this check the form would be an open
    redirect (a crafted ``Referer`` would send the user off-site after
    submit).

    Args:
        request: The DRF/Django :class:`HttpRequest` carrying the form.

    Returns:
        A :class:`HttpResponseRedirect` to the referring page (or to
        the dashboard if the ``Referer`` is missing / off-site), with
        the language cookie set and the user record updated when
        applicable.
    """
    lang = request.POST.get("language", "").strip()
    allowed = {code for code, _ in settings.LANGUAGES}
    if lang not in allowed:
        return HttpResponseBadRequest(gettext("Unsupported language"))

    if request.user.is_authenticated and getattr(request.user, "language", None) != lang:
        request.user.language = lang
        request.user.save(update_fields=["language"])

    translation.activate(lang)
    referer = request.META.get("HTTP_REFERER") or ""
    if referer and url_has_allowed_host_and_scheme(
        referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = referer
    else:
        next_url = reverse("web:dashboard")
    response = HttpResponseRedirect(next_url)
    response.set_cookie(
        settings.LANGUAGE_COOKIE_NAME,
        lang,
        max_age=settings.LANGUAGE_COOKIE_AGE,
        path=settings.LANGUAGE_COOKIE_PATH,
        domain=settings.LANGUAGE_COOKIE_DOMAIN,
        secure=settings.LANGUAGE_COOKIE_SECURE,
        httponly=settings.LANGUAGE_COOKIE_HTTPONLY,
        samesite=settings.LANGUAGE_COOKIE_SAMESITE,
    )
    return response
