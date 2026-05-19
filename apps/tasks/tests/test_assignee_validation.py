"""Assignee membership validation on ``TaskSerializer``.

Prevents new "orphan" assignments — tasks assigned to users who are
not members of the project's workspace. Existing orphan assignments
(from before the validator landed, or from data migrations) keep
working: only writes that *touch* the ``assignee`` field run the
membership check.
"""

import pytest
from rest_framework.test import APIRequestFactory

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.serializers import TaskSerializer
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


def _serializer(*, instance=None, data=None, request_user):
    factory = APIRequestFactory()
    req = factory.post("/api/v1/tasks/")
    req.user = request_user
    return TaskSerializer(instance=instance, data=data, partial=instance is not None, context={"request": req})


@pytest.mark.django_db
class TestAssigneeMembershipValidation:
    """``assignee`` must be in ``project.workspace.members`` when set."""

    def test_assigning_active_member_passes(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        member = UserFactory()
        WorkspaceMember.objects.create(user=member, workspace=ws)
        ser = _serializer(
            data={
                "project": project.id,
                "title": "new",
                "status": Task.STATUS_TODO,
                "assignee": member.id,
            },
            request_user=ws.owner,
        )
        assert ser.is_valid(), ser.errors

    def test_assigning_non_member_fails(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        outsider = UserFactory()  # never added to the workspace
        ser = _serializer(
            data={
                "project": project.id,
                "title": "new",
                "status": Task.STATUS_TODO,
                "assignee": outsider.id,
            },
            request_user=ws.owner,
        )
        assert not ser.is_valid()
        assert "assignee" in ser.errors

    def test_assigning_null_passes(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        ser = _serializer(
            data={
                "project": project.id,
                "title": "new",
                "status": Task.STATUS_TODO,
                "assignee": None,
            },
            request_user=ws.owner,
        )
        assert ser.is_valid(), ser.errors

    def test_existing_orphan_assignment_not_re_validated_on_unrelated_write(self):
        """Touch only ``title`` on a task with an orphan assignee — must pass.

        Old assignments shouldn't suddenly become uneditable just
        because the assignee left the workspace.
        """
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        outsider = UserFactory()
        task = TaskFactory(project=project, reporter=ws.owner, assignee=outsider)
        # ``outsider`` is now a non-member but ``task.assignee`` still
        # points at them — simulates the orphan-after-removal state.
        ser = _serializer(instance=task, data={"title": "updated"}, request_user=ws.owner)
        assert ser.is_valid(), ser.errors

    def test_existing_orphan_blocked_when_assignee_is_re_set_to_same_user(self):
        """If the write explicitly re-sends the orphan assignee, block it.

        The validator runs whenever ``assignee`` is in the write attrs —
        even if the value is unchanged. That's intentional: a UI form
        that submits the full task shape will surface the problem to
        the user, instead of silently letting it through.
        """
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        outsider = UserFactory()
        task = TaskFactory(project=project, reporter=ws.owner, assignee=outsider)
        ser = _serializer(instance=task, data={"assignee": outsider.id}, request_user=ws.owner)
        assert not ser.is_valid()
        assert "assignee" in ser.errors
