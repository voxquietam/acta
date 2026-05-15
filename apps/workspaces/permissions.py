"""Workspace-scoped DRF permission classes.

See docs/decisions/0010-permissions.md for the role matrix that backs
these classes.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Workspace, WorkspaceMember


def workspace_of(obj):
    """Resolve the :class:`Workspace` an arbitrary domain object lives in.

    Handles direct ``workspace`` FK, project-scoped objects via
    ``obj.project.workspace``, and task-scoped objects (comments) via
    ``obj.task.project.workspace``.

    Args:
        obj: A model instance — :class:`Workspace`, :class:`Project`,
            :class:`Task`, :class:`Comment`, :class:`Label`, etc.

    Returns:
        The :class:`Workspace` the object belongs to, or ``None`` if the
        type is unrecognized.
    """
    if isinstance(obj, Workspace):
        return obj
    if hasattr(obj, "workspace_id") and obj.workspace_id is not None:
        return obj.workspace
    if hasattr(obj, "project_id") and obj.project_id is not None:
        return obj.project.workspace
    if hasattr(obj, "task_id") and obj.task_id is not None:
        return obj.task.project.workspace
    return None


def membership(user, workspace):
    """Return the :class:`WorkspaceMember` row for ``user`` in ``workspace``.

    Args:
        user: The acting :class:`User`.
        workspace: The target :class:`Workspace`.

    Returns:
        The matching :class:`WorkspaceMember`, or ``None`` if the user has
        no membership in that workspace.
    """
    if not (user and user.is_authenticated and workspace):
        return None
    return WorkspaceMember.objects.filter(user=user, workspace=workspace).first()


class IsWorkspaceMember(BasePermission):
    """Allow access only if the request user is a member of the workspace."""

    def has_object_permission(self, request, view, obj):
        """Allow when the user has any role in the object's workspace.

        Args:
            request: The current DRF request.
            view: The view instance.
            obj: The model instance being accessed.

        Returns:
            ``True`` if the user is a member, ``False`` otherwise.
        """
        return membership(request.user, workspace_of(obj)) is not None


class IsWorkspaceAdmin(BasePermission):
    """Allow access only to owners and admins of the workspace.

    For methods that don't load an object (list, create), the check defers
    to ``has_permission`` returning ``True`` so per-action view code can
    enforce admin-only semantics on its own.
    """

    def has_object_permission(self, request, view, obj):
        """Allow when the user is admin or owner of the object's workspace.

        Args:
            request: The current DRF request.
            view: The view instance.
            obj: The model instance being accessed.

        Returns:
            ``True`` if the user is owner or admin, ``False`` otherwise.
        """
        m = membership(request.user, workspace_of(obj))
        return m is not None and m.role in (WorkspaceMember.OWNER, WorkspaceMember.ADMIN)


class IsWorkspaceOwner(BasePermission):
    """Allow access only to the workspace owner."""

    def has_object_permission(self, request, view, obj):
        """Allow when the user is the owner of the object's workspace.

        Args:
            request: The current DRF request.
            view: The view instance.
            obj: The model instance being accessed.

        Returns:
            ``True`` if the user is owner, ``False`` otherwise.
        """
        m = membership(request.user, workspace_of(obj))
        return m is not None and m.role == WorkspaceMember.OWNER


class IsAuthorOrWorkspaceAdmin(BasePermission):
    """Allow safe methods to any member; writes only to author or admin/owner.

    Used for comments and project updates: any workspace member can read,
    but only the author can edit, and only admins/owner can edit/delete
    someone else's content.
    """

    def has_object_permission(self, request, view, obj):
        """Decide based on method and authorship.

        Args:
            request: The current DRF request.
            view: The view instance.
            obj: The model instance being accessed — must have an
                ``author`` attribute.

        Returns:
            ``True`` if access is allowed, ``False`` otherwise.
        """
        m = membership(request.user, workspace_of(obj))
        if m is None:
            return False
        if request.method in SAFE_METHODS:
            return True
        if getattr(obj, "author_id", None) == request.user.id:
            return True
        return m.role in (WorkspaceMember.OWNER, WorkspaceMember.ADMIN)
