"""Send a Telegram heads-up that a deploy is about to start.

Called by ``make deploy`` ~1 minute before the disruptive ``up --build``
step, on ANY branch, so people get a warning before the brief restart.

Targeting: there is no group chat — bot delivery is per linked user
(``TelegramAccount.chat_id``). By default the heads-up fans out to every
linked account with ``enabled=True`` that hasn't muted the ``SYSTEM`` kind
("System updates" in settings) — i.e. the team that opted into bot
notifications and still wants downtime warnings. Set
``TELEGRAM_DEPLOY_CHAT_ID`` to override and send to a single chat instead
(an ops group, or one person); the override ignores per-user mutes.

Best-effort throughout: a missing bot token or zero reachable chats makes
this a no-op and never aborts the deploy — matching the rest of the
Telegram integration's fail-soft behaviour.
"""

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Ping linked Telegram chats that a deploy is starting (best-effort)."

    def add_arguments(self, parser):
        """Register the optional context flags the Makefile passes."""
        parser.add_argument(
            "--branch",
            default="",
            help="Branch being deployed (shown in the message).",
        )
        parser.add_argument(
            "--commit",
            default="",
            help="Short commit hash being deployed, if known.",
        )
        parser.add_argument(
            "--eta",
            default="~60s",
            help="Human ETA until the restart (default '~60s').",
        )

    def handle(self, *args, **options):
        """Send the heads-up to every reachable chat, or report why skipped.

        Returns without error in every branch so a failed or unconfigured
        send never aborts ``make deploy``.
        """
        from apps.telegram import client
        from apps.telegram.models import TelegramAccount

        if not client.is_configured():
            self.stdout.write("notify_deploy: no bot token — skipped")
            return

        override = getattr(settings, "TELEGRAM_DEPLOY_CHAT_ID", "")
        if override:
            chat_ids = [int(override)]
        else:
            from apps.notifications.models import Notification

            # Honour the per-chat "System updates" mute: these heads-ups bypass
            # the notification pipeline, so the opt-out is read straight off
            # ``muted_kinds`` here. The explicit override above is an ops target
            # and intentionally ignores user prefs.
            chat_ids = list(
                TelegramAccount.objects.filter(enabled=True)
                .exclude(muted_kinds__contains=[Notification.Kind.SYSTEM])
                .values_list("chat_id", flat=True)
            )
        if not chat_ids:
            self.stdout.write("notify_deploy: no reachable chats — skipped")
            return

        text = self._build_message(options)
        sent = 0
        for chat_id in chat_ids:
            if client.send_message(chat_id, text):
                sent += 1
        self.stdout.write(f"notify_deploy: sent to {sent}/{len(chat_ids)} chat(s)")

    def _build_message(self, options) -> str:
        """Compose the HTML heads-up body from the deploy context flags.

        No timestamp line — Telegram already stamps the delivery time, and an
        absolute server-clock value (UTC) only confused readers in other
        zones. The actionable bit is the relative ETA.
        """
        branch = options["branch"] or "?"
        commit = options["commit"]
        eta = options["eta"]
        target = f"<code>{branch}</code>" + (f" @ <code>{commit}</code>" if commit else "")
        return (
            "⚠️ <b>SYSTEM UPDATE INCOMING</b> ⚠️\n"
            f"Acta restarts in <b>{eta}</b> — brief downtime, save your work.\n"
            f"Branch: {target}"
        )
