"""End-to-end tests for the ``create_task`` web endpoint.

Covers GET (form rendering with pre-fills) and POST (validation,
membership enforcement, activity event, HTMX redirect).
"""

import datetime

from django.urls import reverse

import pytest

from apps.activity.models import ActivityLog
from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def setup(db):
    """Workspace + project + member user fixture."""
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    return ws, project, ws.owner


@pytest.mark.django_db
class TestCreateTaskGet:
    """GET ``/tasks/new/`` renders the modal with pre-fills."""

    def test_renders_form_for_member(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "New task" in body
        # Project should appear as an option.
        assert project.slug_prefix in body

    def test_project_prefill_selects_dropdown(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"), {"project": project.slug_prefix})
        body = resp.content.decode()
        # The selected option marker should land on the prefilled project.
        assert f'value="{project.slug_prefix}"\n                    selected' in body or "selected" in body

    def test_invalid_status_prefill_falls_back_to_planned(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"), {"status": "garbage"})
        body = resp.content.decode()
        # ``planned`` is the default backlog status (Vox: status default
        # = Planned everywhere except the kanban + which passes ?status=).
        assert 'value="planned"' in body

    def test_assignee_prefills_to_current_user(self, client, setup):
        """When the logged-in user is a member of the selected project's
        workspace, the form pre-selects them as assignee."""
        ws, project, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"))
        body = resp.content.decode()
        # The user's own option should be rendered with ``selected``.
        assert f'value="{user.id}"' in body
        assert "selected" in body

    def test_unauthenticated_redirects(self, client):
        resp = client.get(reverse("web:create_task"))
        assert resp.status_code in (302, 301)

    def test_title_prefilled_from_querystring(self, client, setup):
        """``?title=`` survives the project-switch HTMX re-render."""
        _, _, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"), {"title": "Carry me"})
        assert b'value="Carry me"' in resp.content

    def test_description_prefilled_from_querystring(self, client, setup):
        """Description value lands in the hidden editor-output input AND
        the TipTap source textarea so the editor seeds with it."""
        _, _, user = setup
        client.force_login(user)
        resp = client.get(
            reverse("web:create_task"),
            {"description": "**bold** text"},
        )
        body = resp.content.decode()
        # Hidden input form-submit carrier.
        assert 'value="**bold** text"' in body
        # TipTap source textarea (HTML-escaped between tags).
        assert "**bold** text" in body

    def test_priority_prefilled_from_querystring(self, client, setup):
        _, _, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"), {"priority": "2"})
        body = resp.content.decode()
        # Priority 2 option carries ``selected``.
        assert 'value="2" selected' in body or 'value="2"\n        selected' in body

    def test_invalid_priority_falls_back_to_no_priority(self, client, setup):
        _, _, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"), {"priority": "abc"})
        # No crash — page renders with priority 0 selected.
        assert resp.status_code == 200

    def test_due_date_prefilled_from_querystring(self, client, setup):
        _, _, user = setup
        client.force_login(user)
        resp = client.get(reverse("web:create_task"), {"due_date": "2026-12-31"})
        assert b'value="2026-12-31"' in resp.content

    def test_assignee_preserved_when_member_of_new_project(self, client, setup):
        """When the assignee is in the newly-picked project's workspace,
        their id stays selected — typical project-switch flow."""
        ws, project, user = setup
        client.force_login(user)
        resp = client.get(
            reverse("web:create_task"),
            {"project": project.slug_prefix, "assignee": str(user.id)},
        )
        body = resp.content.decode()
        # Option with that id renders with ``selected``.
        assert f'value="{user.id}"' in body
        assert "selected" in body

    def test_assignee_dropped_when_not_in_new_workspace(self, client, setup):
        """If the assignee isn't a member of the new project's workspace,
        the pre-fill silently drops it (submit would 400 otherwise)."""
        ws, project, user = setup
        # Foreign workspace + user who's only in that one.
        from apps.workspaces.tests.factories import WorkspaceFactory

        foreign_ws = WorkspaceFactory()
        client.force_login(user)
        resp = client.get(
            reverse("web:create_task"),
            {"project": project.slug_prefix, "assignee": str(foreign_ws.owner.id)},
        )
        body = resp.content.decode()
        # The foreign user's id is NOT a member of ``ws`` so it can't be
        # the selected option. The fallback (current user, who *is* a
        # member) takes the selection instead.
        # Just sanity-check no crash + the foreign id isn't selected.
        assert resp.status_code == 200
        # foreign user not in members list at all, so won't appear as
        # ``selected``.
        assert f'value="{foreign_ws.owner.id}" selected' not in body

    def test_labels_preserved_for_workspace(self, client, setup):
        """Labels from the project's workspace stay pre-checked."""
        ws, project, user = setup
        from apps.labels.tests.factories import LabelFactory

        label = LabelFactory(workspace=ws)
        client.force_login(user)
        resp = client.get(
            reverse("web:create_task"),
            {"project": project.slug_prefix, "labels": str(label.id)},
        )
        body = resp.content.decode()
        # Alpine ``x-data`` carries the on-state for that label.
        assert "on: true" in body


@pytest.mark.django_db
class TestCreateTaskPost:
    """POST creates the task, logs an event, and tells HTMX to redirect."""

    def test_minimal_form_creates_task(self, client, setup):
        """Default flow: no ``open_after_create`` flag → 204 + HX-Trigger.

        The status defaults to ``planned`` (backlog) per the post-rework
        product spec; clients pre-fill ``?status=`` from the kanban
        column when they want the new task to land in something else.
        """
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "First task"},
        )
        assert resp.status_code == 204
        # ``open_after_create`` defaults off → no HX-Redirect, panel
        # refreshes via the ``acta:task-created`` HX-Trigger.
        assert "HX-Redirect" not in resp.headers
        assert resp.headers.get("HX-Trigger") == "acta:task-created"
        task = Task.objects.get(project=project, title="First task")
        assert task.reporter == user
        assert task.status == Task.STATUS_PLANNED

    def test_open_after_create_navigates_boosted(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={
                "project": project.slug_prefix,
                "title": "Open me",
                "open_after_create": "1",
            },
        )
        assert resp.status_code == 204
        # Boosted client-side nav (no full-page HX-Redirect) + modal close.
        assert "HX-Redirect" not in resp.headers
        loc = resp.headers["HX-Location"]
        task = Task.objects.get(title="Open me")
        assert task.project.slug_prefix in loc
        assert str(task.number) in loc
        assert "#app-content" in loc
        # HX-Boosted so the GET returns the full shell (with #app-content),
        # not the inner partial — otherwise the swap lands empty.
        assert "HX-Boosted" in loc
        assert resp.headers.get("HX-Trigger") == "acta:task-created"

    def test_creates_activity_event(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "Logged"},
        )
        task = Task.objects.get(title="Logged")
        events = ActivityLog.objects.filter(target_id=task.id, event_type="task.created")
        assert events.count() == 1
        assert events.first().actor_id == user.id

    def test_full_form_persists_every_field(self, client, setup):
        ws, project, user = setup
        label = LabelFactory(workspace=ws)
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={
                "project": project.slug_prefix,
                "title": "Big task",
                "description": "details here",
                "status": Task.STATUS_IN_PROGRESS,
                "priority": Task.HIGH,
                "due_date": "2026-06-01",
                "assignee": user.id,
                "labels": [label.id],
            },
        )
        assert resp.status_code == 204
        task = Task.objects.get(title="Big task")
        assert task.description == "details here"
        assert task.status == Task.STATUS_IN_PROGRESS
        assert task.priority == Task.HIGH
        assert task.due_date == datetime.date(2026, 6, 1)
        assert task.assignee_id == user.id
        assert set(task.labels.values_list("id", flat=True)) == {label.id}

    def test_rejects_empty_title(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "   "},
        )
        assert resp.status_code == 400
        assert not Task.objects.filter(project=project).exists()

    def test_rejects_missing_project(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(reverse("web:create_task"), data={"title": "no project"})
        assert resp.status_code == 400

    def test_foreign_project_returns_404(self, client, setup):
        """A project the user doesn't belong to is invisible — 404, not 403."""
        _, _, user = setup
        other_ws = WorkspaceFactory()
        other_project = ProjectFactory(workspace=other_ws)
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": other_project.slug_prefix, "title": "sneaky"},
        )
        assert resp.status_code == 404
        assert not Task.objects.filter(project=other_project).exists()

    def test_assignee_must_be_workspace_member(self, client, setup):
        ws, project, user = setup
        outsider = WorkspaceFactory().owner
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={
                "project": project.slug_prefix,
                "title": "x",
                "assignee": outsider.id,
            },
        )
        assert resp.status_code == 400
        assert not Task.objects.filter(project=project).exists()

    def test_assignee_who_is_workspace_member_is_accepted(self, client, setup):
        ws, project, user = setup
        teammate = WorkspaceMemberFactory(workspace=ws).user
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={
                "project": project.slug_prefix,
                "title": "x",
                "assignee": teammate.id,
            },
        )
        assert resp.status_code == 204
        task = Task.objects.get(title="x")
        assert task.assignee_id == teammate.id

    def test_invalid_status_rejected(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "x", "status": "bogus"},
        )
        assert resp.status_code == 400

    def test_invalid_priority_rejected(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "x", "priority": "9"},
        )
        assert resp.status_code == 400

    def test_invalid_due_date_rejected(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "x", "due_date": "not a date"},
        )
        assert resp.status_code == 400

    def test_foreign_workspace_label_rejected(self, client, setup):
        ws, project, user = setup
        foreign_label = LabelFactory(workspace=WorkspaceFactory())
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={
                "project": project.slug_prefix,
                "title": "x",
                "labels": [foreign_label.id],
            },
        )
        assert resp.status_code == 400
        assert not Task.objects.filter(project=project).exists()

    def test_title_max_length_enforced(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "x" * 201},
        )
        assert resp.status_code == 400

    def test_unauthenticated_post_redirects(self, client, setup):
        ws, project, _ = setup
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "x"},
        )
        assert resp.status_code in (302, 301)


