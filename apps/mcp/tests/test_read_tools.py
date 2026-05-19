"""Tests for the MCP read-only tools.

Workspaces / projects / tasks listing. Each tool is called directly
via its dispatch callable (no MCP framework involved) so we test the
permission gates and shape of the payload without a stdio round-trip.
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.labels.tests.factories import LabelFactory
from apps.mcp.tools import CALLABLES
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestWorkspacesList:
    def test_returns_user_workspaces(self):
        user = UserFactory()
        ws1 = WorkspaceFactory(name="Beta")
        WorkspaceMember.objects.create(user=user, workspace=ws1)
        ws2 = WorkspaceFactory(name="Alpha")
        WorkspaceMember.objects.create(user=user, workspace=ws2)
        WorkspaceFactory(name="HiddenFromUser")  # user is NOT a member of this one

        result = CALLABLES["acta_workspaces_list"](user, {})
        names = {row["name"] for row in result}
        assert names == {"Alpha", "Beta"}
        assert "HiddenFromUser" not in names
        # Alphabetical order — Alpha before Beta.
        assert [row["name"] for row in result] == ["Alpha", "Beta"]


@pytest.mark.django_db
class TestProjectsList:
    def test_returns_user_accessible_projects(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        my_proj = ProjectFactory(workspace=ws, name="My Proj")
        ProjectFactory(name="Other Proj")  # different workspace

        result = CALLABLES["acta_projects_list"](user, {})
        slugs = {row["slug_prefix"] for row in result}
        assert my_proj.slug_prefix in slugs
        assert len(slugs) == 1

    def test_workspace_filter_scopes_results(self):
        user = UserFactory()
        ws_a = WorkspaceFactory()
        ws_b = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws_a)
        WorkspaceMember.objects.create(user=user, workspace=ws_b)
        ProjectFactory(workspace=ws_a, name="In A")
        ProjectFactory(workspace=ws_b, name="In B")

        result = CALLABLES["acta_projects_list"](user, {"workspace": ws_a.slug})
        assert [row["name"] for row in result] == ["In A"]

    def test_archived_excluded_by_default(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        ProjectFactory(workspace=ws, name="Active", archived=False)
        ProjectFactory(workspace=ws, name="Archived", archived=True)

        result = CALLABLES["acta_projects_list"](user, {})
        assert [row["name"] for row in result] == ["Active"]

        result_all = CALLABLES["acta_projects_list"](user, {"include_archived": True})
        names = {row["name"] for row in result_all}
        assert names == {"Active", "Archived"}


@pytest.mark.django_db
class TestTasksList:
    def test_filters_by_calling_user_memberships(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        my_proj = ProjectFactory(workspace=ws)
        mine = TaskFactory(project=my_proj, reporter=user, title="mine")
        TaskFactory(title="theirs")  # different workspace, no membership

        result = CALLABLES["acta_tasks_list"](user, {})
        titles = {row["title"] for row in result}
        assert "mine" in titles
        assert "theirs" not in titles
        assert mine.slug in {row["slug"] for row in result}

    def test_status_filter_string(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws)
        TaskFactory(project=p, reporter=user, status=Task.STATUS_TODO, title="todo")
        TaskFactory(project=p, reporter=user, status=Task.STATUS_DONE, title="done")

        result = CALLABLES["acta_tasks_list"](user, {"status": Task.STATUS_TODO})
        assert {row["title"] for row in result} == {"todo"}

    def test_status_filter_list(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws)
        TaskFactory(project=p, reporter=user, status=Task.STATUS_TODO, title="todo")
        TaskFactory(project=p, reporter=user, status=Task.STATUS_IN_PROGRESS, title="in-progress")
        TaskFactory(project=p, reporter=user, status=Task.STATUS_DONE, title="done")

        result = CALLABLES["acta_tasks_list"](user, {"status": [Task.STATUS_TODO, Task.STATUS_IN_PROGRESS]})
        assert {row["title"] for row in result} == {"todo", "in-progress"}

    def test_assignee_me_filter(self):
        user = UserFactory()
        other = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        WorkspaceMember.objects.create(user=other, workspace=ws)
        p = ProjectFactory(workspace=ws)
        TaskFactory(project=p, reporter=user, assignee=user, title="mine")
        TaskFactory(project=p, reporter=user, assignee=other, title="theirs")

        result = CALLABLES["acta_tasks_list"](user, {"assignee": "me"})
        assert {row["title"] for row in result} == {"mine"}

    def test_assignee_unassigned_filter(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws)
        TaskFactory(project=p, reporter=user, assignee=None, title="no one")
        TaskFactory(project=p, reporter=user, assignee=user, title="me")

        result = CALLABLES["acta_tasks_list"](user, {"assignee": "unassigned"})
        assert {row["title"] for row in result} == {"no one"}

    def test_project_filter_by_slug_prefix(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        a = ProjectFactory(workspace=ws, slug_prefix="AAA")
        b = ProjectFactory(workspace=ws, slug_prefix="BBB")
        TaskFactory(project=a, reporter=user, title="in-a")
        TaskFactory(project=b, reporter=user, title="in-b")

        result = CALLABLES["acta_tasks_list"](user, {"project": "AAA"})
        assert {row["title"] for row in result} == {"in-a"}

    def test_search_q_matches_title_or_description(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws)
        TaskFactory(project=p, reporter=user, title="needle here")
        TaskFactory(project=p, reporter=user, title="other", description="needle in body")
        TaskFactory(project=p, reporter=user, title="no match")

        result = CALLABLES["acta_tasks_list"](user, {"q": "needle"})
        titles = {row["title"] for row in result}
        assert titles == {"needle here", "other"}

    def test_archived_excluded_by_default(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws)
        TaskFactory(project=p, reporter=user, title="live")
        archived = TaskFactory(project=p, reporter=user, title="archived")
        archived.archived_at = timezone.now()
        archived.save(update_fields=["archived_at"])

        result = CALLABLES["acta_tasks_list"](user, {})
        assert {row["title"] for row in result} == {"live"}

        result_all = CALLABLES["acta_tasks_list"](user, {"include_archived": True})
        assert {row["title"] for row in result_all} == {"live", "archived"}

    def test_labels_in_payload(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws)
        backend = LabelFactory(workspace=ws, name="backend", color="#10b981")
        bug = LabelFactory(workspace=ws, name="bug", color="#f43f5e")
        task = TaskFactory(project=p, reporter=user)
        task.labels.add(backend, bug)

        result = CALLABLES["acta_tasks_list"](user, {})
        row = next(r for r in result if r["slug"] == task.slug)
        names = {lab["name"] for lab in row["labels"]}
        assert names == {"backend", "bug"}
        colors = {lab["color"] for lab in row["labels"]}
        assert colors == {"#10b981", "#f43f5e"}

    def test_limit_caps_at_200(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws)
        for i in range(5):
            TaskFactory(project=p, reporter=user, title=f"t{i}")

        result = CALLABLES["acta_tasks_list"](user, {"limit": 2})
        assert len(result) == 2

        result_huge = CALLABLES["acta_tasks_list"](user, {"limit": 9999})
        # 5 tasks total, limit capped at 200 internally; we only have 5.
        assert len(result_huge) == 5


@pytest.mark.django_db
class TestTaskGet:
    def _setup(self, user):
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws, slug_prefix="ACTA")
        return ws, p

    def test_returns_full_task_payload(self):
        user = UserFactory()
        ws, p = self._setup(user)
        task = TaskFactory(
            project=p,
            reporter=user,
            assignee=user,
            title="Add bulk archive cascade",
            description="Walk subtasks under the parent on archive.",
            status=Task.STATUS_IN_PROGRESS,
            priority=Task.HIGH,
        )
        backend = LabelFactory(workspace=ws, name="backend", color="#10b981")
        task.labels.add(backend)

        result = CALLABLES["acta_task_get"](user, {"slug": task.slug})
        assert result["slug"] == task.slug
        assert result["title"] == "Add bulk archive cascade"
        assert "Walk subtasks" in result["description"]
        assert result["status"] == Task.STATUS_IN_PROGRESS
        assert result["priority"] == Task.HIGH
        assert result["project_slug_prefix"] == "ACTA"
        assert result["workspace_slug"] == ws.slug
        assert result["assignee_username"] == user.username
        assert {"name": "backend", "color": "#10b981"} in result["labels"]

    def test_includes_subtasks(self):
        user = UserFactory()
        _, p = self._setup(user)
        parent = TaskFactory(project=p, reporter=user, title="Parent")
        sub_a = TaskFactory(project=p, reporter=user, parent=parent, title="Step A")
        sub_b = TaskFactory(project=p, reporter=user, parent=parent, title="Step B")

        result = CALLABLES["acta_task_get"](user, {"slug": parent.slug})
        sub_titles = [s["title"] for s in result["subtasks"]]
        assert set(sub_titles) == {sub_a.title, sub_b.title}

    def test_includes_comments(self):
        from apps.comments.models import Comment

        user = UserFactory()
        _, p = self._setup(user)
        task = TaskFactory(project=p, reporter=user)
        Comment.objects.create(task=task, author=user, body="First comment")
        Comment.objects.create(task=task, author=user, body="Second comment")

        result = CALLABLES["acta_task_get"](user, {"slug": task.slug})
        bodies = [c["body"] for c in result["comments"]]
        assert bodies == ["First comment", "Second comment"]
        assert result["comments"][0]["author_username"] == user.username
        assert result["comments"][0]["edited"] is False

    def test_includes_activity_log(self):
        from apps.activity.models import ActivityLog

        user = UserFactory()
        ws, p = self._setup(user)
        task = TaskFactory(project=p, reporter=user)
        # Synthesise an activity event (status change).
        ActivityLog.objects.create(
            workspace=ws,
            project=p,
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            actor=user,
            event_type="task.status_changed",
            payload={"from": "to-do", "to": "in-progress"},
        )

        result = CALLABLES["acta_task_get"](user, {"slug": task.slug})
        assert len(result["activity"]) == 1
        event = result["activity"][0]
        assert event["event_type"] == "task.status_changed"
        assert event["payload"]["to"] == "in-progress"
        assert event["actor_username"] == user.username

    def test_missing_slug_raises(self):
        user = UserFactory()
        with pytest.raises(ValueError, match="required"):
            CALLABLES["acta_task_get"](user, {})

    def test_malformed_slug_raises(self):
        user = UserFactory()
        with pytest.raises(ValueError, match="Invalid slug format"):
            CALLABLES["acta_task_get"](user, {"slug": "not-a-real-slug-format"})

    def test_other_user_task_raises_not_found(self):
        owner = UserFactory()
        intruder = UserFactory()
        _, p = self._setup(owner)
        task = TaskFactory(project=p, reporter=owner)
        with pytest.raises(ValueError, match="not found or not accessible"):
            CALLABLES["acta_task_get"](intruder, {"slug": task.slug})


@pytest.mark.django_db
class TestActivityList:
    def _setup(self, user):
        from apps.activity.models import ActivityLog

        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws, slug_prefix="ACTA")
        return ws, p, ActivityLog

    def test_filters_by_user_workspaces(self):
        user = UserFactory()
        other = UserFactory()
        ws, p, ActivityLog = self._setup(user)
        other_ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=other, workspace=other_ws)

        t = TaskFactory(project=p, reporter=user)
        ActivityLog.objects.create(
            workspace=ws,
            project=p,
            target_type="task",
            target_id=t.id,
            actor=user,
            event_type="task.created",
            payload={},
        )
        # An event in a workspace the calling user can't see.
        ActivityLog.objects.create(
            workspace=other_ws,
            project=None,
            target_type="task",
            target_id=99999,
            actor=other,
            event_type="task.created",
            payload={},
        )

        result = CALLABLES["acta_activity_list"](user, {})
        events = [(e["event_type"], e["workspace_slug"]) for e in result]
        assert ("task.created", ws.slug) in events
        assert ("task.created", other_ws.slug) not in events

    def test_event_type_filter_string(self):
        user = UserFactory()
        ws, p, ActivityLog = self._setup(user)
        t = TaskFactory(project=p, reporter=user)
        ActivityLog.objects.create(
            workspace=ws,
            project=p,
            target_type="task",
            target_id=t.id,
            actor=user,
            event_type="task.status_changed",
            payload={},
        )
        ActivityLog.objects.create(
            workspace=ws,
            project=p,
            target_type="task",
            target_id=t.id,
            actor=user,
            event_type="task.archived",
            payload={},
        )

        result = CALLABLES["acta_activity_list"](user, {"event_type": "task.status_changed"})
        types = {e["event_type"] for e in result}
        assert types == {"task.status_changed"}

    def test_task_filter_includes_comment_events(self):
        from apps.activity.models import ActivityLog

        user = UserFactory()
        ws, p, _ = self._setup(user)
        task = TaskFactory(project=p, reporter=user)
        ActivityLog.objects.create(
            workspace=ws,
            project=p,
            target_type="task",
            target_id=task.id,
            actor=user,
            event_type="task.created",
            payload={},
        )
        ActivityLog.objects.create(
            workspace=ws,
            project=p,
            target_type="comment",
            target_id=42,
            actor=user,
            event_type="comment.created",
            payload={"task_id": task.id, "comment_id": 42},
        )
        # Comment event on a DIFFERENT task (shouldn't surface).
        ActivityLog.objects.create(
            workspace=ws,
            project=p,
            target_type="comment",
            target_id=43,
            actor=user,
            event_type="comment.created",
            payload={"task_id": 99999, "comment_id": 43},
        )

        result = CALLABLES["acta_activity_list"](user, {"task": task.slug})
        event_types = sorted(e["event_type"] for e in result)
        assert event_types == ["comment.created", "task.created"]

    def test_actor_filter(self):
        from apps.activity.models import ActivityLog

        user = UserFactory()
        mate = UserFactory(username="kate")
        ws, p, _ = self._setup(user)
        WorkspaceMember.objects.create(user=mate, workspace=ws)
        t = TaskFactory(project=p, reporter=user)
        ActivityLog.objects.create(
            workspace=ws,
            project=p,
            target_type="task",
            target_id=t.id,
            actor=user,
            event_type="task.created",
            payload={},
        )
        ActivityLog.objects.create(
            workspace=ws,
            project=p,
            target_type="task",
            target_id=t.id,
            actor=mate,
            event_type="task.status_changed",
            payload={},
        )

        result = CALLABLES["acta_activity_list"](user, {"actor": "kate"})
        assert {e["actor_username"] for e in result} == {"kate"}


@pytest.mark.django_db
class TestCommentsList:
    def _setup(self, user):
        from apps.comments.models import Comment

        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws, slug_prefix="ACTA")
        return ws, p, Comment

    def test_filters_by_user_workspaces(self):
        user = UserFactory()
        ws, p, Comment = self._setup(user)
        my_task = TaskFactory(project=p, reporter=user)
        Comment.objects.create(task=my_task, author=user, body="visible")

        other_ws = WorkspaceFactory()
        other_proj = ProjectFactory(workspace=other_ws)
        other_task = TaskFactory(project=other_proj, reporter=user)
        Comment.objects.create(task=other_task, author=user, body="hidden — different ws")

        result = CALLABLES["acta_comments_list"](user, {})
        bodies = {c["body"] for c in result}
        assert "visible" in bodies
        assert "hidden — different ws" not in bodies

    def test_task_filter(self):
        user = UserFactory()
        _, p, Comment = self._setup(user)
        t1 = TaskFactory(project=p, reporter=user)
        t2 = TaskFactory(project=p, reporter=user)
        Comment.objects.create(task=t1, author=user, body="on t1")
        Comment.objects.create(task=t2, author=user, body="on t2")

        result = CALLABLES["acta_comments_list"](user, {"task": t1.slug})
        assert {c["body"] for c in result} == {"on t1"}

    def test_q_searches_body(self):
        user = UserFactory()
        _, p, Comment = self._setup(user)
        task = TaskFactory(project=p, reporter=user)
        Comment.objects.create(task=task, author=user, body="cascade subtasks on archive")
        Comment.objects.create(task=task, author=user, body="not related")

        result = CALLABLES["acta_comments_list"](user, {"q": "cascade"})
        assert len(result) == 1
        assert "cascade" in result[0]["body"]


@pytest.mark.django_db
class TestQueryCounts:
    """N+1 regression guard for the bulk list tools.

    Each tool should run a CONSTANT number of queries regardless of
    how many rows it returns. The numbers below are upper bounds
    (workspace-ids lookup + main query + prefetch passes) — if a
    refactor adds a per-row query, the count blows up and the test
    catches it.
    """

    def test_tasks_list_is_constant_query_count(self):
        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws)
        labels = [LabelFactory(workspace=ws) for _ in range(3)]
        for _ in range(20):
            t = TaskFactory(project=p, reporter=user, assignee=user)
            t.labels.set(labels)

        with CaptureQueriesContext(connection) as ctx_small:
            CALLABLES["acta_tasks_list"](user, {"limit": 5})

        with CaptureQueriesContext(connection) as ctx_large:
            CALLABLES["acta_tasks_list"](user, {"limit": 20})

        # Same query count regardless of row count — the prefetch
        # for ``labels`` is one extra query, not one per task.
        assert len(ctx_small.captured_queries) == len(ctx_large.captured_queries)
        # Reasonable upper bound for the constant shape: ws-ids +
        # main task query + labels prefetch ≈ 3-4 queries.
        assert len(ctx_large.captured_queries) <= 6

    def test_comments_list_is_constant_query_count(self):
        from apps.comments.models import Comment

        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws)
        task = TaskFactory(project=p, reporter=user)
        for i in range(20):
            Comment.objects.create(task=task, author=user, body=f"c{i}")

        with CaptureQueriesContext(connection) as ctx_small:
            CALLABLES["acta_comments_list"](user, {"limit": 5})

        with CaptureQueriesContext(connection) as ctx_large:
            CALLABLES["acta_comments_list"](user, {"limit": 20})

        assert len(ctx_small.captured_queries) == len(ctx_large.captured_queries)
        # ws-ids + main comments query (with select_related → author /
        # task / project / workspace pre-joined) ≈ 2 queries.
        assert len(ctx_large.captured_queries) <= 4

    def test_activity_list_is_constant_query_count(self):
        from apps.activity.models import ActivityLog

        user = UserFactory()
        ws = WorkspaceFactory()
        WorkspaceMember.objects.create(user=user, workspace=ws)
        p = ProjectFactory(workspace=ws)
        t = TaskFactory(project=p, reporter=user)
        for i in range(20):
            ActivityLog.objects.create(
                workspace=ws,
                project=p,
                target_type="task",
                target_id=t.id,
                actor=user,
                event_type=f"event.{i}",
                payload={},
            )

        with CaptureQueriesContext(connection) as ctx_small:
            CALLABLES["acta_activity_list"](user, {"limit": 5})

        with CaptureQueriesContext(connection) as ctx_large:
            CALLABLES["acta_activity_list"](user, {"limit": 20})

        assert len(ctx_small.captured_queries) == len(ctx_large.captured_queries)
        assert len(ctx_large.captured_queries) <= 4
