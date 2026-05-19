"""Service-layer helpers for workspace operations.

Keeps non-trivial flows out of model methods and admin classes so the
same logic stays callable from the future workspace-settings page
(Phase 2 of the invite work) and any future API endpoint.
"""

from __future__ import annotations

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from apps.workspaces.models import WorkspaceInvite


def send_invite_email(invite: WorkspaceInvite, *, request=None) -> bool:
    """Send the workspace invite email to ``invite.email``.

    Renders the ``accounts/email/invite.txt`` + ``invite.html``
    templates, wraps both in a ``EmailMultiAlternatives`` so the
    recipient's client picks whichever it prefers, and fires through
    Django's configured mail backend.

    Args:
        invite: The freshly minted :class:`WorkspaceInvite` to send.
        request: Optional :class:`HttpRequest` — when given, used to
            build an absolute URL for the signup link so the email
            body contains ``https://actaspace.com/accounts/invite/<token>/``
            instead of just the relative path. Falls back to a
            settings-based absolute URL when no request is in scope
            (e.g. when called from a management command).

    Returns:
        ``True`` if Django reported the mail dispatched, ``False`` on
        any caught exception. The admin caller is expected to surface
        the failure to the operator without rolling back the invite —
        a failed email isn't worth losing the row, the admin can copy
        the link by hand.
    """
    if request is not None:
        absolute_url = request.build_absolute_uri(invite.signup_url)
    else:
        base = getattr(settings, "ACTA_PUBLIC_BASE_URL", "https://actaspace.com").rstrip("/")
        absolute_url = f"{base}{invite.signup_url}"

    context = {
        "invite": invite,
        "absolute_url": absolute_url,
        "workspace": invite.workspace,
        "role_label": dict(invite._meta.get_field("role").choices).get(invite.role, invite.role),
    }
    # Invite mail body stays English-only by design — Acta admins
    # send these across language boundaries, and a fixed language is
    # easier for the recipient than a guess based on Accept-Language
    # of the server process.
    subject = f"You're invited to {invite.workspace.name} on Acta"
    body_txt = render_to_string("accounts/email/invite.txt", context)
    body_html = render_to_string("accounts/email/invite.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=body_txt,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[invite.email],
    )
    message.attach_alternative(body_html, "text/html")
    # ``fail_silently=True`` returns 0 on failure instead of raising —
    # the admin already has the invite + URL persisted, and a missed
    # email is recoverable (resend / copy link manually).
    sent = message.send(fail_silently=True)
    return bool(sent)
