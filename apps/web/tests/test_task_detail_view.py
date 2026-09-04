"""Task detail page (``/projects/<slug_prefix>/<number>/``)."""

from django.urls import reverse

import pytest

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.attachments.tests.factories import AttachmentFactory
from apps.comments.tests.factories import CommentFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def task_setup(db):
    """Workspace + project + task + member user.

    Returns:
        Tuple ``(user, project, task)``.
    """
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    task = TaskFactory(project=project, reporter=ws.owner)
    ws.owner.active_workspace = ws
    ws.owner.save(update_fields=["active_workspace"])
    return ws.owner, project, task


@pytest.mark.django_db
class TestTaskDetailAccess:
    """Membership-gated access to the task detail page."""

    def test_anonymous_redirected(self, client, task_setup):
        _, project, task = task_setup
        url = reverse(
            "web:task_detail",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )
        resp = client.get(url)
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_member_can_open(self, client, task_setup):
        user, project, task = task_setup
        client.force_login(user)
        url = reverse(
            "web:task_detail",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )
        resp = client.get(url)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert task.title in body
        assert task.slug in body

    def test_foreign_task_returns_404(self, client, task_setup):
        user, _, _ = task_setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        client.force_login(user)
        url = reverse(
            "web:task_detail",
            kwargs={
                "slug_prefix": foreign_project.slug_prefix,
                "number": foreign_task.number,
            },
        )
        resp = client.get(url)
        assert resp.status_code == 404

    def test_unknown_slug_returns_404(self, client, task_setup):
        user, _, task = task_setup
        client.force_login(user)
        url = reverse(
            "web:task_detail",
            kwargs={"slug_prefix": "NOPE", "number": task.number},
        )
        resp = client.get(url)
        assert resp.status_code == 404


@pytest.mark.django_db
class TestTaskDetailContent:
    """Rendered page contains the expected sections."""

    def test_lists_subtasks(self, client, task_setup):
        user, project, parent = task_setup
        sub = TaskFactory(project=project, parent=parent, reporter=user, title="Sub one")
        client.force_login(user)
        resp = client.get(
            reverse(
                "web:task_detail",
                kwargs={"slug_prefix": project.slug_prefix, "number": parent.number},
            ),
        )
        body = resp.content.decode()
        assert "Sub one" in body
        assert sub.slug in body
        assert sub in resp.context["subtasks"]

    def test_lists_comments(self, client, task_setup):
        user, project, task = task_setup
        comment = CommentFactory(task=task, author=user, body="Looks good")
        client.force_login(user)
        resp = client.get(
            reverse(
                "web:task_detail",
                kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
            ),
        )
        assert "Looks good" in resp.content.decode()
        assert comment in resp.context["comments"]

    def test_shows_activity_for_this_task(self, client, task_setup):
        user, project, task = task_setup
        log_event(
            workspace=project.workspace,
            project=project,
            actor=user,
            event_type="task.status_changed",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"from": "to-do", "to": "in-progress"},
        )
        client.force_login(user)
        resp = client.get(
            reverse(
                "web:task_detail",
                kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
            ),
        )
        events = resp.context["activity"]
        assert len(events) == 1
        assert events[0].event_type == "task.status_changed"


