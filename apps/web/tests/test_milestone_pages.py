"""The Milestones tab, one milestone's page, and closing one.

A milestone is a point with a scope (ADR 0037). These tests cover the
two pages over that model and the three writes they offer — create /
edit, close with somewhere for the unfinished work to go, and delete —
plus the rule that outlives all of them: deleting a date never deletes
the work that aimed at it.
"""

import datetime
import json

from django.utils import timezone

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.milestones.models import Milestone
from apps.milestones.tests.factories import MilestoneFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """A workspace, a member, two projects and one shared milestone."""
    workspace = WorkspaceFactory()
    user = UserFactory()
    WorkspaceMember.objects.create(user=user, workspace=workspace)
    backend = ProjectFactory(workspace=workspace, slug_prefix="MBK")
    infra = ProjectFactory(workspace=workspace, slug_prefix="MIN")
    milestone = MilestoneFactory(
        workspace=workspace,
        name="Search beta",
        goal="Beta customers search on the new index",
        projects=[
            backend,
            infra,
        ],
    )
    return workspace, user, backend, infra, milestone


@pytest.mark.django_db
class TestTheList:
    """Every date the workspace aims at, by target date."""

    def test_the_tab_lists_the_workspace_milestones(self, client, setup):
        workspace, user, backend, _, milestone = setup
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/")
        body = resp.content.decode()

        assert resp.status_code == 200
        assert [row["milestone"].pk for row in resp.context["rows"]] == [milestone.pk]
        assert milestone.name in body
        assert milestone.goal in body

    def test_an_empty_workspace_says_what_a_milestone_is(self, client, setup):
        workspace, user, _, _, milestone = setup
        milestone.delete()
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/")

        assert "No milestones yet" in resp.content.decode()

    def test_a_milestone_of_another_workspace_stays_out(self, client, setup):
        workspace, user, _, _, _ = setup
        stranger = ProjectFactory()
        MilestoneFactory(workspace=stranger.workspace, projects=[stranger])
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/")

        assert len(resp.context["rows"]) == 1

    def test_the_page_does_not_grow_a_query_per_row(self, client, setup, django_assert_max_num_queries):
        workspace, user, backend, infra, _ = setup
        for _index in range(6):
            MilestoneFactory(
                workspace=workspace,
                projects=[
                    backend,
                    infra,
                ],
            )
            TaskFactory(project=backend, status=Task.STATUS_TODO)
        client.force_login(user)

        with django_assert_max_num_queries(28):
            client.get(f"/{workspace.slug}/milestones/")


@pytest.mark.django_db
class TestTheDetailPage:
    """Progress, risk, the slices, and the work itself."""

    def test_the_page_renders_the_slices_and_the_work(self, client, setup):
        workspace, user, backend, infra, milestone = setup
        epic = TaskFactory(project=backend, kind=Task.KIND_EPIC, title="Ranking rework")
        TaskFactory(project=backend, milestone=milestone, epic=epic, status=Task.STATUS_DONE)
        TaskFactory(project=infra, milestone=milestone, status=Task.STATUS_IN_PROGRESS, title="Shard the index")
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/{milestone.pk}/")
        body = resp.content.decode()

        assert resp.status_code == 200
        assert (resp.context["done"], resp.context["total"]) == (1, 2)
        assert "Ranking rework" in body
        assert "Shard the index" in body
        assert "MBK" in body and "MIN" in body

    def test_the_at_risk_list_names_the_work_that_will_not_make_it(self, client, setup):
        workspace, user, backend, _, milestone = setup
        TaskFactory(
            project=backend,
            milestone=milestone,
            status=Task.STATUS_TODO,
            title="Refund proration",
            due_date=milestone.target_date + datetime.timedelta(days=6),
        )
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/{milestone.pk}/")

        assert resp.context["risk"] == 1
        assert "+6d" in resp.content.decode()

    def test_a_finished_milestone_offers_to_close(self, client, setup):
        workspace, user, backend, _, milestone = setup
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/{milestone.pk}/")

        assert resp.context["state"] == Milestone.STATE_COMPLETE
        assert "ready to close" in resp.content.decode()

    def test_a_milestone_in_another_workspace_is_a_404(self, client, setup):
        workspace, user, _, _, _ = setup
        stranger = ProjectFactory()
        foreign = MilestoneFactory(workspace=stranger.workspace, projects=[stranger])
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/{foreign.pk}/")

        assert resp.status_code == 404


