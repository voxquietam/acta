"""Fan-out of task / comment events to per-user notifications.

Exercises the real path: ``snapshot_task`` + ``emit_task_diff_events``
for task edits, and ``notify_comment_created`` for comments. Asserts
recipient resolution + the self-actor suppression rule from ADR 0021.
"""

import datetime

from django.utils import timezone

import pytest

from apps.comments.models import Comment
from apps.notifications.models import Notification
from apps.notifications.services import notify_announcement, notify_comment_created, notify_project_update_created
from apps.projects.tests.factories import ProjectFactory, ProjectUpdateFactory
from apps.tasks.events import emit_task_diff_events, snapshot_task
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def trio(db):
    """Workspace + project + three members (assignee, reporter, actor)."""
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    assignee = ws.owner
    reporter = WorkspaceMemberFactory(workspace=ws).user
    actor = WorkspaceMemberFactory(workspace=ws).user
    return project, assignee, reporter, actor


def _change_status(task, new_status, actor):
    """Mutate status the way the inline-edit endpoints do."""
    old = snapshot_task(task)
    task.status = new_status
    task.save()
    emit_task_diff_events(old_state=old, task=task, actor=actor)


def _reassign(task, new_assignee, actor):
    """Mutate assignee the way the inline-edit endpoints do."""
    old = snapshot_task(task)
    task.assignee = new_assignee
    task.save()
    emit_task_diff_events(old_state=old, task=task, actor=actor)


@pytest.mark.django_db
class TestTaskDiffFanout:
    def test_status_change_notifies_assignee_and_reporter(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter, status=Task.STATUS_TODO)
        _change_status(task, Task.STATUS_IN_REVIEW, actor)
        recipients = set(
            Notification.objects.filter(kind=Notification.Kind.STATUS_CHANGE).values_list("recipient_id", flat=True)
        )
        assert recipients == {assignee.id, reporter.id}

    def test_priority_change_notifies_assignee_and_reporter(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter, priority=3)
        old = snapshot_task(task)
        task.priority = 1
        task.save()
        emit_task_diff_events(old_state=old, task=task, actor=actor)
        assert Notification.objects.filter(kind=Notification.Kind.PRIORITY_CHANGE).count() == 2

    def test_actor_never_notified_about_own_change(self, trio):
        project, assignee, reporter, actor = trio
        # actor is also the assignee here → must not self-notify
        task = TaskFactory(project=project, assignee=actor, reporter=reporter, status=Task.STATUS_TODO)
        _change_status(task, Task.STATUS_DONE, actor)
        assert not Notification.objects.filter(recipient=actor).exists()
        assert Notification.objects.filter(recipient=reporter, kind=Notification.Kind.STATUS_CHANGE).exists()

    def test_assignment_notifies_new_assignee(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=None, reporter=reporter, status=Task.STATUS_TODO)
        _reassign(task, assignee, actor)
        rows = Notification.objects.filter(kind=Notification.Kind.ASSIGNED)
        assert list(rows.values_list("recipient_id", flat=True)) == [assignee.id]

    def test_assignment_preview_is_task_description(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(
            project=project, assignee=None, reporter=reporter, description="ship the login page by friday"
        )
        _reassign(task, assignee, actor)
        row = Notification.objects.get(kind=Notification.Kind.ASSIGNED, recipient=assignee)
        assert row.preview == "ship the login page by friday"

    def test_reassignment_notifies_old_and_new_assignee(self, trio):
        project, old_assignee, reporter, actor = trio
        new_assignee = WorkspaceMemberFactory(workspace=project.workspace).user
        task = TaskFactory(project=project, assignee=old_assignee, reporter=reporter, status=Task.STATUS_TODO)
        _reassign(task, new_assignee, actor)
        recipients = set(
            Notification.objects.filter(kind=Notification.Kind.ASSIGNED).values_list("recipient_id", flat=True)
        )
        assert recipients == {old_assignee.id, new_assignee.id}

    def test_due_change_notifies_assignee_and_reporter(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter)
        old = snapshot_task(task)
        task.due_date = timezone.localdate() + datetime.timedelta(days=3)
        task.save()
        emit_task_diff_events(old_state=old, task=task, actor=actor)
        recipients = set(Notification.objects.filter(kind=Notification.Kind.DUE).values_list("recipient_id", flat=True))
        assert recipients == {assignee.id, reporter.id}

    def test_labels_change_does_not_notify(self, trio):
        project, assignee, reporter, actor = trio
        from apps.labels.tests.factories import LabelFactory

        label = LabelFactory(workspace=project.workspace)
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter)
        old = snapshot_task(task)
        task.labels.add(label)
        emit_task_diff_events(old_state=old, task=task, actor=actor)
        assert Notification.objects.count() == 0


