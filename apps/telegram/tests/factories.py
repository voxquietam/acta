"""Factories for :mod:`apps.telegram` test data.

Wave 2 C7 §F9 flagged the absence of these. The existing test files
build :class:`TelegramAccount` / :class:`TelegramMessageTemplate` rows
via inline ``objects.create()`` calls, which is fine for a handful of
tests but friction for future suites — perf benchmarks (high-volume
fanout) and admin-form coverage both want a one-liner.
"""

import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import Notification
from apps.telegram.models import TelegramAccount, TelegramLinkToken, TelegramMessageTemplate


class TelegramAccountFactory(DjangoModelFactory):
    """Build a linked :class:`TelegramAccount`.

    Defaults to an ``enabled`` account with no muted kinds and a unique
    ``chat_id``. Override ``user=``, ``muted_kinds=``, or ``enabled=``
    when the test needs a specific state.
    """

    class Meta:
        model = TelegramAccount

    user = factory.SubFactory(UserFactory)
    chat_id = factory.Sequence(lambda n: 10_000 + n)
    username = factory.Sequence(lambda n: f"tg_user_{n}")
    enabled = True
    muted_kinds = factory.LazyFunction(list)


class TelegramMessageTemplateFactory(DjangoModelFactory):
    """Build a :class:`TelegramMessageTemplate` row.

    Defaults to a known-good body using documented ``{placeholders}``
    so :meth:`TelegramMessageTemplate.clean` passes; override ``body=``
    to exercise validator failure paths.
    """

    class Meta:
        model = TelegramMessageTemplate

    kind = Notification.Kind.ASSIGNED
    body = "{actor} → {slug}: {title}"


class TelegramLinkTokenFactory(DjangoModelFactory):
    """Build a one-shot :class:`TelegramLinkToken` for the deep-link flow."""

    class Meta:
        model = TelegramLinkToken

    user = factory.SubFactory(UserFactory)
    token = factory.Sequence(lambda n: f"tok_{n:022d}")
