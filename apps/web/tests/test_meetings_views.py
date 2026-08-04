import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.comments.models import Comment
from apps.meetings.models import Meeting
from apps.meetings.tests.factories import MeetingFactory
from apps.notifications.models import Notification
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


def _valid_post(**overrides):
    """A minimally valid create/edit POST payload for a meeting."""
    data = {
        "title": "Client sync",
        "happened_at": "2026-06-01T14:00",
        "duration_minutes": "45",
        "project": "",
        "notes": "",
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
    ws, _ = ws_project
    client.force_login(ws.owner)
    resp = client.get(reverse("web:calls_list"))
    assert resp.status_code == 200
    assert b"Calls" in resp.content


@pytest.mark.django_db
def test_create_modal_renders(client, ws_project):
    ws, project = ws_project
    task = TaskFactory(project=project, title="Prefill me")
    client.force_login(ws.owner)
    resp = client.get(reverse("web:create_call"), {"task": str(task.id)})
    assert resp.status_code == 200
    assert b"Log a call" in resp.content
    # The ?task= prefill seeds the picker's selected-task list (the title is
    # rendered into the Alpine state; the slug is there too but escapejs
    # encodes its hyphen, so assert on the unambiguous title).
    assert b"Prefill me" in resp.content


@pytest.mark.django_db
def test_edit_modal_renders(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project, title="Retro")
    client.force_login(ws.owner)
    resp = client.get(reverse("web:edit_call", kwargs={"pk": meeting.id}))
    assert resp.status_code == 200
    assert b"Edit call" in resp.content
    assert b"Retro" in resp.content


@pytest.mark.django_db
def test_create_call_notifies_participants(client, ws_project):
    ws, project = ws_project
    member = UserFactory()
    WorkspaceMemberFactory(user=member, workspace=ws)
    client.force_login(ws.owner)
    resp = client.post(
        reverse("web:create_call"),
        _valid_post(participants=[str(member.id), str(ws.owner.id)]),
    )
    assert resp.status_code == 204
    meeting = Meeting.objects.get(title="Client sync")
    # The participant who isn't the actor gets a MEETING notification…
    note = Notification.objects.get(recipient=member, kind=Notification.Kind.MEETING)
    assert note.meeting == meeting
    assert note.actor == ws.owner
    # …and the actor (also a participant) is self-suppressed.
    assert not Notification.objects.filter(recipient=ws.owner, kind=Notification.Kind.MEETING).exists()


@pytest.mark.django_db
def test_edit_call_notifies_only_new_participants(client, ws_project):
    ws, project = ws_project
    a = UserFactory()
    b = UserFactory()
    WorkspaceMemberFactory(user=a, workspace=ws)
    WorkspaceMemberFactory(user=b, workspace=ws)
    meeting = MeetingFactory(workspace=ws, project=project)
    meeting.participants.add(a)
    client.force_login(ws.owner)
    # Edit: keep a, add b. Only b is newly added → only b is notified.
    client.post(
        reverse("web:edit_call", kwargs={"pk": meeting.id}),
        _valid_post(participants=[str(a.id), str(b.id)]),
    )
    assert Notification.objects.filter(recipient=b, kind=Notification.Kind.MEETING).exists()
    assert not Notification.objects.filter(recipient=a, kind=Notification.Kind.MEETING).exists()


@pytest.mark.django_db
def test_my_work_shows_upcoming_and_recent_calls(client, ws_project):
    ws, project = ws_project
    upcoming = MeetingFactory(
        workspace=ws,
        project=project,
        title="Future planning",
        happened_at=timezone.now() + datetime.timedelta(days=2),
    )
    past = MeetingFactory(
        workspace=ws,
        project=project,
        title="Past retro",
        happened_at=timezone.now() - datetime.timedelta(days=2),
    )
    upcoming.participants.add(ws.owner)
    past.participants.add(ws.owner)
    client.force_login(ws.owner)
    resp = client.get(reverse("web:my_work"))
    assert resp.status_code == 200
    assert b"Upcoming calls" in resp.content
    assert b"Future planning" in resp.content
    assert b"Recent calls" in resp.content
    assert b"Past retro" in resp.content


@pytest.mark.django_db
def test_call_detail_renders(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project, title="Quarterly review")
    client.force_login(ws.owner)
    resp = client.get(reverse("web:call_detail", kwargs={"pk": meeting.id}))
    assert resp.status_code == 200
    assert b"Quarterly review" in resp.content
    # Copy-link points at this meeting's canonical page.
    assert reverse("web:call_detail", kwargs={"pk": meeting.id}).encode() in resp.content


@pytest.mark.django_db
def test_edit_modal_exposes_fullscreen_and_copy(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project)
    client.force_login(ws.owner)
    detail_url = reverse("web:call_detail", kwargs={"pk": meeting.id}).encode()
    edit = client.get(reverse("web:edit_call", kwargs={"pk": meeting.id}))
    assert detail_url in edit.content
    # The create modal has no meeting yet, so no fullscreen / copy affordance.
    create = client.get(reverse("web:create_call"))
    assert detail_url not in create.content


@pytest.mark.django_db
def test_create_call(client, ws_project):
    ws, project = ws_project
    client.force_login(ws.owner)
    resp = client.post(reverse("web:create_call"), _valid_post(project=project.slug_prefix))
    assert resp.status_code == 204
    assert resp["HX-Trigger"] == "acta:meeting-changed"
    meeting = Meeting.objects.get(title="Client sync")
    assert meeting.workspace == ws
    assert meeting.project == project
    assert meeting.duration_minutes == 45
    assert meeting.created_by == ws.owner


@pytest.mark.django_db
def test_create_call_links_task_and_participant(client, ws_project):
    ws, project = ws_project
    task = TaskFactory(project=project)
    client.force_login(ws.owner)
    resp = client.post(
        reverse("web:create_call"),
        _valid_post(tasks=[str(task.id)], participants=[str(ws.owner.id)]),
    )
    assert resp.status_code == 204
    meeting = Meeting.objects.get(title="Client sync")
    assert list(meeting.tasks.all()) == [task]
    assert list(meeting.participants.all()) == [ws.owner]


@pytest.mark.django_db
def test_create_call_logs_activity(client, ws_project):
    ws, _ = ws_project
    client.force_login(ws.owner)
    client.post(reverse("web:create_call"), _valid_post())
    meeting = Meeting.objects.get(title="Client sync")
    event = ActivityLog.objects.get(target_type=ActivityLog.TARGET_MEETING, target_id=meeting.id)
    assert event.event_type == "meeting.created"
    assert event.actor == ws.owner
    assert event.payload["duration_minutes"] == 45


@pytest.mark.django_db
def test_create_call_rejects_bad_duration(client, ws_project):
    ws, _ = ws_project
    client.force_login(ws.owner)
    resp = client.post(reverse("web:create_call"), _valid_post(duration_minutes="0"))
    assert resp.status_code == 400
    assert not Meeting.objects.exists()


@pytest.mark.django_db
def test_create_call_rejects_foreign_task(client, ws_project):
    ws, _ = ws_project
    other_task = TaskFactory()  # task in a different workspace
    client.force_login(ws.owner)
    resp = client.post(reverse("web:create_call"), _valid_post(tasks=[str(other_task.id)]))
    assert resp.status_code == 400
    assert not Meeting.objects.exists()


@pytest.mark.django_db
def test_edit_call(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project, title="Old")
    client.force_login(ws.owner)
    resp = client.post(
        reverse("web:edit_call", kwargs={"pk": meeting.id}),
        _valid_post(title="New title", duration_minutes="90"),
    )
    assert resp.status_code == 204
    meeting.refresh_from_db()
    assert meeting.title == "New title"
    assert meeting.duration_minutes == 90


@pytest.mark.django_db
def test_delete_call(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project)
    client.force_login(ws.owner)
    resp = client.post(reverse("web:delete_call", kwargs={"pk": meeting.id}))
    assert resp.status_code == 204
    assert resp["HX-Trigger"] == "acta:meeting-changed"
    assert not Meeting.objects.filter(pk=meeting.id).exists()
    assert ActivityLog.objects.filter(
        target_type=ActivityLog.TARGET_MEETING,
        target_id=meeting.id,
        event_type="meeting.deleted",
    ).exists()


@pytest.mark.django_db
def test_task_meetings_fragment_shows_rollup(client, ws_project):
    ws, project = ws_project
    task = TaskFactory(project=project)
    m1 = MeetingFactory(workspace=ws, project=project, duration_minutes=30)
    m2 = MeetingFactory(workspace=ws, project=project, duration_minutes=15)
    m1.tasks.add(task)
    m2.tasks.add(task)
    client.force_login(ws.owner)
    resp = client.get(
        reverse(
            "web:task_meetings_fragment",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )
    )
    assert resp.status_code == 200
    # Rollup total = 30 + 15 = 45 minutes across the two linked calls.
    assert b"45m" in resp.content


@pytest.mark.django_db
def test_meetings_panel_present_on_task_rail(client, ws_project):
    ws, project = ws_project
    task = TaskFactory(project=project)
    client.force_login(ws.owner)
    resp = client.get(
        reverse(
            "web:task_meta_fragment",
            kwargs={"slug_prefix": project.slug_prefix, "number": task.number},
        )
    )
    assert resp.status_code == 200
    assert b'id="task-meetings"' in resp.content


@pytest.mark.django_db
def test_post_meeting_comment(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project)
    client.force_login(ws.owner)
    resp = client.post(reverse("web:post_meeting_comment", kwargs={"pk": meeting.id}), {"body": "Good call"})
    assert resp.status_code == 200
    assert b"Good call" in resp.content
    comment = Comment.objects.get(meeting=meeting)
    assert comment.author == ws.owner
    assert comment.parent is None


@pytest.mark.django_db
def test_post_meeting_comment_reply(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project)
    parent = Comment.objects.create(meeting=meeting, author=ws.owner, body="top")
    client.force_login(ws.owner)
    resp = client.post(
        reverse("web:post_meeting_comment", kwargs={"pk": meeting.id}),
        {"body": "a reply", "parent": str(parent.id)},
    )
    assert resp.status_code == 200
    reply = Comment.objects.get(meeting=meeting, parent=parent)
    assert reply.body == "a reply"


@pytest.mark.django_db
def test_edit_and_delete_meeting_comment(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project)
    comment = Comment.objects.create(meeting=meeting, author=ws.owner, body="orig")
    client.force_login(ws.owner)

    edit = client.post(reverse("web:edit_comment", kwargs={"comment_id": comment.id}), {"body": "edited body"})
    assert edit.status_code == 200
    comment.refresh_from_db()
    assert comment.body == "edited body"

    delete = client.post(reverse("web:delete_comment", kwargs={"comment_id": comment.id}))
    assert delete.status_code == 200
    assert not Comment.objects.filter(pk=comment.id).exists()


@pytest.mark.django_db
def test_call_detail_shows_comments(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project)
    Comment.objects.create(meeting=meeting, author=ws.owner, body="visible note")
    client.force_login(ws.owner)
    resp = client.get(reverse("web:call_detail", kwargs={"pk": meeting.id}))
    assert resp.status_code == 200
    assert b"visible note" in resp.content


@pytest.mark.django_db
def test_meeting_comment_with_attachment(client, ws_project):
    from django.core.files.uploadedfile import SimpleUploadedFile

    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project)
    client.force_login(ws.owner)
    upload = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
    resp = client.post(
        reverse("web:post_meeting_comment", kwargs={"pk": meeting.id}),
        {"body": "see attached", "file": upload},
    )
    assert resp.status_code == 200
    comment = Comment.objects.get(meeting=meeting)
    assert comment.attachments.count() == 1