@pytest.mark.django_db
class TestAssigneeResolutionMatrix:
    """All four combinations of (status changed, assignee changed) emit the
    right notification kinds to the right recipients. Wave 2 C7 §F8 noted
    that existing tests cover the status-only and reassignment cases but
    not the full 2×2 matrix.
    """

    def test_status_only(self, trio):
        """Status flips alone notifies current assignee + reporter; no ASSIGNED."""
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter, status=Task.STATUS_TODO)
        old = snapshot_task(task)
        task.status = Task.STATUS_IN_REVIEW
        task.save()
        emit_task_diff_events(old_state=old, task=task, actor=actor)
        status_recipients = set(
            Notification.objects.filter(kind=Notification.Kind.STATUS_CHANGE).values_list("recipient_id", flat=True),
        )
        assert status_recipients == {assignee.id, reporter.id}
        assert not Notification.objects.filter(kind=Notification.Kind.ASSIGNED).exists()

    def test_unassign_only(self, trio):
        """Unassign (assignee → None) without status change emits ASSIGNED to former."""
        project, old_assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=old_assignee, reporter=reporter, status=Task.STATUS_TODO)
        old = snapshot_task(task)
        task.assignee = None
        task.save()
        emit_task_diff_events(old_state=old, task=task, actor=actor)
        assigned_recipients = set(
            Notification.objects.filter(kind=Notification.Kind.ASSIGNED).values_list("recipient_id", flat=True),
        )
        assert assigned_recipients == {old_assignee.id}
        assert not Notification.objects.filter(kind=Notification.Kind.STATUS_CHANGE).exists()

    def test_both_status_and_assignee(self, trio):
        """Two diffs in one save emit both kinds with their respective recipients.

        STATUS_CHANGE reads current assignee + reporter (the new owners
        get the news); ASSIGNED reaches the old assignee (their plate
        just cleared) and the new one (they just got the task). The
        sets intentionally overlap on the new assignee.
        """
        project, old_assignee, reporter, actor = trio
        new_assignee = WorkspaceMemberFactory(workspace=project.workspace).user
        task = TaskFactory(project=project, assignee=old_assignee, reporter=reporter, status=Task.STATUS_TODO)
        old = snapshot_task(task)
        task.status = Task.STATUS_IN_PROGRESS
        task.assignee = new_assignee
        task.save()
        emit_task_diff_events(old_state=old, task=task, actor=actor)
        status_recipients = set(
            Notification.objects.filter(kind=Notification.Kind.STATUS_CHANGE).values_list("recipient_id", flat=True),
        )
        assigned_recipients = set(
            Notification.objects.filter(kind=Notification.Kind.ASSIGNED).values_list("recipient_id", flat=True),
        )
        assert status_recipients == {new_assignee.id, reporter.id}
        assert assigned_recipients == {old_assignee.id, new_assignee.id}

    def test_neither(self, trio):
        """A save with no watched-field diff fans out no notifications."""
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter, status=Task.STATUS_TODO)
        old = snapshot_task(task)
        task.title = "renamed — not a watched diff field"
        task.save()
        emit_task_diff_events(old_state=old, task=task, actor=actor)
        assert Notification.objects.count() == 0


