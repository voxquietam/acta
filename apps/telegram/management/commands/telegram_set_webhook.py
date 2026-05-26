"""Register (or clear) the bot's webhook with Telegram.

With no ``--base-url`` the public URL is taken from ``ACTA_PUBLIC_BASE_URL``,
so ``make deploy`` can run it unattended::

    python manage.py telegram_set_webhook                          # uses ACTA_PUBLIC_BASE_URL
    python manage.py telegram_set_webhook --base-url https://acta.example.com
    python manage.py telegram_set_webhook --delete                 # switch back to polling

The webhook URL is ``<base>/telegram/webhook/<TELEGRAM_WEBHOOK_SECRET>/``
and the same secret is sent as the bot-api secret-token header, so the
view can authenticate Telegram's calls. Requires HTTPS (Telegram refuses
plain-HTTP webhooks).

Deploy-safe: when the bot token or the base URL isn't configured it prints a
notice and exits 0 (rather than failing) so an instance without Telegram still
deploys cleanly. A genuine ``setWebhook`` failure (bad token / non-HTTPS URL)
still raises, so a misconfigured-but-intended setup is surfaced loudly.
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
        # Skip quietly when the integration isn't set up — keeps ``make deploy``
        # green on instances that don't use Telegram.
        if not client.is_configured():
            self.stdout.write(self.style.WARNING("TELEGRAM_BOT_TOKEN not set — skipping webhook."))
            return

        if options["delete"]:
            ok = client.delete_webhook()
            self.stdout.write(self.style.SUCCESS("Webhook deleted." if ok else "Failed to delete webhook."))
            return

        base = options["base_url"] or getattr(settings, "ACTA_PUBLIC_BASE_URL", "")
        if not base:
            self.stdout.write(
                self.style.WARNING("No base URL (pass --base-url or set ACTA_PUBLIC_BASE_URL) — skipping webhook."),
            )
            return

        secret = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")
        if not secret:
            raise CommandError("TELEGRAM_WEBHOOK_SECRET is not set — set it before registering a webhook.")

        url = base.rstrip("/") + reverse("telegram:webhook", args=[secret])
        ok = client.set_webhook(url, secret_token=secret)
        if ok:
            self.stdout.write(self.style.SUCCESS(f"Webhook set: {url}"))
        else:
            raise CommandError("setWebhook failed — check the token, URL, and that it's HTTPS.")
