"""An epic is a task that collects other tasks.

Everything here is about the two things that make it one: the rules a
task has to satisfy to be collected, and the state an epic reads off the
tasks it collects. See docs/decisions/0036-epics.md.
"""

import datetime

from django.core.exceptions import ValidationError

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def workspace(db):
    """A workspace with epics turned on (the default)."""
    return WorkspaceFactory()


@pytest.fixture
def project(workspace):
    """A project in that workspace."""
    return ProjectFactory(workspace=workspace)


@pytest.fixture
def epic(project):
    """An empty epic."""
    return TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED)


def member(epic, project, status=Task.STATUS_TODO, **kwargs):
    """Create a task collected by ``epic``."""
    return TaskFactory(project=project, epic=epic, status=status, **kwargs)


@pytest.mark.django_db
class TestWhatMayBeCollected:

    def test_a_task_joins_an_epic(self, epic, project):
        task = member(epic, project)
        task.full_clean(exclude=["number"])
        assert list(epic.epic_tasks.all()) == [task]

    def test_an_epic_reaches_across_projects(self, epic, workspace):
        # The whole point: a parent must share its child's project, an
        # epic must not.
        elsewhere = ProjectFactory(workspace=workspace, slug_prefix="OTH")
        task = TaskFactory(project=elsewhere, epic=epic)
        task.full_clean(exclude=["number"])
        assert task.epic_id == epic.pk

    def test_an_epic_never_reaches_across_workspaces(self, epic):
        foreign = ProjectFactory(workspace=WorkspaceFactory(), slug_prefix="FGN")
        task = TaskFactory(project=foreign, epic=epic)
        with pytest.raises(ValidationError, match="same workspace"):
            task.full_clean(exclude=["number"])

    def test_a_subtask_keeps_its_parent_and_joins_an_epic(self, epic, project):
        # The reason epics got their own field: with one hierarchy field
        # this task could be a subtask or be collected, never both.
        parent = TaskFactory(project=project)
        task = TaskFactory(project=project, parent=parent, epic=epic)
        task.full_clean(exclude=["number"])
        assert task.parent_id == parent.pk
        assert task.epic_id == epic.pk

    def test_an_epic_cannot_belong_to_an_epic(self, epic, project):
        other = TaskFactory(project=project, kind=Task.KIND_EPIC, epic=epic, status=Task.STATUS_PLANNED)
        with pytest.raises(ValidationError, match="cannot belong to another epic"):
            other.full_clean(exclude=["number"])

    def test_a_plain_task_cannot_collect(self, project):
        not_an_epic = TaskFactory(project=project)
        task = TaskFactory(project=project, epic=not_an_epic)
        with pytest.raises(ValidationError, match="only be collected by an epic"):
            task.full_clean(exclude=["number"])

    def test_an_epic_is_never_a_subtask(self, project):
        parent = TaskFactory(project=project)
        bad = TaskFactory(project=project, kind=Task.KIND_EPIC, parent=parent, status=Task.STATUS_PLANNED)
        with pytest.raises(ValidationError, match="cannot be a subtask"):
            bad.full_clean(exclude=["number"])

    def test_an_unknown_kind_is_rejected(self, project):
        # Short enough to reach ``clean()`` — the column itself stops
        # anything longer first.
        bad = TaskFactory(project=project, kind="story")
        with pytest.raises(ValidationError, match="Unknown kind"):
            bad.full_clean(exclude=["number"])

    @pytest.mark.parametrize(
        "field,value",
        [
            ("due_date", datetime.date(2026, 10, 9)),
            ("size", 5),
        ],
    )
    def test_an_epic_carries_none_of_the_fields_it_derives(self, project, field, value):
        bad = TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED, **{field: value})
        with pytest.raises(ValidationError, match="from its tasks"):
            bad.full_clean(exclude=["number"])


@pytest.mark.django_db
class TestTheWorkspaceToggle:

    def test_off_refuses_a_new_epic(self, workspace, project):
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        bad = TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED)
        with pytest.raises(ValidationError, match="turned off"):
            bad.full_clean(exclude=["number"])

    def test_off_refuses_collecting_into_an_existing_one(self, workspace, project, epic):
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        task = TaskFactory(project=project, epic=epic)
        with pytest.raises(ValidationError, match="turned off"):
            task.full_clean(exclude=["number"])

    def test_off_keeps_what_already_exists(self, workspace, project, epic):
        task = member(epic, project)
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        # Turning the feature off hides it; it does not unpick the work.
        assert list(epic.epic_members()) == [task]

    def test_on_by_default(self, workspace):
        assert workspace.epics_enabled is True


