"""Tests for :mod:`apps.comments.models`."""

import datetime

import pytest

from apps.comments.tests.factories import CommentFactory


@pytest.mark.django_db
class TestWasEdited:
    """``Comment.was_edited`` reflects actual user edits, not the
    microsecond drift between ``auto_now_add`` and ``auto_now`` at
    INSERT time."""

    def test_fresh_comment_is_not_edited(self):
        """A just-created comment must not register as edited.

        ``auto_now_add`` / ``auto_now`` make separate ``timezone.now()``
        calls so ``created_at`` and ``updated_at`` differ by a few
        microseconds even when the user didn't touch the comment.
        """
        comment = CommentFactory()
        assert comment.was_edited is False

    def test_edited_comment_returns_true(self):
        comment = CommentFactory()
        comment.body = "edited body"
        comment.save()
        # Force the timestamps far enough apart that the 1-second
        # tolerance can't absorb the difference.
        Comment = comment.__class__
        Comment.objects.filter(pk=comment.pk).update(
            updated_at=comment.created_at + datetime.timedelta(seconds=30),
        )
        comment.refresh_from_db()
        assert comment.was_edited is True

    def test_below_tolerance_returns_false(self):
        """Sub-second drift between created_at and updated_at must not
        flip ``was_edited`` to True — that's exactly the bug we're
        guarding against."""
        comment = CommentFactory()
        Comment = comment.__class__
        Comment.objects.filter(pk=comment.pk).update(
            updated_at=comment.created_at + datetime.timedelta(milliseconds=500),
        )
        comment.refresh_from_db()
        assert comment.was_edited is False
