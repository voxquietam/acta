"""Tests for the MCP write tools.

``acta_task_create``, ``acta_task_update``, ``acta_task_archive``,
``acta_comment_create``. Each tool routes writes through the same
``TaskSerializer`` / Django models the web UI uses so validation
gates (workspace membership, labels-in-workspace, assignee-must-be-
member, subtask-depth-1) come for free.
"""

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.comments.models import Comment
from apps.labels.tests.factories import LabelFactory
from apps.mcp.tools import CALLABLES
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def project_setup():
    user = UserFactory()
    ws = WorkspaceFactory()
    WorkspaceMember.objects.create(user=user, workspace=ws)
    project = ProjectFactory(workspace=ws, slug_prefix="ACTA")
    return user, ws, project


@pytest.mark.django_db
class TestTaskCreate:
    def test_minimum_fields_create_task(self, project_setup):
        user, _, project = project_setup
        result = CALLABLES["acta_task_create"](user, {"project": "ACTA", "title": "New task"})
        assert result["title"] == "New task"
        assert result["status"] == Task.STATUS_TODO  # default
        assert result["project_slug_prefix"] == "ACTA"
        # The slug is generated as PREFIX-N; we don't pin the number
        # because factory tasks in other tests may shift the counter.
        assert result["slug"].startswith("ACTA-")
        # Persisted to DB.
        assert Task.objects.filter(project=project, title="New task").exists()

    def test_optional_fields_pass_through(self, project_setup):
        user, _, _ = project_setup
        result = CALLABLES["acta_task_create"](
            user,
            {
                "project": "ACTA",
                "title": "Detailed",
                "description": "Body text",
                "status": Task.STATUS_IN_PROGRESS,
                "priority": Task.URGENT,
                "size": 5,
                "due_date": "2026-12-31",
                "assignee_username": user.username,
            },
        )
        assert result["status"] == Task.STATUS_IN_PROGRESS
        assert result["priority"] == Task.URGENT
        assert result["size"] == 5
        assert result["due_date"] == "2026-12-31"
        assert result["assignee_username"] == user.username

    def test_labels_attached_when_in_workspace(self, project_setup):
        user, ws, _ = project_setup
        LabelFactory(workspace=ws, name="backend")
        LabelFactory(workspace=ws, name="bug")
        result = CALLABLES["acta_task_create"](
            user,
            {"project": "ACTA", "title": "t", "label_names": ["backend", "bug"]},
        )
        names = {lab["name"] for lab in result["labels"]}
        assert names == {"backend", "bug"}

    def test_label_outside_workspace_rejected(self, project_setup):
        user, _, _ = project_setup
        other_ws = WorkspaceFactory()
        LabelFactory(workspace=other_ws, name="other-ws-label")
        with pytest.raises(ValueError, match="Labels not found"):
            CALLABLES["acta_task_create"](
                user,
                {"project": "ACTA", "title": "t", "label_names": ["other-ws-label"]},
            )

    def test_non_member_project_rejected(self, project_setup):
        user, _, _ = project_setup
        intruder = UserFactory()  # not in any workspace
        with pytest.raises(ValueError, match="not found or not accessible"):
            CALLABLES["acta_task_create"](intruder, {"project": "ACTA", "title": "t"})

    def test_assignee_not_in_workspace_rejected(self, project_setup):
        user, _, _ = project_setup
        outsider = UserFactory()
        with pytest.raises(ValueError, match="(workspace|not a member|validation)"):
            CALLABLES["acta_task_create"](
                user,
                {"project": "ACTA", "title": "t", "assignee_username": outsider.username},
            )

    def test_subtask_depth_limit_enforced(self, project_setup):
        user, _, project = project_setup
        parent = TaskFactory(project=project, reporter=user)
        sub = TaskFactory(project=project, reporter=user, parent=parent)
        with pytest.raises(ValueError, match="validation"):
            CALLABLES["acta_task_create"](
                user,
                {"project": "ACTA", "title": "subsub", "parent_slug": sub.slug},
            )

    def test_missing_title_rejected(self, project_setup):
        user, _, _ = project_setup
        with pytest.raises(ValueError, match="required"):
            CALLABLES["acta_task_create"](user, {"project": "ACTA"})