@pytest.mark.django_db
class TestWhatAnEpicReadsOffItsTasks:

    def test_an_empty_epic_is_planned_and_zero(self, epic):
        assert epic.epic_counts == (0, 0)
        assert epic.epic_status == Task.STATUS_PLANNED

    def test_counts_are_done_over_total(self, epic, project):
        member(epic, project, Task.STATUS_DONE)
        member(epic, project, Task.STATUS_DONE)
        member(epic, project, Task.STATUS_IN_PROGRESS)
        assert epic.epic_counts == (2, 3)

    def test_cancelled_and_archived_tasks_leave_the_count(self, epic, project):
        from django.utils import timezone

        member(epic, project, Task.STATUS_DONE)
        member(epic, project, Task.STATUS_CANCELLED)
        member(epic, project, archived_at=timezone.now())
        # Neither is work any more, and counting them would hold the
        # epic's progress down forever.
        assert epic.epic_counts == (1, 1)

    @pytest.mark.parametrize(
        "statuses,expected",
        [
            ([Task.STATUS_DONE, Task.STATUS_DONE], Task.STATUS_DONE),
            ([Task.STATUS_DONE, Task.STATUS_TODO], Task.STATUS_IN_PROGRESS),
            ([Task.STATUS_IN_REVIEW, Task.STATUS_PLANNED], Task.STATUS_IN_PROGRESS),
            ([Task.STATUS_TODO, Task.STATUS_PLANNED], Task.STATUS_TODO),
            ([Task.STATUS_PLANNED, Task.STATUS_READY], Task.STATUS_PLANNED),
        ],
    )
    def test_the_status_ladder(self, epic, project, statuses, expected):
        for status in statuses:
            member(epic, project, status)
        assert epic.epic_status == expected

    def test_the_span_is_the_widest_reach_of_its_tasks(self, epic, project):
        member(epic, project, start_date=datetime.date(2026, 9, 15), due_date=datetime.date(2026, 10, 1))
        member(epic, project, start_date=datetime.date(2026, 9, 20), due_date=datetime.date(2026, 11, 14))
        assert epic.epic_span == (datetime.date(2026, 9, 15), datetime.date(2026, 11, 14))

    def test_a_span_with_no_dates_is_empty(self, epic, project):
        member(epic, project)
        assert epic.epic_span == (None, None)

    def test_a_plain_task_collects_nothing(self, project):
        assert TaskFactory(project=project).epic_counts == (0, 0)

    def test_losing_the_epic_leaves_the_task_alone(self, epic, project):
        task = member(epic, project)
        epic.delete()
        task.refresh_from_db()
        # SET_NULL, not CASCADE: deleting the umbrella must not delete
        # the work it was over.
        assert task.epic_id is None
        assert Task.objects.filter(pk=task.pk).exists()


@pytest.mark.django_db
class TestTheApiEnforcesTheSameRules:
    """The REST layer repeats the model's rules rather than trusting it.

    ``Model.clean()`` is not called on ``save()``, so the serializer is
    where an API client actually meets them.
    """

    @pytest.fixture
    def api(self, workspace):
        """An authenticated API client whose user owns the workspace."""
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=workspace.owner)
        return client

    def post(self, api, project, **extra):
        """POST a task payload to the API."""
        payload = {"project": project.id, "title": "From the API"}
        payload.update(extra)
        return api.post("/api/v1/tasks/", payload, format="json")

    def test_a_task_can_be_collected(self, api, project, epic):
        resp = self.post(api, project, epic=epic.id)
        assert resp.status_code == 201, resp.data
        assert Task.objects.get(pk=resp.data["id"]).epic_id == epic.pk

    def test_an_epic_can_be_created(self, api, project):
        resp = self.post(api, project, kind=Task.KIND_EPIC)
        assert resp.status_code == 201, resp.data
        assert Task.objects.get(pk=resp.data["id"]).kind == Task.KIND_EPIC

    def test_a_plain_task_cannot_collect(self, api, project):
        resp = self.post(api, project, epic=TaskFactory(project=project).id)
        assert resp.status_code == 400
        assert "epic" in resp.data

    def test_an_epic_from_another_workspace_is_refused(self, api, project):
        foreign = ProjectFactory(workspace=WorkspaceFactory(), slug_prefix="FGN")
        foreign_epic = TaskFactory(project=foreign, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED)
        resp = self.post(api, project, epic=foreign_epic.id)
        assert resp.status_code == 400
        assert "epic" in resp.data

    def test_an_epic_cannot_be_given_a_deadline(self, api, project):
        resp = self.post(api, project, kind=Task.KIND_EPIC, due_date="2026-10-09")
        assert resp.status_code == 400
        assert "due_date" in resp.data

    def test_the_switch_is_honoured(self, api, workspace, project):
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        resp = self.post(api, project, kind=Task.KIND_EPIC)
        assert resp.status_code == 400
        assert "kind" in resp.data


