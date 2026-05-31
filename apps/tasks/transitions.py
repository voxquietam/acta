"""Required-on-transition policy enforcement.

The workspace-level ``required_fields`` policy gates certain status
transitions on specific task fields being filled in. This module is the
single source of truth callers (inline status change view, bulk PATCH
endpoint, kanban DnD) ask "is this move allowed?" — keeping enforcement
out of model ``save()`` so seed scripts / migrations / management
commands aren't blocked by user-facing policy.
"""

from apps.tasks.models import Task

# Human-readable labels for the field names that show up in the error
# response. Mirrors the gettext keys used in the settings UI.
_FIELD_LABELS = {
    "assignee": "Assignee",
    "priority": "Priority",
    "description": "Description",
}


_FORWARD_FROM_TODO = (Task.STATUS_IN_PROGRESS, Task.STATUS_IN_REVIEW, Task.STATUS_DONE)


def validate_status_transition(task, new_status, workspace):
    """Return missing-field names that block ``task`` from moving to ``new_status``.

    Two gates fire — both directional, so grooming backwards (push a card
    back into planned / ready / cancelled) is always allowed:

    * **leave_todo** — when the task is currently ``to-do`` and is moving
      **forward** (in-progress, in-review, done), every flagged field in
      ``Workspace.REQUIRED_TRANSITIONS["leave_todo"]`` must be set. Going
      back to planned / ready / cancelled is grooming, not work, and
      skips the gate.
    * **enter_in_review** — when the task is moving into ``in-review``
      from any other status, every flagged field in
      ``Workspace.REQUIRED_TRANSITIONS["enter_in_review"]`` must be set.

    Args:
        task: The :class:`Task` being moved.
        new_status: The target status key.
        workspace: The :class:`Workspace` whose policy applies.

    Returns:
        A list of field names that are missing; empty when the move is
        allowed. Returned in the order the policy lists them so the UI
        message reads predictably.
    """
    config = workspace.required_fields_config()
    missing = []
    old_status = task.status
    if old_status == Task.STATUS_TODO and new_status in _FORWARD_FROM_TODO:
        for field in workspace.REQUIRED_TRANSITIONS["leave_todo"]:
            if config["leave_todo"].get(field) and not _field_filled(task, field):
                missing.append(field)
    if new_status == Task.STATUS_IN_REVIEW and old_status != Task.STATUS_IN_REVIEW:
        for field in workspace.REQUIRED_TRANSITIONS["enter_in_review"]:
            if config["enter_in_review"].get(field) and not _field_filled(task, field):
                missing.append(field)
    return missing


def _field_filled(task, field):
    """Return whether ``field`` on ``task`` counts as set for the policy."""
    if field == "assignee":
        return task.assignee_id is not None
    if field == "priority":
        return bool(task.priority)
    if field == "description":
        return bool((task.description or "").strip())
    return True


def format_missing_message(missing):
    """Format the missing-field list into a user-facing toast message."""
    if not missing:
        return ""
    labels = [_FIELD_LABELS.get(f, f.title()) for f in missing]
    if len(labels) == 1:
        return f"{labels[0]} is required for this transition."
    return f"{', '.join(labels)} are required for this transition."