@pytest.mark.django_db
class TestTaskDetailQueryCount:
    """Regression guard: detail page stays N+1-free."""

    def test_constant_queries_with_subtasks_and_comments(
        self,
        client,
        task_setup,
        django_assert_max_num_queries,
        settings,
        tmp_path,
    ):
        settings.MEDIA_ROOT = str(tmp_path / "media")
        user, project, task = task_setup
        for _ in range(5):
            TaskFactory(project=project, parent=task, reporter=user)
        comments = [CommentFactory(task=task, author=user) for _ in range(5)]
        for _ in range(5):
            AttachmentFactory(task=task, workspace=project.workspace, uploader=user)
        for comment in comments:
            AttachmentFactory(task=None, comment=comment, workspace=project.workspace, uploader=user)
        for i in range(5):
            log_event(
                workspace=project.workspace,
                project=project,
                actor=user,
                event_type="task.updated",
                target_type=ActivityLog.TARGET_TASK,
                target_id=task.id,
                payload={"changes": {"title": {"old": "x", "new": f"x{i}"}}},
            )
        client.force_login(user)
        # +1 over the prior cap for the sidebar inbox-unread badge COUNT
        # added by the context processor (ADR 0021); +2 for the task's own
        # reaction summary and the comment-reaction batch; +1 for the
        # comment-replies prefetch; +1 for the workspace-admin role check
        # (drives the comment edit/delete affordances); +1 for the
        # attachments panel (one query for all of the task's files); +2
        # for the comment-attachment prefetches (comment.attachments and
        # replies.attachments); +1 for the move-task project picker
        # (one query listing the workspace's projects). All single queries
        # regardless of row count — constant, not N+1.
        # Cap dropped from 29 → 28 in PR-2 (B3 F1): the duplicate
        # ``task.labels.values_list("id")`` lookup was replaced with a
        # comprehension over the prefetched labels.
        with django_assert_max_num_queries(30):
            client.get(
                reverse(
                    "web:task_detail",
                    kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
                ),
            )

    def test_task_meta_fragment_query_count(self, client, task_setup, django_assert_max_num_queries):
        """SSE-driven meta refresh — runs frequently under peer activity.

        Pulls task + workspace members / labels / label groups / projects /
        cycles + attached label ids. ``attached_label_ids`` should read
        from the prefetched ``labels`` cache (B3 F1), not re-query.
        """
        user, project, task = task_setup
        client.force_login(user)
        with django_assert_max_num_queries(17):
            client.get(
                reverse(
                    "web:task_meta_fragment",
                    kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
                ),
            )

    def test_task_title_fragment_query_count(self, client, task_setup, django_assert_max_num_queries):
        """Topbar / table title SSE refresh — base queryset + render."""
        user, project, task = task_setup
        client.force_login(user)
        with django_assert_max_num_queries(12):
            client.get(
                reverse(
                    "web:task_title_fragment",
                    kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
                ),
            )

    def test_task_description_fragment_query_count(self, client, task_setup, django_assert_max_num_queries):
        """SSE description refresh — base queryset + render."""
        user, project, task = task_setup
        client.force_login(user)
        with django_assert_max_num_queries(12):
            client.get(
                reverse(
                    "web:task_description_fragment",
                    kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
                ),
            )

    def test_task_comments_fragment_query_count(self, client, task_setup, django_assert_max_num_queries):
        """Comment fragment refresh — base + comments + replies + reactions.

        The comment partial fans out a handful of subqueries per comment
        (author render, attachments, reactions); base ~10, +1-2 per comment.
        Upper bound chosen with margin — regression target is "stays
        bounded as comment count grows", which this case (5 comments)
        meets.
        """
        user, project, task = task_setup
        for _ in range(5):
            CommentFactory(task=task, author=user)
        client.force_login(user)
        with django_assert_max_num_queries(31):
            client.get(
                reverse(
                    "web:task_comments_fragment",
                    kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
                ),
            )

    def test_task_timeline_fragment_query_count(self, client, task_setup, django_assert_max_num_queries):
        """Unified comments + activity timeline refresh — bounded under load."""
        user, project, task = task_setup
        for _ in range(5):
            CommentFactory(task=task, author=user)
        for i in range(5):
            log_event(
                workspace=project.workspace,
                project=project,
                actor=user,
                event_type="task.updated",
                target_type=ActivityLog.TARGET_TASK,
                target_id=task.id,
                payload={"changes": {"priority": {"old": 3, "new": i + 1}}},
            )
        client.force_login(user)
        with django_assert_max_num_queries(25):
            client.get(
                reverse(
                    "web:task_timeline_fragment",
                    kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
                ),
            )