@pytest.mark.django_db
class TestClosing:
    """Closing is an action, and the unfinished work has to land somewhere."""

    def test_closing_moves_the_open_work_to_another_milestone(self, client, setup):
        workspace, user, backend, _, milestone = setup
        later = MilestoneFactory(
            workspace=workspace,
            target_date=milestone.target_date + datetime.timedelta(days=20),
            projects=[backend],
        )
        done = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
        open_task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        client.force_login(user)

        resp = client.post(
            f"/{workspace.slug}/milestones/{milestone.pk}/close/",
            {"move_to": later.pk},
        )
        milestone.refresh_from_db()
        done.refresh_from_db()
        open_task.refresh_from_db()

        assert resp.status_code == 302
        assert milestone.is_closed
        assert done.milestone_id == milestone.pk
        assert open_task.milestone_id == later.pk

    def test_closing_can_detach_the_open_work_instead(self, client, setup):
        workspace, user, backend, _, milestone = setup
        open_task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        client.force_login(user)

        client.post(f"/{workspace.slug}/milestones/{milestone.pk}/close/", {"move_to": ""})
        open_task.refresh_from_db()

        assert open_task.milestone_id is None
        assert open_task.status == Task.STATUS_TODO

    def test_work_the_target_does_not_cover_stays_where_it_is(self, client, setup):
        workspace, user, backend, infra, milestone = setup
        backend_only = MilestoneFactory(
            workspace=workspace,
            target_date=milestone.target_date + datetime.timedelta(days=20),
            projects=[backend],
        )
        fits = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        outside = TaskFactory(project=infra, milestone=milestone, status=Task.STATUS_TODO)
        client.force_login(user)

        client.post(
            f"/{workspace.slug}/milestones/{milestone.pk}/close/",
            {"move_to": backend_only.pk},
        )
        fits.refresh_from_db()
        outside.refresh_from_db()

        assert fits.milestone_id == backend_only.pk
        assert outside.milestone_id == milestone.pk

    def test_closing_is_recorded_with_who_did_it(self, client, setup):
        workspace, user, backend, _, milestone = setup
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        client.force_login(user)

        client.post(f"/{workspace.slug}/milestones/{milestone.pk}/close/", {"move_to": ""})
        event = ActivityLog.objects.get(event_type="milestone.closed")

        assert event.actor_id == user.pk
        assert event.target_type == ActivityLog.TARGET_MILESTONE
        assert event.target_id == milestone.pk
        assert event.payload["open_work"] == 1

    def test_reopening_undoes_the_close_not_the_move(self, client, setup):
        workspace, user, backend, _, milestone = setup
        moved = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        client.force_login(user)

        client.post(f"/{workspace.slug}/milestones/{milestone.pk}/close/", {"move_to": ""})
        client.post(f"/{workspace.slug}/milestones/{milestone.pk}/reopen/")
        milestone.refresh_from_db()
        moved.refresh_from_db()

        assert not milestone.is_closed
        assert moved.milestone_id is None