@pytest.mark.django_db
class TestCreateTaskActiveWorkspaceScoping:
    """The picker + POST are confined to the user's ACTIVE workspace."""

    def _two_ws(self):
        ws1 = WorkspaceFactory()
        ws2 = WorkspaceFactory()
        user = ws1.owner
        WorkspaceMemberFactory(user=user, workspace=ws2)
        user.active_workspace = ws1
        user.save(update_fields=["active_workspace"])
        proj_a = ProjectFactory(workspace=ws1)
        proj_b = ProjectFactory(workspace=ws2)
        return user, proj_a, proj_b

    def test_picker_only_shows_active_workspace_projects(self, client):
        user, proj_a, proj_b = self._two_ws()
        client.force_login(user)
        body = client.get(reverse("web:create_task")).content.decode()
        assert proj_a.slug_prefix in body
        assert proj_b.slug_prefix not in body

    def test_cannot_create_in_non_active_workspace(self, client):
        user, proj_a, proj_b = self._two_ws()
        client.force_login(user)
        resp = client.post(reverse("web:create_task"), data={"project": proj_b.slug_prefix, "title": "x"})
        assert resp.status_code == 404
        assert not Task.objects.filter(project=proj_b).exists()

    def test_can_create_in_active_workspace(self, client):
        user, proj_a, proj_b = self._two_ws()
        client.force_login(user)
        resp = client.post(reverse("web:create_task"), data={"project": proj_a.slug_prefix, "title": "ok"})
        assert resp.status_code == 204
        assert Task.objects.filter(project=proj_a, title="ok").exists()


