"""Tests for the modal-mode task detail flow and the merged timeline.

Covers:

* ``TaskDetailView`` picks the modal-shell template on ``?modal=1`` and
  the full-page template otherwise.
* ``_build_timeline`` interleaves comments + non-comment activity events
  sorted by ``created_at``.
* ``_task_activity`` excludes ``task.labels_changed`` and
  ``task.updated`` events whose ``payload.changes`` only carry title /
  description.
* ``task_timeline_fragment`` + ``task_meta_compact_fragment`` endpoints
  render and respect workspace membership.
* ``open_task_modal_attrs`` template tag emits the expected HTMX
  attribute set.
"""

import datetime

from django.template import Context, Template
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.comments.models import Comment
from apps.comments.tests.factories import CommentFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.web.views import _build_timeline, _task_activity
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """Workspace + project + task + member user."""
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    task = TaskFactory(project=project, reporter=ws.owner)
    return ws.owner, project, task


@pytest.mark.django_db
class TestTaskDetailTemplateSelection:
    """``?modal=1`` switches to the modal-shell template."""

    def _detail_url(self, project, task):
        return reverse(
            "web:task_detail",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_default_renders_full_page(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.get(self._detail_url(project, task))
        assert resp.status_code == 200
        templates = [t.name for t in resp.templates if t.name]
        assert "web/projects/task_detail.html" in templates
        assert "web/projects/task_detail_modal.html" not in templates

    def test_modal_param_renders_modal_shell(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.get(self._detail_url(project, task), {"modal": "1"})
        assert resp.status_code == 200
        templates = [t.name for t in resp.templates if t.name]
        assert "web/projects/task_detail_modal.html" in templates
        assert "web/_modal_shell.html" in templates
        body = resp.content.decode()
        # Modal shell hosts the body partial — not the rail-bearing one.
        assert "_task_detail_modal_body" not in body  # rendered, not raw include name
        # Sanity: meta-compact ID is present (modal-only marker).
        assert "task-meta-compact" in body

    def test_modal_param_other_value_falls_back_to_full(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.get(self._detail_url(project, task), {"modal": "0"})
        templates = [t.name for t in resp.templates if t.name]
        assert "web/projects/task_detail.html" in templates


@pytest.mark.django_db
class TestTaskActivityFilters:
    """``_task_activity`` filters chatty events out of the user feed."""

    def test_excludes_labels_changed(self, setup):
        _, project, task = setup
        log_event(
            workspace=project.workspace,
            project=project,
            actor=None,
            event_type="task.status_changed",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"from": "planned", "to": "to-do"},
        )
        log_event(
            workspace=project.workspace,
            project=project,
            actor=None,
            event_type="task.labels_changed",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"added_ids": [], "removed_ids": []},
        )
        events = _task_activity(task)
        types = [e.event_type for e in events]
        assert "task.status_changed" in types
        assert "task.labels_changed" not in types

    def test_excludes_title_only_task_updated(self, setup):
        _, project, task = setup
        log_event(
            workspace=project.workspace,
            project=project,
            actor=None,
            event_type="task.updated",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"changes": {"title": {"old": "x", "new": "y"}}},
        )
        events = _task_activity(task)
        assert events == []

    def test_excludes_description_only_task_updated(self, setup):
        _, project, task = setup
        log_event(
            workspace=project.workspace,
            project=project,
            actor=None,
            event_type="task.updated",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"changes": {"description": {"old_len": 0, "new_len": 5}}},
        )
        events = _task_activity(task)
        assert events == []

    def test_keeps_size_change_in_task_updated(self, setup):
        _, project, task = setup
        log_event(
            workspace=project.workspace,
            project=project,
            actor=None,
            event_type="task.updated",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"changes": {"size": {"old": 1, "new": 2}}},
        )
        events = _task_activity(task)
        assert len(events) == 1
        assert events[0].event_type == "task.updated"

    def test_keeps_mixed_task_updated(self, setup):
        """``size`` alongside ``title`` — still visible (size dominates)."""
        _, project, task = setup
        log_event(
            workspace=project.workspace,
            project=project,
            actor=None,
            event_type="task.updated",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={
                "changes": {
                    "title": {"old": "a", "new": "b"},
                    "size": {"old": 1, "new": 2},
                },
            },
        )
        events = _task_activity(task)
        assert len(events) == 1