@pytest.mark.django_db
class TestEpicsNeverJoinACycle:
    """The cadence policy writes without validating, so it has to know.

    ``set_task_status`` saves directly and the bulk path uses
    ``QuerySet.update()``; neither calls ``full_clean()``. Without a
    guard an epic would silently acquire a cycle and then count toward
    the burndown, the velocity and the cycle summary.
    """

    @pytest.fixture
    def cadence(self, workspace):
        """Turn the workspace's cadence on with an anchored start."""
        workspace.cycle_settings = {"enabled": True, "length_weeks": 2, "start_date": "2026-09-01"}
        workspace.save(update_fields=["cycle_settings"])
        return workspace

    def test_the_in_memory_policy_skips_an_epic(self, cadence, project, epic):
        from apps.cycles.services import apply_cycle_policy

        epic.status = Task.STATUS_IN_PROGRESS
        assert apply_cycle_policy(epic) is False
        assert epic.cycle_id is None

    def test_the_policy_still_applies_to_real_work(self, cadence, project):
        from apps.cycles.services import apply_cycle_policy

        task = TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS)
        assert apply_cycle_policy(task) is True
        assert task.cycle_id is not None

    def test_the_bulk_path_skips_an_epic(self, cadence, project, epic):
        from apps.tasks.bulk import _bulk_apply_cycle_policy

        task = TaskFactory(project=project)
        _bulk_apply_cycle_policy([epic.pk, task.pk], Task.STATUS_TODO)
        epic.refresh_from_db()
        task.refresh_from_db()
        assert epic.cycle_id is None
        assert task.cycle_id is not None


@pytest.mark.django_db
class TestEpicsAreAbsentWhereWorkIsCounted:
    """The sweep, asserted per family rather than per call site.

    An epic is a row in the same table as the work, so every surface
    that counts, lists or boards tasks had to learn about it. A forgotten
    one does not crash — it returns a number one too big — which is
    exactly the failure a test has to catch.
    """

    @pytest.fixture
    def seeded(self, db):
        """A workspace with one plain task and one epic over it."""
        from apps.accounts.tests.factories import UserFactory
        from apps.workspaces.models import WorkspaceMember

        workspace = WorkspaceFactory()
        user = UserFactory()
        WorkspaceMember.objects.create(user=user, workspace=workspace)
        project = ProjectFactory(workspace=workspace, slug_prefix="EPC")
        epic = TaskFactory(
            project=project,
            kind=Task.KIND_EPIC,
            status=Task.STATUS_PLANNED,
            title="Billing migration",
            assignee=user,
        )
        task = TaskFactory(project=project, title="Dual-write charges", assignee=user, epic=epic)
        return workspace, project, user, epic, task

    def test_all_tasks_lists_the_work_and_not_the_epic(self, client, seeded):
        workspace, _, user, epic, task = seeded
        client.force_login(user)
        body = client.get(f"/{workspace.slug}/tasks/").content.decode()
        assert task.title in body
        assert epic.title not in body

    def test_the_project_board_leaves_the_epic_out(self, client, seeded):
        workspace, project, user, epic, task = seeded
        client.force_login(user)
        body = client.get(f"/{workspace.slug}/projects/{project.slug_prefix}/").content.decode()
        assert task.title in body
        assert epic.title not in body

    def test_my_work_leaves_the_epic_out(self, client, seeded):
        workspace, _, user, epic, task = seeded
        client.force_login(user)
        body = client.get(f"/{workspace.slug}/my-work/").content.decode()
        assert task.title in body
        assert epic.title not in body

    def test_the_export_leaves_the_epic_out(self, client, seeded):
        import json

        workspace, _, user, epic, task = seeded
        client.force_login(user)
        payload = json.loads(client.get(f"/{workspace.slug}/tasks/export.json").content)
        titles = [row["title"] for row in payload["tasks"]]
        assert task.title in titles
        assert epic.title not in titles

    def test_project_stats_count_the_work_only(self, seeded):
        import datetime

        from django.utils import timezone

        from apps.projects.stats import compute_update_stats

        _, project, _, _, _ = seeded
        stats = compute_update_stats(project, timezone.now() - datetime.timedelta(days=7))
        # The seeded task is to-do and the epic is planned; without the
        # exclusion the epic would show up in the planned counter.
        assert stats["planned"] == 0

    def test_the_dashboard_counts_the_work_only(self, seeded):
        from apps.web.dashboard import build_dashboard_context

        workspace, _, user, _, _ = seeded
        ctx = build_dashboard_context(workspace=workspace, user=user)
        assert ctx["dash_open_total"] == 1

    def test_the_link_picker_never_offers_an_epic(self, client, seeded):
        workspace, project, user, epic, task = seeded
        client.force_login(user)
        url = f"/projects/{project.slug_prefix}/{task.number}/links/search/"
        rows = client.get(url, {"q": "Billing"}).json()["results"]
        # Membership has its own field; offering the epic as a link
        # target would be a second way to say one relationship.
        assert [r["slug"] for r in rows] == []

    def test_the_parent_picker_never_offers_an_epic(self, client, seeded):
        workspace, project, user, epic, _ = seeded
        client.force_login(user)
        rows = client.get("/tasks/new/search/", {"project": project.slug_prefix, "kind": "parent", "q": "Billing"})
        assert rows.json()["results"] == []

    def test_the_epic_itself_still_opens(self, client, seeded):
        workspace, project, user, epic, _ = seeded
        client.force_login(user)
        resp = client.get(f"/{workspace.slug}/projects/{project.slug_prefix}/{epic.number}/")
        # Hidden from the boards is not hidden from the app: every link,
        # mention and menu resolves an epic like any other task.
        assert resp.status_code == 200
        assert epic.title in resp.content.decode()