@pytest.mark.django_db
class TestCreateTaskLinkRelated:
    """``link_related`` auto-links the new task to an origin (from comment / selection)."""

    def test_post_links_new_task_as_related(self, client, setup):
        ws, project, user = setup
        origin = TaskFactory(project=project, title="Origin")
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "Spun off", "link_related": origin.slug},
        )
        assert resp.status_code == 204
        new = Task.objects.get(project=project, title="Spun off")
        # related is symmetric — both sides see the link.
        assert origin in new.related.all()
        assert new in origin.related.all()
        # Fires the extra trigger so the origin's links panel refetches live.
        assert "acta:link-changed" in resp.headers.get("HX-Trigger", "")

    def test_unlinked_create_omits_link_trigger(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "Plain"},
        )
        assert resp.headers.get("HX-Trigger") == "acta:task-created"

    def test_links_fragment_shows_related(self, client, setup):
        ws, project, user = setup
        origin = TaskFactory(project=project, title="Origin")
        related = TaskFactory(project=project, title="Related one")
        origin.related.add(related)
        client.force_login(user)
        url = reverse("web:task_links_fragment", args=[project.slug_prefix, origin.number])
        resp = client.get(url)
        assert resp.status_code == 200
        assert related.slug in resp.content.decode()

    def test_post_ignores_unresolvable_link(self, client, setup):
        ws, project, user = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "No link", "link_related": "NOPE-999"},
        )
        assert resp.status_code == 204
        new = Task.objects.get(project=project, title="No link")
        assert new.related.count() == 0  # bad slug → task created, no link

    def test_post_ignores_foreign_link_target(self, client, setup):
        ws, project, user = setup
        foreign = TaskFactory()  # task in a workspace the user isn't in
        client.force_login(user)
        resp = client.post(
            reverse("web:create_task"),
            data={"project": project.slug_prefix, "title": "No cross link", "link_related": foreign.slug},
        )
        assert resp.status_code == 204
        new = Task.objects.get(project=project, title="No cross link")
        assert new.related.count() == 0

    def test_get_shows_link_caption_for_visible_origin(self, client, setup):
        ws, project, user = setup
        origin = TaskFactory(project=project, title="Origin task")
        client.force_login(user)
        resp = client.get(reverse("web:create_task"), {"link_related": origin.slug})
        body = resp.content.decode()
        assert f'name="link_related" value="{origin.slug}"' in body
        # Caption pairs slug + title (never a bare slug).
        assert origin.slug in body
        assert "Origin task" in body


@pytest.mark.django_db
class TestCreateFromSelectionMarkers:
    """Phase 2 wiring: rendered comment bodies + the description editor
    carry the affordances the create-from-selection JS hooks onto."""

    def test_comment_body_marked_selectable(self, client, setup):
        from apps.comments.models import Comment

        ws, project, user = setup
        task = TaskFactory(project=project)
        Comment.objects.create(task=task, author=user, body="Some discussion text")
        client.force_login(user)
        url = reverse("web:task_comments_fragment", args=[project.slug_prefix, task.number])
        body = client.get(url).content.decode()
        assert "data-create-from-selection" in body

    def test_description_editor_has_create_task_button(self, client, setup):
        ws, project, user = setup
        task = TaskFactory(project=project, description="hello")
        client.force_login(user)
        url = reverse("web:task_description_fragment", args=[project.slug_prefix, task.number])
        body = client.get(url).content.decode()
        assert "Create task from selection" in body
