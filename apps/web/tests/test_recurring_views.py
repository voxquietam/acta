import datetime

from django.urls import reverse

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.recurring.models import RecurringTask
from apps.recurring.tests.factories import RecurringTaskFactory
from apps.tasks.models import Task
from apps.workspaces.tests.factories import WorkspaceFactory

TODAY = datetime.date.today().isoformat()


def _valid_post(project, **overrides):
    """A minimally valid create/edit POST payload."""
    data = {
        "project": project.slug_prefix,
        "title": "Standup notes",
        "description": "",
        "priority": "0",
        "size": "",
        "assignee": "",
        "freq": "weekly",
        "interval": "1",
        "start_date": TODAY,
        "lead_time_days": "0",
        "end_mode": "never",
    }
    data.update(overrides)
    return data


@pytest.fixture
def ws_project(db):
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    return ws, project


@pytest.mark.django_db
def test_list_page_renders(client, ws_project):
    ws, project = ws_project
    client.force_login(ws.owner)
    resp = client.get(reverse("web:recurring_list"))
    assert resp.status_code == 200
    assert b"Recurring tasks" in resp.content


@pytest.mark.django_db
def test_create_rule(client, ws_project):
    ws, project = ws_project
    client.force_login(ws.owner)
    resp = client.post(reverse("web:recurring_new"), _valid_post(project, weekdays=["0", "2"]))
    assert resp.status_code == 204
    assert resp["HX-Trigger"] == "acta:recurring-changed"
    rule = RecurringTask.objects.get(title="Standup notes")
    assert rule.project == project
    assert rule.workspace == ws
    assert rule.freq == "weekly"
    assert rule.weekdays == [0, 2]
    assert rule.created_by == ws.owner
    # Cursor seeded so the materializer can pick it up.
    assert rule.next_occurrence_date is not None


@pytest.mark.django_db
def test_create_requires_title(client, ws_project):
    ws, project = ws_project
    client.force_login(ws.owner)
    resp = client.post(reverse("web:recurring_new"), _valid_post(project, title=""))
    assert resp.status_code == 400
    assert not RecurringTask.objects.exists()


@pytest.mark.django_db
def test_create_rejects_bad_interval(client, ws_project):
    ws, project = ws_project
    client.force_login(ws.owner)
    resp = client.post(reverse("web:recurring_new"), _valid_post(project, interval="0"))
    assert resp.status_code == 400


@pytest.mark.django_db
def test_after_count_needs_max(client, ws_project):
    ws, project = ws_project
    client.force_login(ws.owner)
    resp = client.post(reverse("web:recurring_new"), _valid_post(project, end_mode="after_count"))
    assert resp.status_code == 400


@pytest.mark.django_db
def test_edit_updates_rule(client, ws_project):
    ws, project = ws_project
    rule = RecurringTaskFactory(project=project, workspace=ws, title="Old", freq="daily")
    client.force_login(ws.owner)
    resp = client.post(
        reverse("web:recurring_edit", kwargs={"pk": rule.id}),
        _valid_post(project, title="New", freq="monthly", day_of_month="15"),
    )
    assert resp.status_code == 204
    rule.refresh_from_db()
    assert rule.title == "New"
    assert rule.freq == "monthly"
    assert rule.day_of_month == 15


@pytest.mark.django_db
def test_toggle_pauses_and_resumes(client, ws_project):
    ws, project = ws_project
    rule = RecurringTaskFactory(project=project, workspace=ws)
    client.force_login(ws.owner)
    client.post(reverse("web:recurring_toggle", kwargs={"pk": rule.id}))
    rule.refresh_from_db()
    assert rule.is_active is False
    client.post(reverse("web:recurring_toggle", kwargs={"pk": rule.id}))
    rule.refresh_from_db()
    assert rule.is_active is True
    assert rule.next_occurrence_date is not None


@pytest.mark.django_db
def test_run_now_spawns_task(client, ws_project):
    ws, project = ws_project
    rule = RecurringTaskFactory(project=project, workspace=ws, freq="daily")
    client.force_login(ws.owner)
    resp = client.post(reverse("web:recurring_run_now", kwargs={"pk": rule.id}))
    assert resp.status_code == 204
    assert resp["HX-Trigger"] == "acta:recurring-changed"
    rule.refresh_from_db()
    assert rule.occurrences_created == 1
    assert Task.objects.filter(recurrence=rule).count() == 1


