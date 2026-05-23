"""Long-poll Telegram updates — the dev / no-public-URL fallback.

Production uses a webhook (``telegram_set_webhook``); locally there's no
public URL for Telegram to reach, so run this instead to process the
``/start`` link command::

    python manage.py telegram_poll

Loops on ``getUpdates`` and routes each update through the same
:func:`apps.telegram.services.process_update` the webhook uses. Ctrl-C to
stop. Deletes any existing webhook first (the two are mutually exclusive).
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from apps.telegram import client
from apps.telegram.services import process_update


class Command(BaseCommand):
    help = "Poll Telegram for updates and process them (dev fallback for the webhook)"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one batch and exit (for scripted checks)")

    def handle(self, *args, **options):
        if not client.is_configured():
            raise CommandError("TELEGRAM_BOT_TOKEN is not set.")
        client.delete_webhook()  # webhook + getUpdates are mutually exclusive
        self.stdout.write("Polling Telegram (Ctrl-C to stop)…")
        offset = None
        try:
            while True:
                updates = client.get_updates(offset=offset)
                for update in updates:
                    process_update(update)
                    offset = update["update_id"] + 1
                if options["once"]:
                    break
                if not updates:
                    time.sleep(1)
        except KeyboardInterrupt:
            self.stdout.write("\nStopped.")
