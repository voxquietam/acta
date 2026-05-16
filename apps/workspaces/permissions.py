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

    For create operations DRF doesn't yet have an instance, so the
    workspace is resolved from ``request.data['workspace']`` (or
    ``request.data['workspace_id']``) before checking membership. A
    missing / non-numeric / inaccessible workspace id is treated as a
    permission failure — the request can't pretend it's just authn
    against a free-form payload.

    Without this check, any authenticated user could ``POST`` a
    ``WorkspaceMember`` row promoting themselves to ``owner`` of any
    workspace.
    """

    def has_permission(self, request, view):
        """Allow membership-creating requests only to admins / owners.

        Safe methods (``GET``/``HEAD``/``OPTIONS``) and detail-route
        accesses defer to ``has_object_permission``; only the
        body-payload-bearing methods need the workspace resolution
        from the request body here.
        """
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        # For PUT/PATCH/DELETE on a detail route DRF calls
        # ``has_object_permission`` against the loaded instance. The
        # only path that lacks that hook is ``POST`` on the list route,
        # which is the one this branch defends.
        if view.action != "create":
            return True
        raw_workspace = request.data.get("workspace") or request.data.get("workspace_id")
        try:
            workspace_id = int(raw_workspace)
        except (TypeError, ValueError):
            return False
        workspace = Workspace.objects.filter(pk=workspace_id).first()
        m = membership(request.user, workspace)
        return m is not None and m.role in (WorkspaceMember.OWNER, WorkspaceMember.ADMIN)

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