@pytest.mark.django_db
class TestCommentFanout:
    def test_comment_notifies_assignee_and_reporter(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter)
        comment = Comment.objects.create(task=task, author=actor, body="heads up")
        notify_comment_created(comment=comment, actor=actor)
        recipients = set(Notification.objects.values_list("recipient_id", flat=True))
        assert recipients == {assignee.id, reporter.id}

    def test_comment_author_not_self_notified(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=actor, reporter=reporter)
        comment = Comment.objects.create(task=task, author=actor, body="mine")
        notify_comment_created(comment=comment, actor=actor)
        assert not Notification.objects.filter(recipient=actor).exists()

    def test_comment_preview_is_stored(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter)
        comment = Comment.objects.create(task=task, author=actor, body="store this body")
        notify_comment_created(comment=comment, actor=actor)
        n = Notification.objects.filter(recipient=assignee).first()
        assert n.preview == "store this body"
        assert n.comment_id == comment.id

    def test_long_comment_preview_is_truncated_with_ellipsis(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter)
        comment = Comment.objects.create(task=task, author=actor, body="x" * 500)
        notify_comment_created(comment=comment, actor=actor)
        preview = Notification.objects.filter(recipient=assignee).first().preview
        assert preview.endswith("…")
        assert len(preview) == 281  # 280 chars + the ellipsis

    def test_reply_notifies_parent_author(self, trio):
        project, assignee, reporter, actor = trio
        # Parent comment authored by a member who is neither assignee nor
        # reporter, so the only reason they'd be notified is the reply.
        parent_author = WorkspaceMemberFactory(workspace=project.workspace).user
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter)
        parent = Comment.objects.create(task=task, author=parent_author, body="top")
        reply = Comment.objects.create(task=task, author=actor, parent=parent, body="re")
        notify_comment_created(comment=reply, actor=actor)
        recipients = set(Notification.objects.values_list("recipient_id", flat=True))
        assert parent_author.id in recipients
        assert recipients == {assignee.id, reporter.id, parent_author.id}


@pytest.mark.django_db
class TestMentionFanout:
    def test_comment_mention_notifies_member(self, trio):
        project, assignee, reporter, actor = trio
        mentioned = WorkspaceMemberFactory(workspace=project.workspace).user
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter)
        comment = Comment.objects.create(
            task=task, author=actor, body=f"hey [@{mentioned.username}](mention:{mentioned.id}) look"
        )
        notify_comment_created(comment=comment, actor=actor)
        assert Notification.objects.filter(recipient=mentioned, kind=Notification.Kind.MENTION).exists()

    def test_mentioned_assignee_gets_mention_not_comment(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter)
        comment = Comment.objects.create(
            task=task, author=actor, body=f"ping [@{assignee.username}](mention:{assignee.id})"
        )
        notify_comment_created(comment=comment, actor=actor)
        kinds = set(Notification.objects.filter(recipient=assignee).values_list("kind", flat=True))
        assert Notification.Kind.MENTION in kinds
        assert Notification.Kind.COMMENT not in kinds

    def test_mention_of_non_member_ignored(self, trio):
        project, assignee, reporter, actor = trio
        outsider = WorkspaceFactory().owner
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter)
        comment = Comment.objects.create(task=task, author=actor, body=f"[@x](mention:{outsider.id})")
        notify_comment_created(comment=comment, actor=actor)
        assert not Notification.objects.filter(recipient=outsider).exists()

    def test_mention_of_actor_suppressed(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter)
        comment = Comment.objects.create(task=task, author=actor, body=f"note [@me](mention:{actor.id})")
        notify_comment_created(comment=comment, actor=actor)
        assert not Notification.objects.filter(recipient=actor).exists()

    def test_description_mention_notifies_once(self, trio):
        project, assignee, reporter, actor = trio
        mentioned = WorkspaceMemberFactory(workspace=project.workspace).user
        task = TaskFactory(project=project, assignee=assignee, reporter=reporter, description="")
        old = snapshot_task(task)
        task.description = f"see [@{mentioned.username}](mention:{mentioned.id})"
        task.save()
        emit_task_diff_events(old_state=old, task=task, actor=actor)
        assert Notification.objects.filter(recipient=mentioned, kind=Notification.Kind.MENTION).count() == 1
        # editing the description again without dropping the mention must not re-ping
        old2 = snapshot_task(task)
        task.description = f"see [@{mentioned.username}](mention:{mentioned.id}) more"
        task.save()
        emit_task_diff_events(old_state=old2, task=task, actor=actor)
        assert Notification.objects.filter(recipient=mentioned, kind=Notification.Kind.MENTION).count() == 1


