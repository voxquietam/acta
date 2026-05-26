"""Account-related page views."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from allauth.account.views import SignupView

from apps.accounts.adapters import INVITE_SESSION_KEY, resolve_invite_from_request
from apps.accounts.models import ApiToken
from apps.attachments.services import set_user_avatar


class InviteAwareSignupView(SignupView):
    """Override allauth's SignupView to pre-fill + lock the invite email.

    The recipient already proved they own the address by clicking the
    invite link in their inbox — typing it a second time is friction
    and a divergence risk (an LLM-pasted typo would land them in the
    DB under a different email than the invite was issued to).

    Two enforcement points:
      - :meth:`get_form_kwargs` injects ``initial={"email": invite.email}``
        so the GET-rendered form shows the right address.
      - :meth:`form_valid` rechecks the submitted email server-side so
        a determined user editing the read-only input gets a clean
        form error instead of a 500 from a downstream raise.
    """

    def get_form_kwargs(self):
        """Inject the invite email into the form's ``initial`` dict.

        ``get_initial`` alone wasn't enough — allauth's BaseSignupView
        overrides ``get_form_kwargs`` and discards the parent's
        initial in some paths. Setting it directly here makes sure it
        survives to the BoundField rendering.
        """
        kwargs = super().get_form_kwargs()
        invite = resolve_invite_from_request(self.request)
        if invite is not None:
            initial = dict(kwargs.get("initial") or {})
            initial["email"] = invite.email
            kwargs["initial"] = initial
        return kwargs

    def get_context_data(self, **kwargs):
        """Expose ``invite`` to the template so it can show workspace + role hint."""
        context = super().get_context_data(**kwargs)
        context["invite"] = resolve_invite_from_request(self.request)
        return context

    def form_valid(self, form):
        """Reject submission if email diverges from the invite, then delegate.

        Allauth's ``save_user`` runs deep inside ``form.save()`` and a
        ``ValidationError`` from there bubbles up as a 500. We catch
        the mismatch earlier and add a form error so the user sees a
        normal validation message and the form re-renders.
        """
        invite = resolve_invite_from_request(self.request)
        if invite is not None:
            submitted = (form.cleaned_data.get("email") or "").strip().lower()
            if submitted and submitted != invite.email:
                form.add_error(
                    "email",
                    _("Email does not match the invite — it was issued to %(expected)s.") % {"expected": invite.email},
                )
                return self.form_invalid(form)
        return super().form_valid(form)


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
        if lang_changed:
            # The persistent shell (sidebar / topbar) lives outside the
            # boosted ``#app-content`` swap, so a partial swap would leave
            # it in the old language. Tell HTMX to do a full reload so the
            # new language applies everywhere; the cookie rides along.
            translation.activate(lang)
            response = HttpResponse(status=204)
            response["HX-Refresh"] = "true"
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
        # Profile-only change: a redirect that the boosted form follows,
        # swapping ``#app-content`` smoothly (no full-page reload / jump).
        return HttpResponseRedirect(reverse("accounts:settings"))
    # ``api_tokens`` powers the API tokens section. ``created_secret``
    # is a one-shot flash value populated by ``create_api_token`` —
    # the plain token, shown ONCE on redirect back here, then cleared.
    created_secret = request.session.pop("created_api_token_secret", None)
    created_name = request.session.pop("created_api_token_name", None)
    from apps.telegram.services import link_deep_link

    return render(
        request,
        "accounts/settings.html",
        {
            "languages": list(settings.LANGUAGES),
            "api_tokens": list(user.api_tokens.order_by("revoked_at", "-created_at")),
            "created_api_token_secret": created_secret,
            "created_api_token_name": created_name,
            "telegram_account": getattr(user, "telegram", None),
            "telegram_link_url": link_deep_link(user),
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


@login_required
@require_POST
def delete_api_token(request, token_id: int):
    """Permanently delete one of the current user's API tokens.

    Unlike :func:`revoke_api_token` (a soft-delete that keeps the row for
    the audit trail), this drops the row entirely so it disappears from
    the settings list. Useful for clearing out stale / mistaken tokens.
    A revoked token can also be deleted to tidy the list.

    Args:
        request: POST request.
        token_id: PK of the token to delete. Scoped to the current user
            via ``get_object_or_404`` — users can't delete other users'
            tokens.
    """
    token = get_object_or_404(ApiToken, pk=token_id, user=request.user)
    name = token.name
    token.delete()
    messages.success(request, _("Token “%(name)s” deleted.") % {"name": name})
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


@require_POST
@login_required
def upload_avatar(request):
    """Set the current user's avatar from an uploaded image.

    Validates + square-crops + resizes via
    :func:`apps.attachments.services.set_user_avatar`. For an HTMX request
    re-renders just the avatar block (the new photo on success, or an inline
    error) so the settings page doesn't reload; otherwise falls back to a
    redirect with a flash message.
    """
    is_htmx = request.headers.get("HX-Request") == "true"
    upload = request.FILES.get("avatar")
    if upload is None:
        return HttpResponseBadRequest("avatar required")
    try:
        set_user_avatar(user=request.user, uploaded_file=upload)
    except ValidationError as exc:
        error = "; ".join(exc.messages)
        if is_htmx:
            return render(request, "accounts/_avatar_block.html", {"avatar_error": error})
        messages.error(request, error)
        return HttpResponseRedirect(reverse("accounts:settings"))
    if is_htmx:
        return render(request, "accounts/_avatar_block.html")
    messages.success(request, _("Avatar updated."))
    return HttpResponseRedirect(reverse("accounts:settings"))


@require_POST
@login_required
def remove_avatar(request):
    """Remove the current user's avatar, reverting to the colour circle.

    HTMX swaps the avatar block in place; a non-HTMX post redirects back to
    settings with a flash message.
    """
    is_htmx = request.headers.get("HX-Request") == "true"
    if request.user.avatar:
        request.user.avatar.delete(save=True)
        if not is_htmx:
            messages.success(request, _("Avatar removed."))
    if is_htmx:
        return render(request, "accounts/_avatar_block.html")
    return HttpResponseRedirect(reverse("accounts:settings"))


@login_required
def serve_avatar(request, user_id: int):
    """Stream a user's avatar image.

    Any authenticated user may view any avatar — a profile photo is shown
    wherever the user appears (comments, assignees, member lists), possibly
    across workspaces, so this is login-gated but not workspace-scoped.
    Avatars are normalized to JPEG on upload.
    """
    user = get_object_or_404(get_user_model(), pk=user_id)
    if not user.avatar:
        raise Http404("no avatar")
    try:
        handle = user.avatar.open("rb")
    except (FileNotFoundError, OSError):
        # DB references an avatar whose file is gone from storage (e.g. the
        # media volume was reset, or the row predates this deployment's
        # uploads). Degrade to 404 so the UI shows its initials-circle
        # fallback instead of a hard 500 on every avatar.
        raise Http404("avatar file missing")
    response = FileResponse(handle, content_type="image/jpeg")
    response["Cache-Control"] = "private, max-age=300"
    return response
