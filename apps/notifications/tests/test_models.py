"""Notification model behavior — read/unread state transitions."""

import pytest

from apps.notifications.models import Notification

from .factories import NotificationFactory


@pytest.mark.django_db
class TestReadState:
    """``mark_read`` / ``mark_unread`` flip ``is_read`` + ``read_at``."""

    def test_mark_read_stamps_read_at(self):
        n = NotificationFactory(is_read=False, read_at=None)
        n.mark_read()
        n.refresh_from_db()
        assert n.is_read is True
        assert n.read_at is not None

    def test_mark_read_is_idempotent(self):
        n = NotificationFactory(is_read=False)
        n.mark_read()
        first = n.read_at
        n.mark_read()
        n.refresh_from_db()
        assert n.read_at == first

    def test_mark_unread_clears_read_at(self):
        n = NotificationFactory(is_read=False)
        n.mark_read()
        n.mark_unread()
        n.refresh_from_db()
        assert n.is_read is False
        assert n.read_at is None

    def test_default_kinds_include_mention(self):
        """The ``mention`` kind exists so the mentions phase needs no migration."""
        assert "mention" in dict(Notification.Kind.choices)
