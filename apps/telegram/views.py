"""HTTP surfaces for the Telegram integration.

* ``telegram_webhook`` — where Telegram POSTs updates in production
  (secret in the path + the bot-api secret-token header). CSRF-exempt;
  always answers 200 so Telegram doesn't retry-storm on a bad payload.
* ``telegram_status`` / ``telegram_disconnect`` — the settings-page
  partial (link state) + its disconnect action, both user-scoped.
"""

from __future__ import annotations

import hmac
import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import TelegramAccount
from .services import link_deep_link, process_update


@csrf_exempt
def telegram_webhook(request, secret):
    """Receive a Telegram update (production path).

    Guards on the secret path segment AND the
    ``X-Telegram-Bot-Api-Secret-Token`` header (both compared
    constant-time). A mismatch 404s — the endpoint's existence stays
    unconfirmed. Malformed bodies are ignored with a 200 so Telegram
    stops resending them.
    """
    expected = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")
    header = request.META.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
    if not expected or not hmac.compare_digest(secret, expected) or not hmac.compare_digest(header, expected):
        raise Http404
    if request.method != "POST":
        raise Http404
    try:
        update = json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=200)
    process_update(update)
    return HttpResponse(status=200)


# Notification kinds offered as per-chat delivery toggles (SYSTEM omitted —
# it isn't user-facing). Order = how they read in the settings list.
def _kind_pref_kinds():
    from apps.notifications.models import Notification

    # ANNOUNCEMENT is intentionally absent: announcements are force-delivered
    # and cannot be muted per-kind.
    K = Notification.Kind
    return [
        K.MENTION,
        K.ASSIGNED,
        K.COMMENT,
        K.STATUS_CHANGE,
        K.PRIORITY_CHANGE,
        K.DUE,
        K.PROJECT_UPDATE,
        K.CYCLE,
    ]


def _settings_context(user):
    """Build the context the Telegram settings partial expects."""
    account = getattr(user, "telegram", None)
    ctx = {
        "telegram_account": account,
        "telegram_link_url": link_deep_link(user),
    }
    if account is not None:
        from apps.notifications.models import Notification

        muted = set(account.muted_kinds or [])
        labels = dict(Notification.Kind.choices)
        # (value, label, is_on) per offered kind — "on" = not muted.
        ctx["telegram_kind_prefs"] = [(k, labels[k], k not in muted) for k in _kind_pref_kinds()]
    return ctx


@login_required
def telegram_status(request):
    """Return the Telegram settings partial — polled while unlinked.

    The connect panel HTMX-polls this; once the webhook has bound the
    chat, the partial swaps to the linked card (which carries no poll
    trigger, so polling stops).
    """
    return render(request, "telegram/_settings.html", _settings_context(request.user))


@require_POST
@login_required
def telegram_disconnect(request):
    """Unlink the user's Telegram chat; return the refreshed partial."""
    TelegramAccount.objects.filter(user=request.user).delete()
    html = render_to_string("telegram/_settings.html", _settings_context(request.user), request=request)
    return HttpResponse(html)


@require_POST
@login_required
def telegram_toggle(request):
    """Flip whether the user's linked chat receives notifications."""
    account = TelegramAccount.objects.filter(user=request.user).first()
    if account is not None:
        account.enabled = not account.enabled
        account.save(update_fields=["enabled"])
    html = render_to_string("telegram/_settings.html", _settings_context(request.user), request=request)
    return HttpResponse(html)


@require_POST
@login_required
def telegram_toggle_kind(request):
    """Mute / unmute one notification kind for the user's chat."""
    from apps.notifications.models import Notification

    kind = request.POST.get("kind", "")
    valid = {value for value, _label in Notification.Kind.choices}
    account = TelegramAccount.objects.filter(user=request.user).first()
    if account is not None and kind in valid:
        muted = list(account.muted_kinds or [])
        if kind in muted:
            muted.remove(kind)
        else:
            muted.append(kind)
        account.muted_kinds = muted
        account.save(update_fields=["muted_kinds"])
    html = render_to_string("telegram/_settings.html", _settings_context(request.user), request=request)
    return HttpResponse(html)
