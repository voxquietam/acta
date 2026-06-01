"""Tests for the MCP write tools.

``acta_task_create``, ``acta_task_update``, ``acta_task_archive``,
``acta_comment_create``. Each tool routes writes through the same
``TaskSerializer`` / Django models the web UI uses so validation
gates (workspace membership, assignee-must-be-member, subtask-depth-1)
come for free. Labels are the one place the MCP path is more lenient
than the UI — missing names are auto-created instead of rejected.
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
        assert result["status"] == Task.STATUS_PLANNED  # default = backlog
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

    def test_missing_label_auto_created_in_workspace(self, project_setup):
        """A name that doesn't exist gets a fresh Label in this workspace.

        A same-named label can already exist in another workspace — the
        auto-create still happens because label uniqueness is
        workspace-scoped, so the LLM doesn't accidentally borrow
        someone else's label just because the name collides.
        """
        from apps.labels.models import Label

        user, _, project = project_setup
        other_ws = WorkspaceFactory()
        other_label = LabelFactory(workspace=other_ws, name="auto-me")

        result = CALLABLES["acta_task_create"](
            user,
            {"project": "ACTA", "title": "t", "label_names": ["auto-me"]},
        )
        names = [lab["name"] for lab in result["labels"]]
        assert names == ["auto-me"]

        # Workspace-scoped uniqueness: our workspace now has its own
        # ``auto-me`` row, separate from the one in ``other_ws``.
        our_label = Label.objects.get(workspace=project.workspace, name="auto-me")
        assert our_label.pk != other_label.pk
        assert our_label.color.startswith("#")

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

    def test_move_project_renumbers_and_cascades(self, project_setup):
        user, ws, project = project_setup
        dst = ProjectFactory(workspace=ws, slug_prefix="HRW")
        parent = TaskFactory(project=project, reporter=user)
        child = TaskFactory(project=project, reporter=user, parent=parent)
        result = CALLABLES["acta_task_update"](user, {"slug": parent.slug, "project": "HRW"})
        assert result["slug"].startswith("HRW-")
        parent.refresh_from_db()
        child.refresh_from_db()
        assert parent.project_id == dst.id
        assert child.project_id == dst.id  # subtask cascades with its parent

    def test_move_cross_workspace_rejected(self, project_setup):
        user, _, project = project_setup
        other_ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=other_ws)
        ProjectFactory(workspace=other_ws, slug_prefix="OTH")
        task = TaskFactory(project=project, reporter=user)
        with pytest.raises(ValueError, match="[Cc]ross-workspace"):
            CALLABLES["acta_task_update"](user, {"slug": task.slug, "project": "OTH"})

    def test_set_cancelled_status(self, project_setup):
        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user, status=Task.STATUS_TODO)
        result = CALLABLES["acta_task_update"](user, {"slug": task.slug, "status": "cancelled"})
        assert result["status"] == "cancelled"


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


@pytest.mark.django_db
class TestTaskLink:
    def _two_tasks(self, project):
        a = TaskFactory(project=project, title="A")
        b = TaskFactory(project=project, title="B")
        return a, b

    def test_link_blocks(self, project_setup):
        user, _, project = project_setup
        a, b = self._two_tasks(project)
        CALLABLES["acta_task_link"](user, {"slug": a.slug, "target_slug": b.slug, "kind": "blocks"})
        assert b in a.blocks.all()
        assert a in b.blocked_by.all()

    def test_link_blocked_by_reverses(self, project_setup):
        user, _, project = project_setup
        a, b = self._two_tasks(project)
        CALLABLES["acta_task_link"](user, {"slug": a.slug, "target_slug": b.slug, "kind": "blocked_by"})
        assert a in b.blocks.all()

    def test_link_related_symmetric(self, project_setup):
        user, _, project = project_setup
        a, b = self._two_tasks(project)
        CALLABLES["acta_task_link"](user, {"slug": a.slug, "target_slug": b.slug, "kind": "related"})
        assert b in a.related.all()
        assert a in b.related.all()

    def test_link_self_rejected(self, project_setup):
        user, _, project = project_setup
        a, _b = self._two_tasks(project)
        with pytest.raises(ValueError, match="itself"):
            CALLABLES["acta_task_link"](user, {"slug": a.slug, "target_slug": a.slug, "kind": "blocks"})

    def test_link_circular_rejected(self, project_setup):
        user, _, project = project_setup
        a, b = self._two_tasks(project)
        CALLABLES["acta_task_link"](user, {"slug": a.slug, "target_slug": b.slug, "kind": "blocks"})
        with pytest.raises(ValueError, match="circular"):
            CALLABLES["acta_task_link"](user, {"slug": b.slug, "target_slug": a.slug, "kind": "blocks"})

    def test_link_bad_kind_rejected(self, project_setup):
        user, _, project = project_setup
        a, b = self._two_tasks(project)
        with pytest.raises(ValueError, match="kind"):
            CALLABLES["acta_task_link"](user, {"slug": a.slug, "target_slug": b.slug, "kind": "frobnicate"})

    def test_unlink_blocks(self, project_setup):
        user, _, project = project_setup
        a, b = self._two_tasks(project)
        a.blocks.add(b)
        CALLABLES["acta_task_unlink"](user, {"slug": a.slug, "target_slug": b.slug, "kind": "blocks"})
        assert b not in a.blocks.all()

    def test_task_get_includes_links(self, project_setup):
        user, _, project = project_setup
        a, b = self._two_tasks(project)
        a.blocks.add(b)
        payload = CALLABLES["acta_task_get"](user, {"slug": a.slug})
        assert "links" in payload
        blocks_slugs = [t["slug"] for t in payload["links"]["blocks"]]
        assert b.slug in blocks_slugs
        assert payload["is_blocked"] is False


@pytest.fixture
def admin_project_setup():
    """A workspace whose ``user`` is an OWNER (admin-equivalent) member."""
    user = UserFactory()
    ws = WorkspaceFactory(owner=user)  # factory seeds the owner membership
    project = ProjectFactory(workspace=ws, slug_prefix="ACTA")
    return user, ws, project


@pytest.mark.django_db
class TestProjectUpdate:
    def test_member_can_edit_description(self, project_setup):
        user, _, project = project_setup
        result = CALLABLES["acta_project_update"](user, {"slug_prefix": "ACTA", "description": "New body"})
        assert result["description"] == "New body"
        project.refresh_from_db()
        assert project.description == "New body"

    def test_non_admin_cannot_archive(self, project_setup):
        user, _, _ = project_setup  # plain member, not admin/owner
        with pytest.raises(ValueError, match="Only workspace admins"):
            CALLABLES["acta_project_update"](user, {"slug_prefix": "ACTA", "archived": True})

    def test_admin_can_archive_and_rename(self, admin_project_setup):
        user, _, project = admin_project_setup
        result = CALLABLES["acta_project_update"](
            user,
            {"slug_prefix": "ACTA", "archived": True, "name": "Renamed"},
        )
        assert result["archived"] is True
        assert result["name"] == "Renamed"
        project.refresh_from_db()
        assert project.archived is True
        assert project.name == "Renamed"

    def test_lead_must_be_workspace_member(self, admin_project_setup):
        user, ws, _ = admin_project_setup
        outsider = UserFactory()
        with pytest.raises(ValueError, match="must be a member"):
            CALLABLES["acta_project_update"](user, {"slug_prefix": "ACTA", "lead_username": outsider.username})

    def test_lead_can_be_cleared_with_null(self, admin_project_setup):
        user, ws, project = admin_project_setup
        project.lead = user
        project.save(update_fields=["lead"])
        result = CALLABLES["acta_project_update"](user, {"slug_prefix": "ACTA", "lead_username": None})
        assert result["lead_username"] is None
        project.refresh_from_db()
        assert project.lead_id is None

    def test_unknown_field_rejected(self, project_setup):
        user, _, _ = project_setup
        with pytest.raises(ValueError, match="Unknown field"):
            CALLABLES["acta_project_update"](user, {"slug_prefix": "ACTA", "bogus": 1})

    def test_empty_update_rejected(self, project_setup):
        user, _, _ = project_setup
        with pytest.raises(ValueError, match="Nothing to update"):
            CALLABLES["acta_project_update"](user, {"slug_prefix": "ACTA"})


@pytest.mark.django_db
class TestProjectPostUpdate:
    def test_posts_status_update(self, project_setup):
        from apps.projects.models import ProjectUpdate

        user, _, project = project_setup
        result = CALLABLES["acta_project_post_update"](
            user,
            {"project": "ACTA", "health": "on_track", "body": "All good"},
        )
        assert result["health"] == "on_track"
        assert result["body"] == "All good"
        assert ProjectUpdate.objects.filter(project=project, body="All good", author=user).exists()

    def test_invalid_health_rejected(self, project_setup):
        user, _, _ = project_setup
        with pytest.raises(ValueError, match="health"):
            CALLABLES["acta_project_post_update"](user, {"project": "ACTA", "health": "great", "body": "x"})

    def test_empty_body_rejected(self, project_setup):
        user, _, _ = project_setup
        with pytest.raises(ValueError, match="body"):
            CALLABLES["acta_project_post_update"](user, {"project": "ACTA", "health": "on_track", "body": "  "})


@pytest.mark.django_db
class TestCommentUpdate:
    def test_author_can_edit(self, project_setup):
        from apps.activity.models import ActivityLog

        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user)
        comment = Comment.objects.create(task=task, author=user, body="orig")
        result = CALLABLES["acta_comment_update"](user, {"id": comment.id, "body": "edited"})
        assert result["body"] == "edited"
        comment.refresh_from_db()
        assert comment.body == "edited"
        assert ActivityLog.objects.filter(
            event_type="comment.edited",
            target_id=comment.id,
            actor=user,
        ).exists()

    def test_non_author_member_cannot_edit(self, project_setup):
        user, ws, project = project_setup
        other = UserFactory()
        WorkspaceMember.objects.create(user=other, workspace=ws)  # plain member
        task = TaskFactory(project=project, reporter=user)
        comment = Comment.objects.create(task=task, author=user, body="orig")
        with pytest.raises(ValueError, match="author or a workspace admin"):
            CALLABLES["acta_comment_update"](other, {"id": comment.id, "body": "hijack"})

    def test_admin_can_edit_others_comment(self, admin_project_setup):
        user, ws, project = admin_project_setup  # user is owner
        author = UserFactory()
        WorkspaceMember.objects.create(user=author, workspace=ws)
        task = TaskFactory(project=project, reporter=author)
        comment = Comment.objects.create(task=task, author=author, body="orig")
        result = CALLABLES["acta_comment_update"](user, {"id": comment.id, "body": "moderated"})
        assert result["body"] == "moderated"


@pytest.mark.django_db
class TestCommentDelete:
    def test_author_can_delete(self, project_setup):
        from apps.activity.models import ActivityLog

        user, _, project = project_setup
        task = TaskFactory(project=project, reporter=user)
        comment = Comment.objects.create(task=task, author=user, body="bye")
        cid = comment.id
        result = CALLABLES["acta_comment_delete"](user, {"id": cid})
        assert result["deleted_id"] == cid
        assert not Comment.objects.filter(id=cid).exists()
        assert ActivityLog.objects.filter(
            event_type="comment.deleted",
            target_id=cid,
            actor=user,
        ).exists()

    def test_non_author_member_cannot_delete(self, project_setup):
        user, ws, project = project_setup
        other = UserFactory()
        WorkspaceMember.objects.create(user=other, workspace=ws)
        task = TaskFactory(project=project, reporter=user)
        comment = Comment.objects.create(task=task, author=user, body="keep")
        with pytest.raises(ValueError, match="author or a workspace admin"):
            CALLABLES["acta_comment_delete"](other, {"id": comment.id})
        assert Comment.objects.filter(id=comment.id).exists()


@pytest.mark.django_db
class TestProjectCreate:
    def test_admin_creates_project(self):
        user = UserFactory()
        ws = WorkspaceFactory(owner=user)  # owner = admin-equivalent member
        result = CALLABLES["acta_project_create"](
            user,
            {"workspace": ws.slug, "name": "Interface", "slug_prefix": "UI", "description": "UI work"},
        )
        assert result["slug_prefix"] == "UI"
        assert result["description"] == "UI work"
        from apps.projects.models import Project

        assert Project.objects.filter(workspace=ws, slug_prefix="UI", name="Interface").exists()

    def test_non_admin_member_cannot_create(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)  # plain member
        with pytest.raises(ValueError, match="Only workspace admins"):
            CALLABLES["acta_project_create"](user, {"workspace": ws.slug, "name": "X", "slug_prefix": "XX"})

    def test_invalid_slug_prefix_rejected(self):
        user = UserFactory()
        ws = WorkspaceFactory(owner=user)
        with pytest.raises(ValueError, match="validation failed"):
            CALLABLES["acta_project_create"](user, {"workspace": ws.slug, "name": "Bad", "slug_prefix": "lower"})

    def test_lead_must_be_workspace_member(self):
        user = UserFactory()
        ws = WorkspaceFactory(owner=user)
        outsider = UserFactory()
        with pytest.raises(ValueError, match="must be a member"):
            CALLABLES["acta_project_create"](
                user,
                {"workspace": ws.slug, "name": "P", "slug_prefix": "PP", "lead_username": outsider.username},
            )


@pytest.mark.django_db
class TestLabelGroups:
    def test_admin_creates_group_and_label_joins_it(self):
        # NOTE: fresh workspaces auto-seed Type/Area/Layer groups, so use a
        # name that isn't seeded to exercise the create (created=True) path.
        user = UserFactory()
        ws = WorkspaceFactory(owner=user)
        grp = CALLABLES["acta_label_group_create"](
            user, {"workspace": ws.slug, "name": "Severity", "is_exclusive": True}
        )
        assert grp["is_exclusive"] is True
        assert grp["created"] is True
        label = CALLABLES["acta_label_create"](
            user, {"workspace": ws.slug, "name": "blocker", "color": "#ef4444", "group": "Severity"}
        )
        assert label["group_name"] == "Severity"

    def test_group_create_is_idempotent(self):
        user = UserFactory()
        ws = WorkspaceFactory(owner=user)
        first = CALLABLES["acta_label_group_create"](user, {"workspace": ws.slug, "name": "Severity"})
        assert first["created"] is True
        again = CALLABLES["acta_label_group_create"](user, {"workspace": ws.slug, "name": "Severity"})
        assert again["created"] is False

    def test_non_admin_cannot_create_group(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        with pytest.raises(ValueError, match="Only workspace admins"):
            CALLABLES["acta_label_group_create"](user, {"workspace": ws.slug, "name": "Type"})

    def test_label_create_with_unknown_group_errors(self):
        user = UserFactory()
        ws = WorkspaceFactory(owner=user)
        with pytest.raises(ValueError, match="not found in this workspace"):
            CALLABLES["acta_label_create"](user, {"workspace": ws.slug, "name": "bug", "group": "Nope"})

    def test_label_update_regroups_and_ungroups(self):
        user = UserFactory()
        ws = WorkspaceFactory(owner=user)
        CALLABLES["acta_label_group_create"](user, {"workspace": ws.slug, "name": "Type"})
        label = CALLABLES["acta_label_create"](user, {"workspace": ws.slug, "name": "perf"})
        assert label["group_name"] is None
        regrouped = CALLABLES["acta_label_update"](user, {"id": label["id"], "group": "Type"})
        assert regrouped["group_name"] == "Type"
        cleared = CALLABLES["acta_label_update"](user, {"id": label["id"], "group": None})
        assert cleared["group_name"] is None