@pytest.mark.django_db
class TestTheApiAndMcpHideEpicsUnlessAsked:
    """Agents and API clients get the work by default, epics on request.

    Silently listing epics alongside tasks would make every "count the
    tasks" answer one too big; silently hiding them with no way in would
    make an epic unreachable. Both surfaces take an explicit ``kind``.
    """

    @pytest.fixture
    def seeded(self, workspace, project, epic):
        """A plain task beside the epic, with the epic over it."""
        return epic, TaskFactory(project=project, title="Dual-write charges", epic=epic)

    def test_the_rest_list_leaves_epics_out(self, workspace, seeded):
        from rest_framework.test import APIClient

        epic, task = seeded
        api = APIClient()
        api.force_authenticate(user=workspace.owner)
        slugs = [row["slug"] for row in api.get("/api/v1/tasks/").data["results"]]
        assert task.slug in slugs
        assert epic.slug not in slugs

    def test_rest_kind_epic_returns_only_epics(self, workspace, seeded):
        from rest_framework.test import APIClient

        epic, task = seeded
        api = APIClient()
        api.force_authenticate(user=workspace.owner)
        slugs = [row["slug"] for row in api.get("/api/v1/tasks/", {"kind": "epic"}).data["results"]]
        assert slugs == [epic.slug]

    def test_rest_kind_all_returns_both(self, workspace, seeded):
        from rest_framework.test import APIClient

        epic, task = seeded
        api = APIClient()
        api.force_authenticate(user=workspace.owner)
        slugs = {row["slug"] for row in api.get("/api/v1/tasks/", {"kind": "all"}).data["results"]}
        assert slugs == {epic.slug, task.slug}

    def test_an_epic_still_fetches_by_id(self, workspace, seeded):
        from rest_framework.test import APIClient

        epic, _ = seeded
        api = APIClient()
        api.force_authenticate(user=workspace.owner)
        # Hiding it from the list must not make it unreachable — a client
        # holding the id keeps working.
        assert api.get(f"/api/v1/tasks/{epic.pk}/").status_code == 200

    def test_the_mcp_list_leaves_epics_out(self, workspace, seeded):
        from apps.mcp.tools.read import tasks_list

        epic, task = seeded
        slugs = [row["slug"] for row in tasks_list(workspace.owner, {})]
        assert task.slug in slugs
        assert epic.slug not in slugs

    def test_the_mcp_list_reports_the_rollup_on_an_epic(self, workspace, project, seeded):
        from apps.mcp.tools.read import tasks_list

        epic, _ = seeded
        member(epic, project, Task.STATUS_DONE)
        row = next(r for r in tasks_list(workspace.owner, {"kind": "epic"}) if r["slug"] == epic.slug)
        assert row["kind"] == Task.KIND_EPIC
        assert row["progress"] == {"done": 1, "total": 2}
        # Computed, not the stored column the row was created with.
        assert row["status"] == Task.STATUS_IN_PROGRESS

    def test_the_mcp_list_can_narrow_to_one_epic(self, workspace, project, seeded):
        from apps.mcp.tools.read import tasks_list

        epic, task = seeded
        TaskFactory(project=project, title="Unrelated")
        slugs = [row["slug"] for row in tasks_list(workspace.owner, {"epic": epic.slug})]
        assert slugs == [task.slug]

    def test_mcp_task_get_returns_the_epic_with_its_tasks(self, workspace, seeded):
        from apps.mcp.tools.read import task_get

        epic, task = seeded
        payload = task_get(workspace.owner, {"slug": epic.slug})
        assert payload["kind"] == Task.KIND_EPIC
        assert [t["slug"] for t in payload["epic"]["tasks"]] == [task.slug]
        assert payload["epic"]["total"] == 1

    def test_mcp_create_can_collect_into_an_epic(self, workspace, project, epic):
        from apps.mcp.tools.write import task_create

        task_create(workspace.owner, {"project": project.slug_prefix, "title": "New", "epic_slug": epic.slug})
        assert Task.objects.get(title="New").epic_id == epic.pk

    def test_mcp_update_moves_a_task_out_of_its_epic(self, workspace, project, seeded):
        from apps.mcp.tools.write import task_update

        epic, task = seeded
        task_update(workspace.owner, {"slug": task.slug, "epic_slug": None})
        task.refresh_from_db()
        assert task.epic_id is None


