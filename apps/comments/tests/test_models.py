"""Tests for :mod:`apps.comments.models`."""

import datetime

from django.core.exceptions import ValidationError

import pytest

from apps.comments.models import Comment
from apps.comments.tests.factories import CommentFactory
from apps.tasks.tests.factories import TaskFactory


@pytest.mark.django_db
class TestThreadingInvariant:
    """``Comment.clean()`` enforces depth-1 threading + target consistency.

    The DB ``CheckConstraint`` covers exactly-one-target (task or
    project_update); ``clean()`` adds the depth limit and forces a reply
    to share its parent's target. Both rules are part of the
    polymorphic-comments design (ADR 0022) and Wave 2 C5 §F3 flagged
    them as untested.
    """

    def test_reply_cannot_have_parent(self):
        """Replies are capped at depth 1 — a reply cannot itself spawn one."""
        c1 = CommentFactory()
        c2 = CommentFactory(task=c1.task, parent=c1)
        c3 = Comment(
            task=c1.task,
            author=c1.author,
            body="reply-of-reply",
            parent=c2,
        )
        with pytest.raises(ValidationError, match="depth limit"):
            c3.full_clean()

    def test_reply_must_share_target(self):
        """A reply targeting a different task than its parent is rejected."""
        c1 = CommentFactory()
        other_task = TaskFactory()
        c2 = Comment(
            task=other_task,
            author=c1.author,
            body="cross-target reply",
            parent=c1,
        )
        with pytest.raises(ValidationError, match="same target"):
            c2.full_clean()

    def test_delete_parent_cascades_replies(self):
        """``parent`` FK is CASCADE — deleting C1 removes its C2 reply."""
        c1 = CommentFactory()
        c2 = CommentFactory(task=c1.task, parent=c1)
        c1.delete()
        assert not Comment.objects.filter(pk=c2.pk).exists()


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
