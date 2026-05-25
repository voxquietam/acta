"""Allauth adapters that gate signup on a valid workspace invite.

Signup stays closed by default (``is_open_for_signup`` returns False
unless the request carries an active :class:`WorkspaceInvite` token).
The invite is the only path for a new account to land — either via
``?invite=<token>`` on the signup URL or via the same token stashed
in the session by ``apps.accounts.views.invite_landing``.

The social adapter follows the same rule: a Google login can only
*create* a new account when an active invite authorises it — either an
invite token in flight whose address matches the Google account, OR (when
the user clicked "Sign in with Google" directly instead of the invite
link) an active invite addressed to the Google account's email. The
Google auth proves the user owns that address, so the email match is as
safe as the link. An existing account is logged in (and linked) by
verified email without an invite — that path is handled by allauth's
``SOCIALACCOUNT_EMAIL_AUTHENTICATION`` and never reaches signup.
"""

from django.utils import timezone

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

INVITE_SESSION_KEY = "acta_active_invite_token"


def claim_invite_for_user(request, user, invite):
    """Consume the invite and grant the user workspace membership.

    Flips ``accepted_at`` and creates the ``WorkspaceMember`` row inside
    a single transaction, then clears the session marker so a second
    concurrent tab can't consume the same invite twice. Shared by both
    the password and the social signup paths.

    Args:
        request: The current HttpRequest (its session marker is cleared).
        user: The freshly created user to grant membership to.
        invite: The active :class:`WorkspaceInvite` being claimed.
    """
    from django.db import transaction

    from apps.workspaces.models import WorkspaceMember

    with transaction.atomic():
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at"])
        WorkspaceMember.objects.get_or_create(
            workspace=invite.workspace,
            user=user,
            defaults={"role": invite.role},
        )
    request.session.pop(INVITE_SESSION_KEY, None)


def resolve_invite_from_request(request):
    """Return the active :class:`WorkspaceInvite` for the request, or ``None``.

    Order of lookup: ``?invite=`` querystring (the link the recipient
    just clicked) → session-stashed token (set by the landing view so
    the invite survives the redirect to the allauth signup form). Both
    fall through to ``None`` if the token is missing, unknown,
    expired, or already consumed.
    """
    from apps.workspaces.models import WorkspaceInvite

    token = (request.GET.get("invite") or request.session.get(INVITE_SESSION_KEY) or "").strip()
    if not token:
        return None
    try:
        invite = WorkspaceInvite.objects.select_related("workspace").get(token=token)
    except WorkspaceInvite.DoesNotExist:
        return None
    if not invite.is_active:
        return None
    return invite


def _active_invite_for_email(email):
    """Most recent active (unexpired, unconsumed) invite for ``email``, or None.

    ``email`` must already be lower-cased (invite addresses are stored
    lower-cased — see :meth:`WorkspaceInvite.generate`).
    """
    from apps.workspaces.models import WorkspaceInvite

    if not email:
        return None
    return (
        WorkspaceInvite.objects.select_related("workspace")
        .filter(email=email, accepted_at__isnull=True, expires_at__gt=timezone.now())
        .order_by("-created_at")
        .first()
    )


def resolve_social_invite(request, sociallogin):
    """The active invite authorising this social signup, or ``None``.

    Two paths:

    * **Invite link clicked** — a token is in flight (session / querystring);
      it authorises signup only if its address matches the social account.
    * **"Sign in with Google" clicked directly** (no token in flight) — match
      an active invite by the provider's email. A successful Google auth
      proves the user owns that address and the invite was issued to exactly
      it, so this is as safe as the link path, minus the trap of having to
      click the link first.
    """
    social_email = (sociallogin.user.email or "").strip().lower()
    invite = resolve_invite_from_request(request)
    if invite is not None:
        return invite if (social_email and social_email == invite.email) else None
    return _active_invite_for_email(social_email)


class NoSignupAccountAdapter(DefaultAccountAdapter):
    """Allauth adapter — signup gated on a valid workspace invite.

    Acta is invite-only by design. ``is_open_for_signup`` returns
    True only when ``resolve_invite_from_request`` finds an active
    invite for the current request; otherwise allauth renders the
    "signup closed" page exactly as it did before invites existed.
    """

    def is_open_for_signup(self, request):
        """Allow signup only when the request carries an active invite.

        Args:
            request: The current HttpRequest.

        Returns:
            ``True`` when an active :class:`WorkspaceInvite` is
            present in the querystring or session; ``False`` otherwise.
        """
        return resolve_invite_from_request(request) is not None

    def save_user(self, request, user, form, commit=True):
        """Persist the new user, lock the invite, and grant membership.

        Called by allauth at the end of the signup flow with the
        bound, valid signup form. We finish off allauth's default
        ``save_user`` first, then claim the invite atomically:
        flip ``accepted_at`` and create the ``WorkspaceMember`` row.
        Anyone landing without an invite never reaches this code path
        because :meth:`is_open_for_signup` already rejected them.
        """
        user = super().save_user(request, user, form, commit=commit)
        invite = resolve_invite_from_request(request)
        if invite is None:
            # Defence in depth: ``is_open_for_signup`` would have to
            # have returned True for execution to reach here, but if
            # the invite expired between the two calls we abort
            # without granting workspace access. Email-vs-invite
            # mismatch is caught earlier in ``InviteAwareSignupView``.
            return user

        claim_invite_for_user(request, user, invite)
        return user


class NoSignupSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Social-login adapter: signup gated on an invite + matching email.

    A Google login creates a new local account only when the request
    carries an active workspace invite **and** the provider's verified
    email matches the invite address — the invite was issued to a
    specific person, so we won't mint an account for a different Google
    identity that merely holds the link. Logging an *existing* account
    in by verified email needs no invite and never lands here; that is
    allauth's ``SOCIALACCOUNT_EMAIL_AUTHENTICATION`` path.
    """

    def is_open_for_signup(self, request, sociallogin):
        """Allow social signup only with a matching active invite.

        Honours both an in-flight invite token (link path) and a pending
        invite addressed to the social account's email (direct "Sign in with
        Google" path) — see :func:`resolve_social_invite`.

        Args:
            request: The current HttpRequest.
            sociallogin: The :class:`allauth.socialaccount.models.SocialLogin`
                instance describing the in-flight social auth.

        Returns:
            ``True`` when an active invite authorises this signup.
        """
        return resolve_social_invite(request, sociallogin) is not None

    def save_user(self, request, sociallogin, form=None):
        """Persist the social user, then claim the invite + grant membership.

        Reached only after :meth:`is_open_for_signup` confirmed a
        matching active invite, so the invite is re-resolved and
        consumed here exactly as the password signup path does.

        Args:
            request: The current HttpRequest.
            sociallogin: The in-flight :class:`SocialLogin`.
            form: The optional signup form (absent under auto-signup).

        Returns:
            The newly created user.
        """
        user = super().save_user(request, sociallogin, form=form)
        invite = resolve_social_invite(request, sociallogin)
        if invite is not None:
            claim_invite_for_user(request, user, invite)
        return user
