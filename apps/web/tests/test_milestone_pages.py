"""The Milestones tab, one milestone's page, and closing one.

A milestone is a point with a scope (ADR 0037). These tests cover the
two pages over that model and the three writes they offer — create /
edit, close with somewhere for the unfinished work to go, and delete —
plus the rule that outlives all of them: deleting a date never deletes
the work that aimed at it.
"""

import datetime

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