@pytest.mark.django_db
class TestDuplicateSlugPrefixAcrossWorkspaces:
    """A slug prefix is unique per workspace, so the URL can be ambiguous.

    Someone in two workspaces that both own, say, a ``SER`` project matched
    two rows for ``/projects/SER/2/``. ``get_object_or_404`` turned that into
    ``MultipleObjectsReturned`` — a 500 on an ordinary click, and only ever
    for people in more than one workspace.
    """

    def _both_workspaces(self):
        """Build two workspaces sharing a prefix, with the same member."""
        first = WorkspaceFactory()
        user = first.owner
        second = WorkspaceFactory()
        second.memberships.create(user=user)
        p1 = ProjectFactory(workspace=first, slug_prefix="SER")
        p2 = ProjectFactory(workspace=second, slug_prefix="SER")
        t1 = TaskFactory(project=p1, number=2, title="first workspace task", reporter=user)
        t2 = TaskFactory(project=p2, number=2, title="second workspace task", reporter=user)
        return user, (first, t1), (second, t2)

    def _url(self, task):
        return reverse(
            "web:task_detail",
            kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
        )

    def test_duplicate_prefix_offers_a_choice(self, client):
        """No guessing: both are reachable, so the user picks."""
        user, (_, t1), (_, t2) = self._both_workspaces()
        client.force_login(user)
        response = client.get(self._url(t1))
        assert response.status_code == 300
        assert t1.title.encode() in response.content
        assert t2.title.encode() in response.content

    def test_workspace_hint_resolves_directly(self, client):
        """Links made inside the app carry ``?w=`` and skip the choice."""
        user, (first, t1), (second, t2) = self._both_workspaces()
        client.force_login(user)

        r1 = client.get(self._url(t1), {"w": first.slug})
        assert r1.status_code == 200
        assert r1.context["task"].pk == t1.pk

        r2 = client.get(self._url(t2), {"w": second.slug})
        assert r2.status_code == 200
        assert r2.context["task"].pk == t2.pk

    def test_single_reachable_match_opens_without_asking(self, client):
        """Only one of the two workspaces is visible, so there is no choice.

        The queryset is already membership-scoped, so a collision the user
        cannot reach never becomes a question.
        """
        user, (first, t1), (second, _) = self._both_workspaces()
        second.memberships.filter(user=user).delete()
        client.force_login(user)
        response = client.get(self._url(t1))
        assert response.status_code == 200
        assert response.context["task"].pk == t1.pk

    def test_stale_hint_falls_back_to_the_choice(self, client):
        """A hint naming a workspace without the task must not 404 it."""
        user, (_, t1), _ = self._both_workspaces()
        client.force_login(user)
        assert client.get(self._url(t1), {"w": "no-such-workspace"}).status_code == 300

    def test_modal_variant_also_offers_the_choice(self, client):
        """The kanban card click fetches ``?modal=1`` — same lookup path."""
        user, (_, t1), _ = self._both_workspaces()
        client.force_login(user)
        response = client.get(self._url(t1), {"modal": "1"}, HTTP_HX_REQUEST="true")
        assert response.status_code == 300

    def test_opening_a_task_focuses_its_workspace(self, client):
        """The sidebar must follow the task, as it already does for projects.

        Following a link into another workspace used to leave the whole app
        — sidebar, All Tasks, every scoped view — pointing at the previous
        one while a task from elsewhere sat on screen.
        """
        user, (first, t1), (second, t2) = self._both_workspaces()
        user.active_workspace = first
        user.save(update_fields=["active_workspace"])
        client.force_login(user)

        client.get(self._url(t2), {"w": second.slug})
        user.refresh_from_db()
        assert user.active_workspace_id == second.pk

    def test_modal_does_not_move_the_workspace(self, client):
        """A modal is a peek over the current board, not a navigation."""
        user, (first, _), (second, t2) = self._both_workspaces()
        user.active_workspace = first
        user.save(update_fields=["active_workspace"])
        client.force_login(user)

        client.get(self._url(t2), {"w": second.slug, "modal": "1"}, HTTP_HX_REQUEST="true")
        user.refresh_from_db()
        assert user.active_workspace_id == first.pk


@pytest.mark.django_db
class TestDuplicateProjectPrefix:
    """The project page collides the same way a task URL does."""

    def _two_projects(self):
        first = WorkspaceFactory()
        user = first.owner
        second = WorkspaceFactory()
        second.memberships.create(user=user)
        p1 = ProjectFactory(workspace=first, slug_prefix="SER", name="Server one")
        p2 = ProjectFactory(workspace=second, slug_prefix="SER", name="Server two")
        return user, (first, p1), (second, p2)

    def _url(self, project):
        return reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix})

    def test_duplicate_prefix_offers_a_choice(self, client):
        user, (_, p1), (_, p2) = self._two_projects()
        client.force_login(user)
        response = client.get(self._url(p1))
        assert response.status_code == 300
        assert p1.name.encode() in response.content
        assert p2.name.encode() in response.content

    def test_hint_resolves_and_focuses_the_workspace(self, client):
        user, (first, p1), (second, p2) = self._two_projects()
        user.active_workspace = first
        user.save(update_fields=["active_workspace"])
        client.force_login(user)
        response = client.get(self._url(p2), {"w": second.slug})
        assert response.status_code == 200
        user.refresh_from_db()
        assert user.active_workspace_id == second.pk

    def test_single_reachable_match_opens_without_asking(self, client):
        user, (first, p1), (second, _) = self._two_projects()
        second.memberships.filter(user=user).delete()
        client.force_login(user)
        assert client.get(self._url(p1)).status_code == 200