@pytest.mark.django_db
def test_comment_thread_scope_namespacing(client, ws_project):
    """Page and edit-modal threads use distinct, namespaced element ids.

    Guards the duplicate-id bug: when the edit modal opens over the detail
    page both render the thread, so a modal-posted comment must target the
    modal's container (``meeting-comments-m-…``), not the page's.
    """
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project)
    client.force_login(ws.owner)

    page = client.get(reverse("web:call_detail", kwargs={"pk": meeting.id})).content
    assert f'id="meeting-comments-page-{meeting.id}"'.encode() in page

    modal = client.get(reverse("web:edit_call", kwargs={"pk": meeting.id})).content
    assert f'id="meeting-comments-m-{meeting.id}"'.encode() in modal

    # A comment posted from the modal comes back with modal-scoped ids so HTMX
    # appends it into the modal's container, not the page's hidden one.
    resp = client.post(
        reverse("web:post_meeting_comment", kwargs={"pk": meeting.id}),
        {"body": "from modal", "scope": "m"},
    )
    cid = Comment.objects.get(meeting=meeting).id
    assert f'id="meeting-comment-m-{cid}"'.encode() in resp.content
    assert f'id="meeting-comment-page-{cid}"'.encode() not in resp.content


@pytest.mark.django_db
def test_meeting_comment_reply_form_renders(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project)
    parent = Comment.objects.create(meeting=meeting, author=ws.owner, body="top")
    client.force_login(ws.owner)
    resp = client.get(reverse("web:meeting_comment_reply_form", kwargs={"pk": meeting.id, "comment_id": parent.id}))
    assert resp.status_code == 200
    # Reply composer posts back to this meeting's comment endpoint.
    assert reverse("web:post_meeting_comment", kwargs={"pk": meeting.id}).encode() in resp.content