@pytest.mark.django_db
class TestProjectUpdateFanout:
    def test_notifies_other_members_not_author(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        author = ws.owner
        member = WorkspaceMemberFactory(workspace=ws).user
        update = ProjectUpdateFactory(project=project, author=author, body="weekly recap")
        notify_project_update_created(update=update, actor=author)
        notifs = Notification.objects.filter(kind=Notification.Kind.PROJECT_UPDATE)
        recipients = set(notifs.values_list("recipient_id", flat=True))
        assert member.id in recipients
        assert author.id not in recipients  # self-suppressed
        row = notifs.get(recipient=member)
        assert row.project_update_id == update.id
        assert "weekly recap" in row.preview

    def test_does_not_notify_foreign_workspace(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        outsider = WorkspaceFactory().owner
        update = ProjectUpdateFactory(project=project, author=ws.owner, body="x")
        notify_project_update_created(update=update, actor=ws.owner)
        assert not Notification.objects.filter(recipient=outsider).exists()


@pytest.mark.django_db
class TestAnnouncementFanout:
    def test_broadcasts_to_all_including_sender(self):
        ws = WorkspaceFactory()
        sender = ws.owner
        member = WorkspaceMemberFactory(workspace=ws).user
        count = notify_announcement(workspace_id=ws.id, actor=sender, title="Heads up", body="server maintenance sat")
        notifs = Notification.objects.filter(kind=Notification.Kind.ANNOUNCEMENT)
        recipients = set(notifs.values_list("recipient_id", flat=True))
        assert recipients == {member.id, sender.id}  # the sender keeps a copy too
        assert count == 2
        # the sender's own copy is pre-read; the member's is a fresh alert
        assert notifs.get(recipient=sender).is_read is True
        assert notifs.get(recipient=member).is_read is False
        assert notifs.get(recipient=member).payload["title"] == "Heads up"
        assert "server maintenance sat" in notifs.get(recipient=member).preview

    def test_does_not_reach_other_workspaces(self):
        ws = WorkspaceFactory()
        outsider = WorkspaceFactory().owner
        notify_announcement(workspace_id=ws.id, actor=ws.owner, title="t", body="b")
        assert not Notification.objects.filter(recipient=outsider).exists()

    def test_project_update_not_counted_as_unread(self):
        """The unread count (sidebar badge + live SSE badge) skips project
        updates — they surface in the Updates tab, not Notifications."""
        from apps.notifications.services import _unread_count

        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        member = WorkspaceMemberFactory(workspace=ws).user
        update = ProjectUpdateFactory(project=project, author=ws.owner, body="recap")
        notify_project_update_created(update=update, actor=ws.owner)
        # The member has exactly one (unread) PROJECT_UPDATE notification…
        assert Notification.objects.filter(recipient=member, is_read=False).count() == 1
        # …but it must not count toward the unread badge.
        assert _unread_count(member.id) == 0


@pytest.mark.django_db
class TestNotifyTaskCreated:
    """Creating a task already assigned notifies the assignee (ADR 0021 gap)."""

    def test_assignee_notified_on_create(self, trio):
        from apps.notifications.services import notify_task_created

        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=assignee, reporter=actor)
        notify_task_created(task=task, actor=actor)
        n = Notification.objects.get(recipient=assignee, kind=Notification.Kind.ASSIGNED)
        assert n.task_id == task.id
        assert n.actor_id == actor.id

    def test_self_assign_on_create_is_suppressed(self, trio):
        from apps.notifications.services import notify_task_created

        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=actor, reporter=actor)
        notify_task_created(task=task, actor=actor)
        assert not Notification.objects.filter(kind=Notification.Kind.ASSIGNED).exists()

    def test_unassigned_create_notifies_no_one(self, trio):
        from apps.notifications.services import notify_task_created

        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=None, reporter=actor)
        notify_task_created(task=task, actor=actor)
        assert not Notification.objects.filter(kind=Notification.Kind.ASSIGNED).exists()