@pytest.mark.django_db
class TestTaskUpdate:
    def test_partial_update_changes_only_passed_fields(self, project_setup):
        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user, title="old", status=Task.STATUS_TODO, priority=Task.MEDIUM)
        result = CALLABLES["acta_task_update"](user, {"slug": task.slug, "status": Task.STATUS_DONE})
        assert result["status"] == Task.STATUS_DONE
        # Title untouched.
        assert result["title"] == "old"
        task.refresh_from_db()
        assert task.status == Task.STATUS_DONE
        assert task.priority == Task.MEDIUM

    def test_clear_assignee_with_null(self, project_setup):
        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user, assignee=user)
        result = CALLABLES["acta_task_update"](user, {"slug": task.slug, "assignee_username": None})
        assert result["assignee_username"] is None

    def test_replace_labels(self, project_setup):
        user, ws, project = project_setup
        a = LabelFactory(workspace=ws, name="a")
        b = LabelFactory(workspace=ws, name="b")
        LabelFactory(workspace=ws, name="c")
        task = TaskFactory(project=project, reporter=user)
        task.labels.add(a, b)

        result = CALLABLES["acta_task_update"](user, {"slug": task.slug, "label_names": ["c"]})
        assert [lab["name"] for lab in result["labels"]] == ["c"]

    def test_other_users_task_raises(self, project_setup):
        user, _, project = project_setup
        intruder = UserFactory()
        task = TaskFactory(project=project, reporter=user)
        with pytest.raises(ValueError, match="not found or not accessible"):
            CALLABLES["acta_task_update"](intruder, {"slug": task.slug, "title": "hacked"})


@pytest.mark.django_db
class TestTaskArchive:
    def test_archive_sets_archived_at(self, project_setup):
        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user)
        assert task.archived_at is None
        result = CALLABLES["acta_task_archive"](user, {"slug": task.slug})
        task.refresh_from_db()
        assert task.archived_at is not None
        assert result["slug"] == task.slug

    def test_archive_is_idempotent(self, project_setup):
        from django.utils import timezone

        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user)
        task.archived_at = timezone.now()
        task.save(update_fields=["archived_at"])
        original = task.archived_at
        CALLABLES["acta_task_archive"](user, {"slug": task.slug})
        task.refresh_from_db()
        # Already archived — timestamp doesn't get re-set.
        assert task.archived_at == original


@pytest.mark.django_db
class TestCommentCreate:
    def test_creates_comment_under_calling_user(self, project_setup):
        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user)
        result = CALLABLES["acta_comment_create"](user, {"task": task.slug, "body": "Looks good"})
        assert result["body"] == "Looks good"
        assert result["author_username"] == user.username
        assert Comment.objects.filter(task=task, body="Looks good").exists()

    def test_empty_body_rejected(self, project_setup):
        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user)
        with pytest.raises(ValueError, match="required"):
            CALLABLES["acta_comment_create"](user, {"task": task.slug, "body": "   "})

    def test_non_member_cant_comment(self, project_setup):
        user, _, project = project_setup
        intruder = UserFactory()
        task = TaskFactory(project=project, reporter=user)
        with pytest.raises(ValueError, match="not found or not accessible"):
            CALLABLES["acta_comment_create"](intruder, {"task": task.slug, "body": "evil"})


@pytest.mark.django_db
class TestBulkCreate:
    def test_creates_all_in_one_transaction(self, project_setup):
        user, _, project = project_setup
        result = CALLABLES["acta_tasks_bulk_create"](
            user,
            {
                "tasks": [
                    {"project": "ACTA", "title": "first"},
                    {"project": "ACTA", "title": "second"},
                    {"project": "ACTA", "title": "third"},
                ],
            },
        )
        assert result["count"] == 3
        titles = {row["title"] for row in result["created"]}
        assert titles == {"first", "second", "third"}
        assert Task.objects.filter(project=project, title__in=titles).count() == 3

    def test_rollback_on_validation_failure(self, project_setup):
        user, _, project = project_setup
        outsider = UserFactory()
        before = Task.objects.filter(project=project).count()
        # Second task has an invalid assignee — whole batch should roll back.
        with pytest.raises(ValueError, match="index 1"):
            CALLABLES["acta_tasks_bulk_create"](
                user,
                {
                    "tasks": [
                        {"project": "ACTA", "title": "ok"},
                        {"project": "ACTA", "title": "bad", "assignee_username": outsider.username},
                    ],
                },
            )
        after = Task.objects.filter(project=project).count()
        assert after == before  # nothing persisted

    def test_empty_list_rejected(self, project_setup):
        user, _, _ = project_setup
        with pytest.raises(ValueError, match="non-empty list"):
            CALLABLES["acta_tasks_bulk_create"](user, {"tasks": []})


