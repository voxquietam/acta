"""Account-related page views."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.accounts.adapters import INVITE_SESSION_KEY
from apps.accounts.models import ApiToken


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


@login_required
def user_settings(request):
    """Render the user-settings page or apply a profile update.

    Hosts the profile fields (``first_name`` / ``last_name``) and the
    preference fields (``language``) on a single page. The language
    switcher used to live in the topbar; it's been moved here so the
    topbar stays focused on workspace navigation. The same
    ``set_language`` cookie + UI activation path is reused — the
    settings POST just delegates to that view's helper logic so the
    cookie + reload semantics stay identical.

    GET → renders ``accounts/settings.html``.
    POST → updates the user record, persists the language cookie if
    the picked value changed, redirects back to ``/settings/`` (so a
    hard refresh shows the new state) with a flash message ready for
    the toast layer via ``HX-Trigger`` headers in a follow-up pass.
    """
    user = request.user
    if request.method == "POST":
        first = (request.POST.get("first_name") or "").strip()[:150]
        last = (request.POST.get("last_name") or "").strip()[:150]
        lang = (request.POST.get("language") or "").strip()
        allowed = {code for code, _ in settings.LANGUAGES}
        updates = []
        if user.first_name != first:
            user.first_name = first
            updates.append("first_name")
        if user.last_name != last:
            user.last_name = last
            updates.append("last_name")
        lang_changed = False
        if lang and lang in allowed and getattr(user, "language", "") != lang:
            user.language = lang
            updates.append("language")
            lang_changed = True
        if updates:
            user.save(update_fields=updates)
        response = HttpResponseRedirect(reverse("accounts:settings"))
        if lang_changed:
            translation.activate(lang)
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
    # ``api_tokens`` powers the API tokens section. ``created_secret``
    # is a one-shot flash value populated by ``create_api_token`` —
    # the plain token, shown ONCE on redirect back here, then cleared.
    created_secret = request.session.pop("created_api_token_secret", None)
    created_name = request.session.pop("created_api_token_name", None)
    return render(
        request,
        "accounts/settings.html",
        {
            "languages": list(settings.LANGUAGES),
            "api_tokens": list(user.api_tokens.order_by("revoked_at", "-created_at")),
            "created_api_token_secret": created_secret,
            "created_api_token_name": created_name,
        },
    )


@login_required
@require_POST
def create_api_token(request):
    """Mint a new API token for the current user.

    Stashes the plain secret in ``request.session`` for one-shot
    rendering on the redirect target — the secret is shown ONCE on
    the settings page, then cleared. The DB only stores the hash; if
    the user navigates away before copying, the token is unusable and
    they need to revoke + recreate.

    Args:
        request: POST with a ``name`` form field.

    Returns:
        Redirect to ``accounts:settings``. On success, the next render
        of that page surfaces the plain secret in a copy-once panel.
    """
    name = (request.POST.get("name") or "").strip()[:80]
    if not name:
        messages.error(request, _("Token name is required."))
        return HttpResponseRedirect(reverse("accounts:settings"))
    # Don't shadow ``_`` (gettext_lazy) with a throwaway tuple slot —
    # Python promotes it to local-scope for the whole function and
    # the ``_()`` call above would fail with UnboundLocalError.
    new_token, plain = ApiToken.generate(user=request.user, name=name)
    del new_token  # only the plain secret matters from here on
    # One-shot flash: the secret is read-and-cleared on the next render.
    request.session["created_api_token_secret"] = plain
    request.session["created_api_token_name"] = name
    return HttpResponseRedirect(reverse("accounts:settings"))


@login_required
@require_POST
def revoke_api_token(request, token_id: int):
    """Revoke one of the current user's API tokens.

    Soft-delete: sets ``revoked_at`` instead of deleting the row, so
    the audit trail (when it was minted, when it was last used)
    survives. The auth backend rejects revoked tokens at every
    subsequent request, so the integration that owned this token
    stops working immediately.

    Args:
        request: POST request.
        token_id: PK of the token to revoke. Scoped to the current
            user via ``get_object_or_404`` — users can't revoke
            other users' tokens.
    """
    token = get_object_or_404(ApiToken, pk=token_id, user=request.user)
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        messages.success(request, _("Token “%(name)s” revoked.") % {"name": token.name})
    return HttpResponseRedirect(reverse("accounts:settings"))


def invite_accept(request, token: str):
    """Landing page for an invite URL.

    The recipient clicks the link in their email; this view verifies
    the token is still active and stashes it in the session so the
    ``NoSignupAccountAdapter`` recognises the invite on every step of
    the allauth signup flow — even the POST that submits the form,
    where the querystring would otherwise have been dropped.

    Failure paths:
      - Unknown / consumed / expired token → redirect to login with a
        flash explaining the link is no longer valid.
      - Already-authenticated user → redirect to the home page; they
        don't need to sign up again.

    The success redirect points at allauth's signup view with the
    token both in the session *and* the querystring for defence in
    depth.
    """
    from apps.workspaces.models import WorkspaceInvite

    if request.user.is_authenticated:
        messages.info(
            request,
            _("You're already signed in — share the invite link with someone who needs an account."),
        )
        return redirect("/")

    try:
        invite = WorkspaceInvite.objects.select_related("workspace").get(token=token)
    except WorkspaceInvite.DoesNotExist:
        messages.error(request, _("That invite link is not valid."))
        return redirect("account_login")

    if not invite.is_active:
        if invite.is_consumed:
            messages.error(request, _("That invite link has already been used."))
        else:
            messages.error(request, _("That invite link has expired — ask the admin to resend it."))
        return redirect("account_login")

    request.session[INVITE_SESSION_KEY] = invite.token
    # Keep the querystring as well so allauth's signup template can
    # show ``invite.workspace.name`` in the page header without us
    # having to override the template.
    signup_url = reverse("account_signup")
    return HttpResponseRedirect(f"{signup_url}?invite={invite.token}")