@pytest.mark.django_db
class TestTheEditor:
    """Four fields and a scope, and the scope is the part with teeth."""

    def test_creating_a_milestone_lands_on_its_page(self, client, setup):
        workspace, user, backend, _, _ = setup
        client.force_login(user)
        target = timezone.localdate() + datetime.timedelta(days=21)

        resp = client.post(
            f"/{workspace.slug}/milestones/new/",
            {
                "name": "Billing cutover",
                "goal": "Every charge runs on the new provider",
                "description": "",
                "target_date": target.isoformat(),
                "owner": user.pk,
                "projects": [backend.pk],
            },
        )
        created = Milestone.objects.get(name="Billing cutover")

        assert resp.status_code == 204
        assert resp["HX-Redirect"].endswith(f"/milestones/{created.pk}/")
        assert list(created.projects.all()) == [backend]
        assert created.owner_id == user.pk

    def test_a_date_in_the_past_is_refused_with_a_reason(self, client, setup):
        workspace, user, backend, _, _ = setup
        client.force_login(user)

        resp = client.post(
            f"/{workspace.slug}/milestones/new/",
            {
                "name": "Ledger read-only",
                "target_date": (timezone.localdate() - datetime.timedelta(days=2)).isoformat(),
                "projects": [backend.pk],
            },
        )

        assert resp.status_code == 200
        assert "A milestone is a date ahead" in resp.content.decode()
        assert not Milestone.objects.filter(name="Ledger read-only").exists()

    def test_a_milestone_with_no_project_is_refused(self, client, setup):
        workspace, user, _, _, _ = setup
        client.force_login(user)

        resp = client.post(
            f"/{workspace.slug}/milestones/new/",
            {
                "name": "Docs site v1",
                "target_date": (timezone.localdate() + datetime.timedelta(days=10)).isoformat(),
            },
        )

        assert "Pick at least one project" in resp.content.decode()
        assert not Milestone.objects.filter(name="Docs site v1").exists()

    def test_narrowing_the_scope_detaches_the_work_it_drops(self, client, setup):
        workspace, user, backend, infra, milestone = setup
        stays = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        dropped = TaskFactory(project=infra, milestone=milestone, status=Task.STATUS_TODO)
        client.force_login(user)

        client.post(
            f"/{workspace.slug}/milestones/{milestone.pk}/edit/",
            {
                "name": milestone.name,
                "target_date": milestone.target_date.isoformat(),
                "projects": [backend.pk],
                f"scope_action_{infra.slug_prefix}": "detach",
            },
        )
        stays.refresh_from_db()
        dropped.refresh_from_db()

        assert list(milestone.projects.all()) == [backend]
        assert stays.milestone_id == milestone.pk
        assert dropped.milestone_id is None

    def test_narrowing_can_move_the_dropped_work_to_a_milestone_that_covers_it(self, client, setup):
        workspace, user, backend, infra, milestone = setup
        infra_milestone = MilestoneFactory(
            workspace=workspace,
            target_date=milestone.target_date + datetime.timedelta(days=14),
            projects=[infra],
        )
        dropped = TaskFactory(project=infra, milestone=milestone, status=Task.STATUS_TODO)
        client.force_login(user)

        client.post(
            f"/{workspace.slug}/milestones/{milestone.pk}/edit/",
            {
                "name": milestone.name,
                "target_date": milestone.target_date.isoformat(),
                "projects": [backend.pk],
                f"scope_action_{infra.slug_prefix}": "move",
                f"scope_move_{infra.slug_prefix}": infra_milestone.pk,
            },
        )
        dropped.refresh_from_db()

        assert dropped.milestone_id == infra_milestone.pk

    def test_the_editor_carries_the_question_each_untick_would_raise(self, client, setup):
        workspace, user, backend, infra, milestone = setup
        TaskFactory(project=infra, milestone=milestone, status=Task.STATUS_TODO)
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/{milestone.pk}/edit/")
        body = resp.content.decode()
        conflicts = {row["project"].slug_prefix: row for row in resp.context["conflicts"]}

        assert resp.status_code == 200
        assert "Edit milestone" in body
        # Only the project that actually holds work raises a question,
        # and the block is in the page, hidden until its tick comes off.
        assert set(conflicts) == {infra.slug_prefix}
        assert conflicts[infra.slug_prefix]["attached"] == 1
        assert "Leave them unattached" in body


@pytest.mark.django_db
class TestDeleting:
    """The one irreversible action, and the rule that survives it."""

    def test_deleting_keeps_the_work_and_only_drops_the_milestone(self, client, setup):
        workspace, user, backend, _, milestone = setup
        WorkspaceMember.objects.filter(user=user, workspace=workspace).update(role=WorkspaceMember.ADMIN)
        task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        client.force_login(user)

        resp = client.post(f"/{workspace.slug}/milestones/{milestone.pk}/delete/")
        task.refresh_from_db()

        assert resp.status_code == 302
        assert not Milestone.objects.filter(pk=milestone.pk).exists()
        assert task.milestone_id is None
        assert task.status == Task.STATUS_TODO

    def test_a_member_may_not_delete_a_milestone(self, client, setup):
        workspace, user, _, _, milestone = setup
        client.force_login(user)

        resp = client.post(f"/{workspace.slug}/milestones/{milestone.pk}/delete/")

        assert resp.status_code == 403
        assert Milestone.objects.filter(pk=milestone.pk).exists()