@pytest.mark.django_db
class TestBulkUpdate:
    def test_updates_all_in_one_transaction(self, project_setup):
        user, _, project = project_setup
        t1 = TaskFactory(project=project, reporter=user, status=Task.STATUS_TODO)
        t2 = TaskFactory(project=project, reporter=user, status=Task.STATUS_TODO)
        result = CALLABLES["acta_tasks_bulk_update"](
            user,
            {
                "updates": [
                    {"slug": t1.slug, "status": Task.STATUS_IN_PROGRESS},
                    {"slug": t2.slug, "status": Task.STATUS_DONE},
                ],
            },
        )
        assert result["count"] == 2
        t1.refresh_from_db()
        t2.refresh_from_db()
        assert t1.status == Task.STATUS_IN_PROGRESS
        assert t2.status == Task.STATUS_DONE

    def test_rollback_on_failure(self, project_setup):
        user, _, project = project_setup
        t1 = TaskFactory(project=project, reporter=user, status=Task.STATUS_TODO)
        with pytest.raises(ValueError, match="not found or not accessible"):
            CALLABLES["acta_tasks_bulk_update"](
                user,
                {
                    "updates": [
                        {"slug": t1.slug, "status": Task.STATUS_IN_PROGRESS},
                        {"slug": "DOES-9999", "status": Task.STATUS_DONE},
                    ],
                },
            )
        t1.refresh_from_db()
        # First update was rolled back too.
        assert t1.status == Task.STATUS_TODO


@pytest.mark.django_db
class TestBulkArchive:
    def test_archives_all(self, project_setup):
        user, _, project = project_setup
        t1 = TaskFactory(project=project, reporter=user)
        t2 = TaskFactory(project=project, reporter=user)
        result = CALLABLES["acta_tasks_bulk_archive"](user, {"slugs": [t1.slug, t2.slug]})
        assert result["count"] == 2
        t1.refresh_from_db()
        t2.refresh_from_db()
        assert t1.archived_at is not None
        assert t2.archived_at is not None

    def test_already_archived_unchanged_in_batch(self, project_setup):
        from django.utils import timezone

        user, _, project = project_setup
        t = TaskFactory(project=project, reporter=user)
        t.archived_at = timezone.now()
        t.save(update_fields=["archived_at"])
        original = t.archived_at
        # Should not raise and not touch the existing timestamp.
        CALLABLES["acta_tasks_bulk_archive"](user, {"slugs": [t.slug]})
        t.refresh_from_db()
        assert t.archived_at == original


@pytest.mark.django_db
class TestTaskDelete:
    def test_delete_drops_row_and_emits_event(self, project_setup):
        from apps.activity.models import ActivityLog

        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user, title="goner")
        task_id = task.id
        result = CALLABLES["acta_task_delete"](user, {"slug": task.slug})
        assert not Task.objects.filter(pk=task_id).exists()
        assert result["snapshot"]["title"] == "goner"
        event = ActivityLog.objects.filter(event_type="task.deleted", target_id=task_id).first()
        assert event is not None
        assert event.actor == user
        assert event.payload["snapshot"]["title"] == "goner"

    def test_other_users_task_raises(self, project_setup):
        user, _, project = project_setup
        intruder = UserFactory()
        task = TaskFactory(project=project, reporter=user)
        with pytest.raises(ValueError, match="not found or not accessible"):
            CALLABLES["acta_task_delete"](intruder, {"slug": task.slug})


@pytest.mark.django_db
class TestBulkDelete:
    def test_deletes_all_in_one_transaction(self, project_setup):
        user, _, project = project_setup
        t1 = TaskFactory(project=project, reporter=user)
        t2 = TaskFactory(project=project, reporter=user)
        result = CALLABLES["acta_tasks_bulk_delete"](user, {"slugs": [t1.slug, t2.slug]})
        assert result["count"] == 2
        assert not Task.objects.filter(pk__in=[t1.pk, t2.pk]).exists()

    def test_rollback_on_failure(self, project_setup):
        user, _, project = project_setup
        t1 = TaskFactory(project=project, reporter=user)
        with pytest.raises(ValueError, match="not found or not accessible"):
            CALLABLES["acta_tasks_bulk_delete"](user, {"slugs": [t1.slug, "DOES-9999"]})
        # First slug shouldn't have been deleted — atomic rollback.
        assert Task.objects.filter(pk=t1.pk).exists()


