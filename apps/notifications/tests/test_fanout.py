"""Fan-out of task / comment events to per-user notifications.

Exercises the real path: ``snapshot_task`` + ``emit_task_diff_events``
for task edits, and ``notify_comment_created`` for comments. Asserts
recipient resolution + the self-actor suppression rule from ADR 0021.
"""

import pytest

from apps.comments.models import Comment
from apps.notifications.models import Notification
from apps.notifications.services import notify_comment_created
from apps.projects.tests.factories import ProjectFactory
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

    def test_assignment_notifies_only_new_assignee(self, trio):
        project, assignee, reporter, actor = trio
        task = TaskFactory(project=project, assignee=None, reporter=reporter, status=Task.STATUS_TODO)
        _reassign(task, assignee, actor)
        rows = Notification.objects.filter(kind=Notification.Kind.ASSIGNED)
        assert list(rows.values_list("recipient_id", flat=True)) == [assignee.id]

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