@pytest.mark.django_db
class TestProgressSurvivesTheArchive:
    """Finished work keeps counting after the auto-archive job files it.

    The daily ``archive_stale_done_tasks`` job archives done tasks, and
    while archived work was excluded outright, an epic's progress walked
    backwards with nothing having happened — 1/3 became 0/2, and a
    finished epic eventually read 0/0, which says "no tasks yet". What
    an archive means is "filed away", not "never happened".
    """

    def _epic(self, project):
        return TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED)

    def test_an_archived_done_task_still_counts_on_both_sides(self, db):
        from django.utils import timezone

        project = ProjectFactory()
        epic = self._epic(project)
        TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO)
        TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO)
        TaskFactory(project=project, epic=epic, status=Task.STATUS_DONE, archived_at=timezone.now())
        assert epic.epic_counts == (1, 3)

    def test_archiving_a_finished_task_does_not_move_the_counter(self, db):
        from django.utils import timezone

        project = ProjectFactory()
        epic = self._epic(project)
        done = TaskFactory(project=project, epic=epic, status=Task.STATUS_DONE)
        TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO)
        before = epic.epic_counts
        done.archived_at = timezone.now()
        done.save(update_fields=["archived_at"])
        assert epic.epic_counts == before == (1, 2)

    def test_a_fully_archived_epic_reads_done_not_empty(self, db):
        from django.utils import timezone

        project = ProjectFactory()
        epic = self._epic(project)
        for _ in range(2):
            TaskFactory(project=project, epic=epic, status=Task.STATUS_DONE, archived_at=timezone.now())
        assert epic.epic_counts == (2, 2)
        assert epic.epic_status == Task.STATUS_DONE

    def test_archived_but_unfinished_work_stays_out(self, db):
        """Shelved, not done — counting it holds progress down forever."""
        from django.utils import timezone

        project = ProjectFactory()
        epic = self._epic(project)
        TaskFactory(project=project, epic=epic, status=Task.STATUS_DONE)
        TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO, archived_at=timezone.now())
        assert epic.epic_counts == (1, 1)

    def test_cancelled_work_counts_nowhere(self, db):
        project = ProjectFactory()
        epic = self._epic(project)
        TaskFactory(project=project, epic=epic, status=Task.STATUS_DONE)
        TaskFactory(project=project, epic=epic, status=Task.STATUS_CANCELLED)
        assert epic.epic_counts == (1, 1)

    def test_the_board_still_lists_only_live_work(self, db):
        """Counted is not listed: an archived card must not come back."""
        from django.utils import timezone

        project = ProjectFactory()
        epic = self._epic(project)
        live = TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO)
        TaskFactory(project=project, epic=epic, status=Task.STATUS_DONE, archived_at=timezone.now())
        assert [t.pk for t in epic.epic_members()] == [live.pk]
        assert epic.epic_counted().count() == 2

    def test_the_annotation_agrees_with_the_property(self, db):
        """The tab annotates; a single epic uses the property. One rule."""
        from django.utils import timezone

        project = ProjectFactory()
        epic = self._epic(project)
        TaskFactory(project=project, epic=epic, status=Task.STATUS_DONE, archived_at=timezone.now())
        TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO)
        row = Task.objects.epics().with_epic_rollup().get(pk=epic.pk)
        assert (row.member_done, row.member_total) == epic.epic_counts == (1, 2)
