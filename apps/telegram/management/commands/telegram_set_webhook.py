"""Register (or clear) the bot's webhook with Telegram.

Run once per deploy after the public URL is known::

    python manage.py telegram_set_webhook --base-url https://acta.example.com
    python manage.py telegram_set_webhook --delete   # switch back to polling

The webhook URL is ``<base>/telegram/webhook/<TELEGRAM_WEBHOOK_SECRET>/``
and the same secret is sent as the bot-api secret-token header, so the
view can authenticate Telegram's calls. Requires HTTPS (Telegram refuses
plain-HTTP webhooks).
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from apps.telegram import client


class Command(BaseCommand):
    help = "Register or delete the Telegram webhook for this deployment"

    def add_arguments(self, parser):
        parser.add_argument("--base-url", type=str, default=None, help="Public base URL, e.g. https://acta.example.com")
        parser.add_argument("--delete", action="store_true", help="Remove the webhook (use polling instead)")

    def handle(self, *args, **options):
        if not client.is_configured():
            raise CommandError("TELEGRAM_BOT_TOKEN is not set.")
        if options["delete"]:
            ok = client.delete_webhook()
            self.stdout.write(self.style.SUCCESS("Webhook deleted." if ok else "Failed to delete webhook."))
            return
        base = options["base_url"]
        if not base:
            raise CommandError("Provide --base-url (or --delete).")
        secret = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")
        if not secret:
            raise CommandError("TELEGRAM_WEBHOOK_SECRET is not set — set it before registering a webhook.")
        url = base.rstrip("/") + reverse("telegram:webhook", args=[secret])
        ok = client.set_webhook(url, secret_token=secret)
        if ok:
            self.stdout.write(self.style.SUCCESS(f"Webhook set: {url}"))
        else:
            raise CommandError("setWebhook failed — check the token, URL, and that it's HTTPS.")