@pytest.mark.django_db
class TestFillingFromThePage:
    """The picker on the milestone page, and the one-click attach."""

    def test_the_picker_offers_in_scope_work_and_names_what_it_cannot_take(self, client, setup):
        workspace, user, backend, _, milestone = setup
        outside_project = ProjectFactory(workspace=workspace, slug_prefix="MWB", name="Web")
        TaskFactory(project=backend, status=Task.STATUS_TODO, title="Shard the search index")
        TaskFactory(project=outside_project, status=Task.STATUS_TODO, title="Search results page")
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/{milestone.pk}/add/", {"q": "search"})
        body = resp.content.decode()

        assert resp.status_code == 200
        assert "Shard the search index" in body
        assert "Search results page" in body
        assert "is not in this milestone’s scope" in body

    def test_attaching_commits_the_work_and_says_what_landed(self, client, setup):
        workspace, user, backend, _, milestone = setup
        first = TaskFactory(project=backend, status=Task.STATUS_TODO)
        second = TaskFactory(project=backend, status=Task.STATUS_TODO)
        client.force_login(user)

        resp = client.post(
            f"/{workspace.slug}/milestones/{milestone.pk}/attach/",
            {"task_ids": [first.pk, second.pk]},
        )
        first.refresh_from_db()
        second.refresh_from_db()
        trigger = json.loads(resp["HX-Trigger"])

        assert resp.status_code == 204
        assert first.milestone_id == milestone.pk
        assert second.milestone_id == milestone.pk
        assert trigger["acta:milestone-changed"] is True
        assert trigger["acta:toast"]["message"] == f"2 tasks attached to {milestone.name}"

    def test_work_the_scope_does_not_cover_is_refused(self, client, setup):
        workspace, user, _, _, milestone = setup
        outside_project = ProjectFactory(workspace=workspace, slug_prefix="MWC")
        task = TaskFactory(project=outside_project, status=Task.STATUS_TODO)
        client.force_login(user)

        resp = client.post(
            f"/{workspace.slug}/milestones/{milestone.pk}/attach/",
            {"task_ids": [task.pk]},
        )
        task.refresh_from_db()

        assert resp.status_code == 400
        assert task.milestone_id is None


@pytest.mark.django_db
class TestTheReports:
    """What the page says is missing — and never fixes by itself."""

    def test_the_page_asks_about_work_due_before_the_date(self, client, setup):
        workspace, user, backend, _, milestone = setup
        TaskFactory(
            project=backend,
            status=Task.STATUS_TODO,
            title="Rotate the signing keys",
            due_date=milestone.target_date - datetime.timedelta(days=1),
        )
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/{milestone.pk}/")
        body = resp.content.decode()

        assert [row["task"].title for row in resp.context["reports"]["near"]] == ["Rotate the signing keys"]
        assert "Forgotten, or deliberately out?" in body

    def test_the_page_names_a_blocker_sitting_outside(self, client, setup):
        workspace, user, backend, infra, milestone = setup
        inside = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        blocker = TaskFactory(project=infra, status=Task.STATUS_TODO, title="Size the cluster")
        blocker.blocks.add(inside)
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/{milestone.pk}/")

        assert [row["task"].title for row in resp.context["reports"]["blocks"]] == ["Size the cluster"]
        assert "Size the cluster" in resp.content.decode()

    def test_a_closed_milestone_stops_asking(self, client, setup):
        workspace, user, backend, _, milestone = setup
        TaskFactory(
            project=backend,
            status=Task.STATUS_TODO,
            due_date=milestone.target_date - datetime.timedelta(days=1),
        )
        milestone.closed_at = timezone.now()
        milestone.save()
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/milestones/{milestone.pk}/")

        assert "reports" not in resp.context


