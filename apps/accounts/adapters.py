"""Allauth adapters that gate signup on a valid workspace invite.

Signup stays closed by default (``is_open_for_signup`` returns False
unless the request carries an active :class:`WorkspaceInvite` token).
The invite is the only path for a new account to land — either via
``?invite=<token>`` on the signup URL or via the same token stashed
in the session by ``apps.accounts.views.invite_landing``.

Open-signup never works through the social adapter on principle: we
don't want an arbitrary Google login to auto-create accounts even
when an invite is in flight.
"""

from django.utils import timezone

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

INVITE_SESSION_KEY = "acta_active_invite_token"


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
        from django.db import transaction

        from apps.workspaces.models import WorkspaceMember

        user = super().save_user(request, user, form, commit=commit)
        invite = resolve_invite_from_request(request)
        if invite is None:
            # Defence in depth: ``is_open_for_signup`` would have to
            # have returned True for execution to reach here, but if
            # the invite expired between the two calls we abort
            # without granting workspace access. Email-vs-invite
            # mismatch is caught earlier in ``InviteAwareSignupView``.
            return user

        with transaction.atomic():
            invite.accepted_at = timezone.now()
            invite.save(update_fields=["accepted_at"])
            WorkspaceMember.objects.get_or_create(
                workspace=invite.workspace,
                user=user,
                defaults={"role": invite.role},
            )

        # Clear the session marker so a second concurrent tab can't
        # accidentally consume the same invite a second time.
        request.session.pop(INVITE_SESSION_KEY, None)
        return user


class NoSignupSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Social-login adapter that blocks first-time signup unconditionally.

    Even with an in-flight workspace invite, we don't let an arbitrary
    Google login auto-create a local account — the email on the
    social account might not match the invite, and the trust model
    for invite emails (admin verified the recipient) doesn't carry
    over to a third-party identity provider. Existing users whose
    accounts are already linked to a social provider still sign in
    normally.
    """

    def is_open_for_signup(self, request, sociallogin):
        """Return False so unknown social logins do not auto-create users.

        Args:
            request: The current HttpRequest.
            sociallogin: The :class:`allauth.socialaccount.models.SocialLogin`
                instance describing the in-flight social auth.

        Returns:
            Always False — signup via social provider is disabled.
        """
        return False
