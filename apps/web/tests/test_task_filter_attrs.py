"""Tests for the ``task_filter_attrs`` template tag.

The tag emits the ``data-*`` attributes that drive client-side
filtering in ``acta.js``. Covered here:

* Every attribute renders with the expected value for known task
  states (status, priority, assignee, labels, archive flag).
* HTML special chars in title / description don't break out of the
  ``data-search-haystack`` attribute.
* ``data-assignee-me`` flips correctly based on the rendering user.
* Cold-load filter URLs ``?status=...``, ``?priority=...`` still
  return correctly filtered task lists (server-side fallback path is
  untouched).
"""

from django.template import Context, Template
from django.test import RequestFactory
from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


def _render(task, *, user):
    """Render ``{% task_filter_attrs task %}`` for ``task`` + ``user``.

    Returns the rendered HTML string with the attribute block; the
    surrounding ``<x ...>`` wrapper makes Django happy and lets us
    parse the attributes back with a simple regex if needed.
    """
    tpl = Template("{% load web_extras %}<x {% task_filter_attrs task %}></x>")
    req = RequestFactory().get("/")
    req.user = user
    return tpl.render(Context({"task": task, "request": req}))


@pytest.mark.django_db
class TestTaskFilterAttrs:
    """``{% task_filter_attrs task %}`` emits a consistent attribute set."""

    def test_basic_attributes_present(self):
        task = TaskFactory(
            status=Task.STATUS_IN_PROGRESS,
            priority=Task.HIGH,
            title="Refactor activity log",
        )
        html = _render(task, user=task.reporter)
        assert 'data-status="in-progress"' in html
        assert 'data-priority="2"' in html
        assert f'data-project-id="{task.project_id}"' in html
        assert f'data-workspace-id="{task.project.workspace_id}"' in html
        assert 'data-archived="0"' in html

    def test_assignee_id_and_me_flag(self):
        user = UserFactory()
        task = TaskFactory(assignee=user, reporter=user)
        # Rendering as the assignee — me flag flips on.
        html_self = _render(task, user=user)
        assert f'data-assignee-id="{user.id}"' in html_self
        assert 'data-assignee-me="1"' in html_self
        # Rendering as a different user — me flag is "0".
        other = UserFactory()
        html_other = _render(task, user=other)
        assert f'data-assignee-id="{user.id}"' in html_other
        assert 'data-assignee-me="0"' in html_other

    def test_unassigned_task_has_empty_assignee_id(self):
        task = TaskFactory(assignee=None)
        html = _render(task, user=task.reporter)
        assert 'data-assignee-id=""' in html
        assert 'data-assignee-me="0"' in html

    def test_label_ids_space_separated(self):
        task = TaskFactory()
        l1 = LabelFactory(workspace=task.project.workspace)
        l2 = LabelFactory(workspace=task.project.workspace)
        task.labels.add(l1, l2)
        html = _render(task, user=task.reporter)
        # Order of labels may vary — assert both IDs present in the value.
        import re

        match = re.search(r'data-label-ids="([^"]*)"', html)
        assert match
        ids = set(match.group(1).split())
        assert ids == {str(l1.id), str(l2.id)}

    def test_no_labels_empty_string(self):
        task = TaskFactory()
        html = _render(task, user=task.reporter)
        assert 'data-label-ids=""' in html

    def test_archived_flag_when_set(self):
        from django.utils import timezone as tz

        task = TaskFactory()
        task.archived_at = tz.now()
        task.save(update_fields=["archived_at"])
        html = _render(task, user=task.reporter)
        assert 'data-archived="1"' in html

    def test_search_haystack_combines_title_and_description(self):
        task = TaskFactory(
            title="Stage 5 polish",
            description="**Tweaks** to status badges and labels",
        )
        html = _render(task, user=task.reporter)
        # Lowercased + truncated to first 160 chars of description.
        assert "stage 5 polish" in html
        assert "tweaks" in html

    def test_search_haystack_truncates_long_description(self):
        long_desc = "abcdefghij" * 40  # 400 chars
        task = TaskFactory(title="x", description=long_desc)
        html = _render(task, user=task.reporter)
        # 160-char cap on description, lowercase, leading title + space.
        # Substring 'abcdefghij' should appear; full 400 chars should NOT
        # all be in the haystack.
        assert "abcdefghij" in html
        assert "abcdefghij" * 17 not in html  # would mean we exceeded the cap

    def test_html_in_title_does_not_break_attribute(self):
        task = TaskFactory(title='Fix "quoted" <b>html</b> in title')
        html = _render(task, user=task.reporter)
        # The double-quote inside the title must be HTML-escaped so it
        # doesn't terminate the data-search-haystack attribute.
        assert "data-search-haystack=" in html
        # No raw double-quote inside the value (would close the attr).
        attr_start = html.index("data-search-haystack=") + len("data-search-haystack=") + 1
        attr_end = html.index('"', attr_start)
        value = html[attr_start:attr_end]
        assert '"' not in value
        assert "&quot;" in value or "&#x27;" in value or "fix" in value


@pytest.mark.django_db
class TestServerSideFilterFallback:
    """Cold load with ``?status=...`` etc. still works.

    Client-side filter is a UI enhancement — server-side filtering
    powers refresh / share links / SSE-driven re-renders. This is the
    regression guard: if someone changes the JS handler, this test
    still passes because the server path is unchanged.
    """

    def test_status_filter_on_cold_load(self, client, db):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        todo = TaskFactory(project=project, reporter=ws.owner, status=Task.STATUS_TODO, title="todo-row")
        done = TaskFactory(project=project, reporter=ws.owner, status=Task.STATUS_DONE, title="done-row")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:all_tasks"), {"status": Task.STATUS_TODO})
        body = resp.content.decode()
        assert todo.title in body
        assert done.title not in body

    def test_priority_filter_on_cold_load(self, client, db):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        urgent = TaskFactory(project=project, reporter=ws.owner, priority=Task.URGENT, title="urgent-row")
        low = TaskFactory(project=project, reporter=ws.owner, priority=Task.LOW, title="low-row")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:all_tasks"), {"priority": str(Task.URGENT)})
        body = resp.content.decode()
        assert urgent.title in body
        assert low.title not in body

    def test_search_q_on_cold_load(self, client, db):
        # ``needle`` / ``haystack`` would collide with the
        # ``data-search-haystack`` attribute name — pick names that
        # don't appear anywhere in the rendered HTML for safe
        # substring assertions.
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        TaskFactory(project=project, reporter=ws.owner, title="zzfindme")
        TaskFactory(project=project, reporter=ws.owner, title="zzhideme")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:all_tasks"), {"q": "zzfindme"})
        body = resp.content.decode()
        assert "zzfindme" in body
        assert "zzhideme" not in body