@pytest.mark.django_db
class TestCreatingWithAMilestone:
    """The create dialog's default: from the project, never a guess."""

    def _payload(self, body):
        """Return the create dialog's JSON payload from a rendered page."""
        import re

        match = re.search(r'<script id="create-task-data" type="application/json">(.*?)</script>', body, re.S)
        return json.loads(match.group(1))

    def _milestone_field(self, body):
        """Return the dialog's milestone row, or ``None`` when absent."""
        fields = self._payload(body)["fields"]
        return next((field for field in fields if field["key"] == "milestone"), None)

    def test_one_open_milestone_fills_itself_in(self, client, setup):
        workspace, user, backend, _, milestone = setup
        milestone.projects.set([backend])
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/tasks/new/?project={backend.slug_prefix}")
        field = self._milestone_field(resp.content.decode())
        chosen = [option for option in field["options"] if option["on"]]

        assert [option["n"] for option in chosen] == [milestone.name]

    def test_two_open_milestones_guess_nothing(self, client, setup):
        workspace, user, backend, _, milestone = setup
        milestone.projects.set([backend])
        MilestoneFactory(workspace=workspace, name="Later", projects=[backend])
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/tasks/new/?project={backend.slug_prefix}")
        field = self._milestone_field(resp.content.decode())
        chosen = [option for option in field["options"] if option["on"]]

        assert len(field["options"]) == 3
        assert [option["v"] for option in chosen] == [""]

    def test_a_project_no_milestone_covers_gets_no_row(self, client, setup):
        workspace, user, backend, _, milestone = setup
        milestone.projects.set([backend])
        lonely = ProjectFactory(workspace=workspace, slug_prefix="MLN")
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/tasks/new/?project={lonely.slug_prefix}")

        assert self._milestone_field(resp.content.decode()) is None

    def test_an_epic_is_never_offered_one(self, client, setup):
        workspace, user, backend, _, milestone = setup
        workspace.epics_enabled = True
        workspace.save()
        milestone.projects.set([backend])
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/tasks/new/?project={backend.slug_prefix}&kind=epic")

        assert self._milestone_field(resp.content.decode()) is None

    def test_creating_commits_the_task_to_the_chosen_milestone(self, client, setup):
        workspace, user, backend, _, milestone = setup
        client.force_login(user)

        client.post(
            f"/{workspace.slug}/tasks/new/",
            {
                "project": backend.slug_prefix,
                "title": "Rotate the signing keys",
                "status": Task.STATUS_TODO,
                "milestone": milestone.pk,
            },
        )
        task = Task.objects.get(title="Rotate the signing keys")

        assert task.milestone_id == milestone.pk

    def test_a_milestone_that_does_not_cover_the_project_is_refused(self, client, setup):
        workspace, user, _, _, milestone = setup
        lonely = ProjectFactory(workspace=workspace, slug_prefix="MLF")
        client.force_login(user)

        resp = client.post(
            f"/{workspace.slug}/tasks/new/",
            {
                "project": lonely.slug_prefix,
                "title": "Out of scope",
                "status": Task.STATUS_TODO,
                "milestone": milestone.pk,
            },
        )

        assert resp.status_code == 400
        assert not Task.objects.filter(title="Out of scope").exists()


@pytest.mark.django_db
class TestFromAnEpic:
    """An epic stores no milestone — setting one writes onto its tasks."""

    @pytest.fixture
    def epic_setup(self, setup):
        """An epic over work in both projects of the shared milestone."""
        workspace, user, backend, infra, milestone = setup
        workspace.epics_enabled = True
        workspace.save()
        epic = TaskFactory(project=backend, kind=Task.KIND_EPIC, title="Search rework")
        inside = TaskFactory(project=backend, epic=epic, status=Task.STATUS_TODO)
        other = TaskFactory(project=infra, epic=epic, status=Task.STATUS_TODO)
        return workspace, user, backend, infra, milestone, epic, inside, other

    def test_the_page_shows_where_the_work_sits_and_what_can_take_it(self, client, epic_setup):
        workspace, user, backend, _, milestone, epic, inside, _ = epic_setup
        inside.milestone = milestone
        inside.save()
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/projects/{epic.project.slug_prefix}/{epic.number}/")
        rows = {
            (row["milestone"].name if row["milestone"] else None): row["count"]
            for row in resp.context["epic_milestones"]
        }
        targets = {row["milestone"].name: (row["fits"], row["total"]) for row in resp.context["epic_milestone_targets"]}

        assert rows == {milestone.name: 1, None: 1}
        assert targets == {milestone.name: (2, 2)}

    def test_setting_it_writes_onto_every_task_the_scope_covers(self, client, epic_setup):
        workspace, user, backend, _, milestone, epic, inside, other = epic_setup
        milestone.projects.set([backend])
        client.force_login(user)

        resp = client.post(
            f"/{workspace.slug}/projects/{epic.project.slug_prefix}/{epic.number}/epic-milestone/",
            {"milestone_id": milestone.pk},
        )
        inside.refresh_from_db()
        other.refresh_from_db()
        epic.refresh_from_db()
        trigger = json.loads(resp["HX-Trigger"])

        assert resp.status_code == 204
        assert inside.milestone_id == milestone.pk
        assert other.milestone_id is None
        assert epic.milestone_id is None
        assert "1 task committed" in trigger["acta:toast"]["message"]
        assert "1 stays outside its scope" in trigger["acta:toast"]["message"]

    def test_the_epic_can_take_its_work_back_out(self, client, epic_setup):
        workspace, user, _, _, milestone, epic, inside, other = epic_setup
        for task in (inside, other):
            task.milestone = milestone
            task.save()
        client.force_login(user)

        client.post(
            f"/{workspace.slug}/projects/{epic.project.slug_prefix}/{epic.number}/epic-milestone/",
            {"milestone_id": ""},
        )
        inside.refresh_from_db()
        other.refresh_from_db()

        assert inside.milestone_id is None
        assert other.milestone_id is None
