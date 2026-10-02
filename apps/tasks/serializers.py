from django.utils.translation import gettext_lazy as _

from rest_framework import serializers

from apps.common.markdown import render_markdown
from apps.workspaces.models import WorkspaceMember

from .models import Task


class TaskSerializer(serializers.ModelSerializer):
    slug = serializers.ReadOnlyField()
    description_html = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            "id",
            "project",
            "number",
            "slug",
            "parent",
            "kind",
            "epic",
            "title",
            "description",
            "description_html",
            "status",
            "priority",
            "size",
            "start_date",
            "end_date",
            "due_date",
            "assignee",
            "reporter",
            "labels",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "number",
            "slug",
            "reporter",
            "created_at",
            "updated_at",
        ]

    def get_description_html(self, obj) -> str | None:
        """Render the task description from Markdown to sanitized HTML.

        Only renders when the request opted in via ``?expand=description_html``
        — otherwise list endpoints would pay the markdown+bleach cost
        on every row (50 rows = 50 sanitisation passes) even though
        the kanban / table UIs only render the body on the detail
        page. Detail clients ask for the expansion explicitly.

        Args:
            obj: The :class:`Task` instance.

        Returns:
            Sanitized HTML produced from ``obj.description``, or
            ``None`` when not requested.
        """
        request = self.context.get("request")
        if request is None:
            return render_markdown(obj.description)
        expand = request.query_params.get("expand", "") if hasattr(request, "query_params") else ""
        if "description_html" not in {p.strip() for p in expand.split(",") if p.strip()}:
            return None
        return render_markdown(obj.description)

    def validate_project(self, project):
        """Reject tasks targeted at projects the user cannot access.

        Args:
            project: The candidate :class:`Project` for the task.

        Returns:
            The validated project.

        Raises:
            serializers.ValidationError: When the user is not a member of
                the project's workspace.
        """
        user = self.context["request"].user
        if not WorkspaceMember.objects.filter(user=user, workspace=project.workspace).exists():
            raise serializers.ValidationError(_("You are not a member of this project's workspace."))
        return project

    def validate_status(self, value):
        """Ensure the submitted status is one of the known values.

        Args:
            value: The candidate status string.

        Returns:
            The validated status.

        Raises:
            serializers.ValidationError: When the status is unknown.
        """
        if value not in Task.STATUS_VALUES:
            raise serializers.ValidationError(
                _("Unknown status: %(value)s. Must be one of %(allowed)s.")
                % {"value": value, "allowed": ", ".join(Task.STATUS_VALUES)},
            )
        return value

    def validate(self, attrs):
        """Enforce cross-field invariants for tasks.

        Checks:
            * Parent and child must share a project.
            * Subtask depth is limited to one level.
            * An epic collects plain tasks from its own workspace, is
              never a subtask, and carries none of the fields it derives
              from them. Unlike a parent, it reaches across projects on
              purpose. See docs/decisions/0036-epics.md.
            * Labels (if any) must belong to the same workspace as the
              task's project.
            * Assignee (if set on this write) must be an active member
              of the project's workspace — prevents new "orphan"
              assignments to users who left or were removed. Existing
              orphan assignments stay untouched (writes that don't
              change ``assignee`` skip the check).

        Args:
            attrs: Pre-validated field values from the serializer.

        Returns:
            The validated attrs dict.

        Raises:
            serializers.ValidationError: When any invariant is violated.
        """
        parent = attrs.get("parent") or getattr(self.instance, "parent", None)
        project = attrs.get("project") or getattr(self.instance, "project", None)
        labels = attrs.get("labels")

        if parent and project and parent.project_id != project.id:
            raise serializers.ValidationError(
                {"parent": _("Subtask must be in the same project as its parent.")},
            )
        if parent and parent.parent_id is not None:
            raise serializers.ValidationError(
                {"parent": _("Subtasks cannot have their own subtasks (depth limit 1).")},
            )
        kind = attrs.get("kind") or getattr(self.instance, "kind", Task.KIND_TASK)
        epic = attrs["epic"] if "epic" in attrs else getattr(self.instance, "epic", None)
        if kind not in Task.KIND_VALUES:
            raise serializers.ValidationError(
                {"kind": _("Unknown kind: %(value)s.") % {"value": kind}},
            )
        if (kind == Task.KIND_EPIC or epic is not None) and project and not project.workspace.epics_enabled:
            raise serializers.ValidationError(
                {"kind": _("Epics are turned off for this workspace.")},
            )
        if epic is not None:
            if kind == Task.KIND_EPIC:
                raise serializers.ValidationError(
                    {"epic": _("An epic cannot belong to another epic.")},
                )
            if epic.kind != Task.KIND_EPIC:
                raise serializers.ValidationError(
                    {"epic": _("Tasks can only be collected by an epic.")},
                )
            if project and epic.project.workspace_id != project.workspace_id:
                raise serializers.ValidationError(
                    {"epic": _("Epic must be in the same workspace.")},
                )
        if kind == Task.KIND_EPIC:
            if parent is not None:
                raise serializers.ValidationError(
                    {"parent": _("An epic cannot be a subtask.")},
                )
            for field, message in (
                ("due_date", _("An epic takes its dates from its tasks.")),
                ("size", _("An epic takes its size from its tasks.")),
                ("cycle", _("An epic does not join a cycle.")),
            ):
                value = attrs[field] if field in attrs else getattr(self.instance, field, None)
                if value is not None:
                    raise serializers.ValidationError({field: message})
        if labels and project:
            wrong = [lab.id for lab in labels if lab.workspace_id != project.workspace_id]
            if wrong:
                raise serializers.ValidationError(
                    {
                        "labels": _("Labels %(ids)s are not in this project's workspace.") % {"ids": wrong},
                    },
                )
        # Only validate ``assignee`` when this write actually touches
        # the field. Otherwise unrelated writes to existing tasks
        # whose assignees were orphaned years ago would start failing.
        if "assignee" in attrs and project:
            assignee = attrs.get("assignee")
            if (
                assignee is not None
                and not WorkspaceMember.objects.filter(user=assignee, workspace=project.workspace).exists()
            ):
                raise serializers.ValidationError(
                    {
                        "assignee": _("User %(user)s is not a member of this project's workspace.")
                        % {"user": assignee.display_name or assignee.username},
                    },
                )
        # Scheduling a task's timeline (start / end) is the assignee's call:
        # only the current assignee may move those dates on an existing
        # task. An unassigned task stays open to anyone. The hard
        # ``due_date`` deadline is intentionally unrestricted. Mirrors the
        # web inline-edit guard (apps.web.views._can_edit_task_dates).
        if self.instance is not None:
            request = self.context.get("request")
            user = getattr(request, "user", None)
            assignee_id = self.instance.assignee_id
            if user is not None and assignee_id is not None and assignee_id != user.id:
                for date_field in ("start_date", "end_date"):
                    if date_field in attrs and attrs[date_field] != getattr(self.instance, date_field):
                        raise serializers.ValidationError(
                            {date_field: _("Only the assignee can change the start/end date.")},
                        )
        return attrs