@pytest.mark.django_db
class TestLabelCrud:
    def test_create_label(self, project_setup):
        from apps.labels.models import Label

        user, ws, _ = project_setup
        result = CALLABLES["acta_label_create"](user, {"workspace": ws.slug, "name": "backend", "color": "#10b981"})
        assert result["name"] == "backend"
        assert result["color"] == "#10b981"
        assert Label.objects.filter(workspace=ws, name="backend").exists()

    def test_create_in_foreign_workspace_rejected(self, project_setup):
        user, _, _ = project_setup
        other_ws = WorkspaceFactory()
        with pytest.raises(ValueError, match="not found or not accessible"):
            CALLABLES["acta_label_create"](user, {"workspace": other_ws.slug, "name": "x"})

    def test_update_label(self, project_setup):
        user, ws, _ = project_setup
        label = LabelFactory(workspace=ws, name="old", color="#000000")
        result = CALLABLES["acta_label_update"](user, {"id": label.id, "name": "new", "color": "#ffffff"})
        assert result["name"] == "new"
        assert result["color"] == "#ffffff"
        label.refresh_from_db()
        assert label.name == "new"

    def test_delete_label_cascades_associations(self, project_setup):
        from apps.labels.models import Label

        user, ws, project = project_setup
        label = LabelFactory(workspace=ws, name="dropme")
        task = TaskFactory(project=project, reporter=user)
        task.labels.add(label)
        result = CALLABLES["acta_label_delete"](user, {"id": label.id})
        assert result["deleted_id"] == label.id
        assert not Label.objects.filter(pk=label.id).exists()
        # Task survives, association is gone.
        task.refresh_from_db()
        assert task.labels.count() == 0

    def test_delete_other_workspace_label_rejected(self, project_setup):
        user, _, _ = project_setup
        other_ws = WorkspaceFactory()
        label = LabelFactory(workspace=other_ws, name="not yours")
        with pytest.raises(ValueError, match="not found or not accessible"):
            CALLABLES["acta_label_delete"](user, {"id": label.id})


@pytest.mark.django_db
class TestActivityLogIntegration:
    """MCP write-tools must emit the same activity events the web does.

    Otherwise SSE-subscribed web clients miss MCP-driven mutations and
    the audit trail loses MCP as a surface. Verifies actor = MCP user
    so credential attribution survives the trail.
    """

    def test_task_create_emits_task_created_event(self, project_setup):
        from apps.activity.models import ActivityLog

        user, _, project = project_setup
        before = ActivityLog.objects.filter(event_type="task.created").count()
        result = CALLABLES["acta_task_create"](user, {"project": "ACTA", "title": "Logged"})
        after = ActivityLog.objects.filter(event_type="task.created").count()
        assert after - before == 1
        event = ActivityLog.objects.filter(event_type="task.created").order_by("-created_at").first()
        assert event.actor == user
        assert event.workspace == project.workspace
        assert event.target_type == "task"
        assert event.payload["title"] == "Logged"
        assert result["slug"] is not None

    def test_task_update_emits_diff_events(self, project_setup):
        from apps.activity.models import ActivityLog

        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user, status=Task.STATUS_TODO)
        CALLABLES["acta_task_update"](user, {"slug": task.slug, "status": Task.STATUS_DONE})
        events = ActivityLog.objects.filter(
            target_type="task",
            target_id=task.id,
            event_type__startswith="task.",
        )
        # Exactly one diff event for the status change, actor=user.
        status_events = events.filter(event_type="task.status_changed")
        assert status_events.count() == 1
        assert status_events.first().actor == user

    def test_task_archive_emits_archived_event(self, project_setup):
        from apps.activity.models import ActivityLog

        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user)
        CALLABLES["acta_task_archive"](user, {"slug": task.slug})
        archived_events = ActivityLog.objects.filter(
            target_type="task",
            target_id=task.id,
            event_type="task.archived",
        )
        assert archived_events.count() == 1
        assert archived_events.first().actor == user

    def test_comment_create_emits_comment_created_event(self, project_setup):
        from apps.activity.models import ActivityLog

        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user)
        result = CALLABLES["acta_comment_create"](user, {"task": task.slug, "body": "audit me"})
        event = (
            ActivityLog.objects.filter(event_type="comment.created", target_id=result["id"])
            .order_by("-created_at")
            .first()
        )
        assert event is not None
        assert event.actor == user
        assert event.payload["task_id"] == task.id
        assert "audit me" in event.payload["body_preview"]