@pytest.mark.django_db
def test_editor_seeds_from_task(client, ws_project):
    from apps.tasks.tests.factories import TaskFactory

    ws, project = ws_project
    task = TaskFactory(project=project, title="Seed me", priority=2)
    client.force_login(ws.owner)
    body = client.get(
        reverse("web:recurring_new"),
        {"from_task": task.slug},
        HTTP_HX_REQUEST="true",
    ).content.decode()
    # Title pre-filled from the task; the modal is still in "new rule" mode.
    assert 'value="Seed me"' in body
    assert "Create rule" in body


@pytest.mark.django_db
def test_context_menu_has_make_recurring(client, ws_project):
    from apps.tasks.tests.factories import TaskFactory

    ws, project = ws_project
    task = TaskFactory(project=project)
    client.force_login(ws.owner)
    menu = client.get(
        reverse("web:task_context_menu", kwargs={"slug_prefix": project.slug_prefix, "number": task.number}),
    ).content.decode()
    assert "Make recurring" in menu
    # ``escapejs`` encodes the slug's hyphen (``-``); the JS decodes it
    # back to the real slug at click time. Assert the param key is wired.
    assert "recurring/new/?from_task=" in menu


@pytest.mark.django_db
def test_recurring_instance_badge_and_marker(client, ws_project):
    from apps.recurring.services import materialize_due

    ws, project = ws_project
    rule = RecurringTaskFactory(project=project, workspace=ws, title="Daily ritual", freq="daily", start_date=TODAY)
    created = materialize_due(datetime.date.fromisoformat(TODAY))
    task = next(t for t in created if t.recurrence_id == rule.id)
    client.force_login(ws.owner)
    detail = client.get(
        reverse("web:task_detail", kwargs={"slug_prefix": project.slug_prefix, "number": task.number}),
    ).content.decode()
    assert "Repeats" in detail
    assert "Edit schedule" in detail
    table = client.get(reverse("web:all_tasks") + "?view=table").content.decode()
    assert "Recurring task" in table  # the marker's title attribute


@pytest.mark.django_db
def test_list_filters(client, ws_project):
    ws, project = ws_project
    other = ProjectFactory(workspace=ws)
    RecurringTaskFactory(project=project, workspace=ws, title="Weekly here", freq="weekly")
    RecurringTaskFactory(project=other, workspace=ws, title="Daily there", freq="daily", is_active=False)
    client.force_login(ws.owner)
    url = reverse("web:recurring_list")
    # by project
    body = client.get(url, {"project": project.slug_prefix}, HTTP_HX_REQUEST="true").content
    assert b"Weekly here" in body and b"Daily there" not in body
    # by state (paused)
    body = client.get(url, {"state": "paused"}, HTTP_HX_REQUEST="true").content
    assert b"Daily there" in body and b"Weekly here" not in body
    # by frequency
    body = client.get(url, {"freq": "weekly"}, HTTP_HX_REQUEST="true").content
    assert b"Weekly here" in body and b"Daily there" not in body


@pytest.mark.django_db
def test_delete_removes_rule_but_keeps_tasks(client, ws_project):
    ws, project = ws_project
    rule = RecurringTaskFactory(project=project, workspace=ws, freq="daily")
    client.force_login(ws.owner)
    client.post(reverse("web:recurring_run_now", kwargs={"pk": rule.id}))
    task = Task.objects.get(recurrence=rule)
    client.post(reverse("web:recurring_delete", kwargs={"pk": rule.id}))
    assert not RecurringTask.objects.filter(pk=rule.id).exists()
    task.refresh_from_db()
    assert task.recurrence_id is None  # SET_NULL — the task survives


@pytest.mark.django_db
def test_cannot_touch_rule_in_foreign_workspace(client, ws_project):
    ws, project = ws_project
    other_ws = WorkspaceFactory()
    other_project = ProjectFactory(workspace=other_ws)
    foreign = RecurringTaskFactory(project=other_project, workspace=other_ws)
    client.force_login(ws.owner)
    assert client.get(reverse("web:recurring_edit", kwargs={"pk": foreign.id})).status_code == 404
    assert client.post(reverse("web:recurring_toggle", kwargs={"pk": foreign.id})).status_code == 404
    assert client.post(reverse("web:recurring_delete", kwargs={"pk": foreign.id})).status_code == 404
