"""``seed_telegram_templates`` management command."""

from django.core.management import call_command

import pytest

from apps.notifications.models import Notification
from apps.telegram.management.commands.seed_telegram_templates import DEFAULT_TEMPLATES
from apps.telegram.models import TelegramMessageTemplate


@pytest.mark.django_db
class TestSeedTelegramTemplates:
    def test_creates_a_row_per_kind_when_empty(self):
        assert TelegramMessageTemplate.objects.count() == 0
        call_command("seed_telegram_templates")
        assert TelegramMessageTemplate.objects.count() == len(DEFAULT_TEMPLATES)
        assert set(TelegramMessageTemplate.objects.values_list("kind", flat=True)) == set(DEFAULT_TEMPLATES)
        # Announcement is intentionally left to the built-in default.
        assert not TelegramMessageTemplate.objects.filter(kind=Notification.Kind.ANNOUNCEMENT).exists()

    def test_idempotent_and_preserves_edits(self):
        call_command("seed_telegram_templates")
        edited = TelegramMessageTemplate.objects.get(kind=Notification.Kind.ASSIGNED)
        edited.body = "custom wording {actor}"
        edited.save()
        call_command("seed_telegram_templates")  # second run
        assert TelegramMessageTemplate.objects.count() == len(DEFAULT_TEMPLATES)
        edited.refresh_from_db()
        assert edited.body == "custom wording {actor}"  # untouched without --overwrite

    def test_overwrite_resets_to_default(self):
        call_command("seed_telegram_templates")
        row = TelegramMessageTemplate.objects.get(kind=Notification.Kind.ASSIGNED)
        row.body = "stale"
        row.save()
        call_command("seed_telegram_templates", "--overwrite")
        row.refresh_from_db()
        assert row.body == DEFAULT_TEMPLATES[Notification.Kind.ASSIGNED]