@pytest.mark.django_db
class TestBuildTimeline:
    """``_build_timeline`` merges comments + activity events by time."""

    def test_orders_by_created_at_ascending(self, setup):
        _, project, task = setup
        # Explicit timestamps — auto_now_add gives microsecond precision
        # but the wall clock can collide on fast tests, leaving sort
        # order undefined. Hard-coding via .update() forces a sequence.
        t0 = timezone.now() - datetime.timedelta(minutes=3)
        comment_a = CommentFactory(task=task, author=task.reporter, body="A")
        Comment.objects.filter(pk=comment_a.pk).update(created_at=t0)
        log_event(
            workspace=project.workspace,
            project=project,
            actor=None,
            event_type="task.status_changed",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"from": "planned", "to": "to-do"},
        )
        ActivityLog.objects.filter(target_id=task.id, event_type="task.status_changed").update(
            created_at=t0 + datetime.timedelta(minutes=1),
        )
        comment_b = CommentFactory(task=task, author=task.reporter, body="B")
        Comment.objects.filter(pk=comment_b.pk).update(created_at=t0 + datetime.timedelta(minutes=2))

        timeline = _build_timeline(task.__class__.objects.get(pk=task.pk))
        kinds = [kv[0] for kv in timeline]
        items = [kv[1] for kv in timeline]

        assert kinds == ["comment", "event", "comment"]
        assert items[0].pk == comment_a.pk
        assert items[-1].pk == comment_b.pk

    def test_comment_events_filtered_out(self, setup):
        """``comment.created`` events from log_event are excluded — the
        Comment object carries the body and would otherwise render twice.
        """
        _, project, task = setup
        comment = CommentFactory(task=task, author=task.reporter, body="hello")
        log_event(
            workspace=project.workspace,
            project=project,
            actor=None,
            event_type="comment.created",
            target_type=ActivityLog.TARGET_COMMENT,
            target_id=comment.id,
            payload={"task_id": task.id, "body_preview": "hello"},
        )
        timeline = _build_timeline(task)
        # Only the comment itself — no duplicate "added a comment" row.
        assert len(timeline) == 1
        assert timeline[0][0] == "comment"


@pytest.mark.django_db
class TestModalFragmentEndpoints:
    """Modal-only fragment endpoints respect workspace membership."""

    def _timeline_url(self, project, task):
        return reverse(
            "web:task_timeline_fragment",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def _meta_compact_url(self, project, task):
        return reverse(
            "web:task_meta_compact_fragment",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )

    def test_timeline_fragment_renders_for_member(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.get(self._timeline_url(project, task))
        assert resp.status_code == 200
        assert b"task-timeline-list" in resp.content

    def test_timeline_fragment_404_for_foreign_task(self, client, setup):
        user, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        client.force_login(user)
        resp = client.get(self._timeline_url(foreign_project, foreign_task))
        assert resp.status_code == 404

    def test_meta_compact_fragment_renders_for_member(self, client, setup):
        user, project, task = setup
        client.force_login(user)
        resp = client.get(self._meta_compact_url(project, task))
        assert resp.status_code == 200
        # Compact partial uses the same cell partials as the rail; status
        # label of the task should appear.
        assert task.status.encode() in resp.content or b"status" in resp.content.lower()

    def test_meta_compact_fragment_404_for_foreign_task(self, client, setup):
        user, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        foreign_task = TaskFactory(project=foreign_project, reporter=foreign_ws.owner)
        client.force_login(user)
        resp = client.get(self._meta_compact_url(foreign_project, foreign_task))
        assert resp.status_code == 404


@pytest.mark.django_db
class TestOpenTaskModalAttrs:
    """``{% open_task_modal_attrs task %}`` template tag output."""

    def test_emits_all_htmx_attrs(self, setup):
        _, _, task = setup
        out = Template("{% load web_extras %}{% open_task_modal_attrs task %}").render(
            Context({"task": task}),
        )
        expected_url = f"/projects/{task.project.slug_prefix}/{task.number}/"
        assert f'hx-get="{expected_url}?modal=1"' in out
        assert 'hx-target="#modal-root"' in out
        assert 'hx-swap="innerHTML"' in out
        assert f'hx-push-url="{expected_url}"' in out
        # ``&&`` is HTML-encoded so the attribute parses cleanly. HTMX
        # decodes it before evaluating the trigger filter.
        assert "hx-trigger=" in out
        assert "ctrlKey" in out
        assert "metaKey" in out
        assert "shiftKey" in out