@pytest.mark.django_db
def test_react_to_meeting_comment(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project)
    comment = Comment.objects.create(meeting=meeting, author=ws.owner, body="react to me")
    client.force_login(ws.owner)
    resp = client.post(
        reverse("web:toggle_reaction", kwargs={"target_type": "comment", "target_id": comment.id}),
        {"emoji": "👍"},
    )
    assert resp.status_code == 200
    assert comment.reactions.filter(emoji="👍", user=ws.owner).exists()


@pytest.mark.django_db
def test_edit_modal_shows_comments(client, ws_project):
    ws, project = ws_project
    meeting = MeetingFactory(workspace=ws, project=project)
    Comment.objects.create(meeting=meeting, author=ws.owner, body="modal note")
    client.force_login(ws.owner)
    resp = client.get(reverse("web:edit_call", kwargs={"pk": meeting.id}))
    assert resp.status_code == 200
    assert b"modal note" in resp.content
    # The boxed top-level composer (paperclip + Post comment) is present.
    assert b"Post comment" in resp.content


@pytest.mark.django_db
def test_meeting_task_search(client, ws_project):
    ws, project = ws_project
    task = TaskFactory(project=project, title="Migrate billing")
    client.force_login(ws.owner)
    resp = client.get(reverse("web:meeting_task_search"), {"q": "billing"})
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()["results"]]
    assert task.id in ids


@pytest.mark.django_db
def test_calls_list_no_n_plus_one(client, ws_project):
    ws, project = ws_project
    client.force_login(ws.owner)

    def _count_queries(n):
        Meeting.objects.all().delete()
        for _ in range(n):
            m = MeetingFactory(workspace=ws, project=project)
            m.participants.add(ws.owner)
            m.tasks.add(TaskFactory(project=project))
        # Warm up auth/session caches so the measured request isn't skewed by
        # one-off session lookups that vary with call ordering.
        client.get(reverse("web:calls_list"), HTTP_HX_REQUEST="true")
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(reverse("web:calls_list"), HTTP_HX_REQUEST="true")
            assert resp.status_code == 200
        return len(ctx.captured_queries)

    # Query count must not grow with the number of meetings/participants/tasks.
    assert _count_queries(2) == _count_queries(6)
