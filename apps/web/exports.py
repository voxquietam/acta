"""JSON serialization for the "export filtered view" download endpoints.

Hand-rolled (not the DRF serializers) on purpose: the export is meant to
be read by a human or a script, so assignees, labels, and projects render
as names rather than bare ids, and the shape stays decoupled from the
API's request/HTML-rendering machinery.

The functions assume their inputs were loaded with the right
``select_related`` / ``prefetch_related`` so serialization adds no
queries — see the export views in :mod:`apps.web.views` for the querysets
and :func:`apps.reactions.services.summarize_reactions` for the one-query
reaction aggregation reused here.
"""

from collections import defaultdict

from apps.comments.models import Comment
from apps.reactions.services import summarize_reactions


def _user_ref(user):
    """Return a compact ``{id, username, name}`` dict for a user, or ``None``.

    Args:
        user: A :class:`~apps.accounts.models.User` or ``None``.

    Returns:
        The reference dict, or ``None`` when ``user`` is ``None``.
    """
    if user is None:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "name": user.display_name,
    }


def _dt(value):
    """Return an ISO-8601 string for a datetime/date, or ``None``."""
    return value.isoformat() if value else None


def _reactions(summary):
    """Flatten a ``summarize_reactions`` bucket list to export shape.

    Drops the viewer-specific ``mine`` flag (meaningless in a file) and
    renames ``names`` to ``by``.

    Args:
        summary: The list ``summarize_reactions`` returns for one target.

    Returns:
        A list of ``{emoji, count, by}`` dicts.
    """
    return [
        {
            "emoji": bucket["emoji"],
            "count": bucket["count"],
            "by": bucket["names"],
        }
        for bucket in summary
    ]


def _serialize_comment(comment, *, replies_by_parent, reactions_by_id=None):
    """Serialize one comment, recursing into its replies.

    Args:
        comment: The :class:`~apps.comments.models.Comment` to serialize.
        replies_by_parent: ``{parent_id: [reply, ...]}`` for nesting.
        reactions_by_id: Optional ``{comment_id: summary}`` map; when
            given, a ``reactions`` key is added. ``None`` omits reactions
            (task-comment export skips them).

    Returns:
        A JSON-ready dict of the comment, with a nested ``replies`` list.
    """
    data = {
        "id": comment.id,
        "author": _user_ref(comment.author),
        "body": comment.body,
        "created_at": _dt(comment.created_at),
        "updated_at": _dt(comment.updated_at),
        "replies": [
            _serialize_comment(reply, replies_by_parent=replies_by_parent, reactions_by_id=reactions_by_id)
            for reply in replies_by_parent.get(comment.id, [])
        ],
    }
    if reactions_by_id is not None:
        data["reactions"] = _reactions(reactions_by_id.get(comment.id, []))
    return data


def serialize_task(task, *, comments=None):
    """Serialize one task to the full export shape.

    Assumes ``assignee``, ``reporter``, ``parent__project``, and
    ``project`` are select-related and ``labels`` is prefetched.

    Args:
        task: The :class:`~apps.tasks.models.Task` to serialize.
        comments: Optional pre-serialized comment list; when given it is
            attached under a ``comments`` key.

    Returns:
        A JSON-ready dict of the task's fields.
    """
    data = {
        "slug": task.slug,
        "number": task.number,
        "title": task.title,
        "description": task.description or "",
        "status": task.status,
        "priority": task.priority,
        "size": task.size,
        "due_date": _dt(task.due_date),
        "assignee": _user_ref(task.assignee),
        "reporter": _user_ref(task.reporter),
        "parent": task.parent.slug if task.parent_id else None,
        "labels": [
            {
                "name": label.name,
                "color": label.color,
            }
            for label in task.labels.all()
        ],
        "project": {
            "slug_prefix": task.project.slug_prefix,
            "name": task.project.name,
        },
        "created_at": _dt(task.created_at),
        "updated_at": _dt(task.updated_at),
    }
    if comments is not None:
        data["comments"] = comments
    return data


def serialize_tasks(tasks, *, include_comments=False):
    """Serialize an iterable of tasks (already filtered + ordered).

    When ``include_comments`` is set, every comment for the whole task set
    is loaded in one query and grouped in Python (top-level + replies), so
    attaching threads stays N+1-free no matter how many tasks. Task
    comments carry no reactions (only the overview export does).

    Args:
        tasks: An iterable of :class:`~apps.tasks.models.Task`.
        include_comments: When True, attach each task's ``comments`` tree.

    Returns:
        A list of task dicts in the iterable's order.
    """
    tasks = list(tasks)
    if not include_comments:
        return [serialize_task(task) for task in tasks]

    comments = list(
        Comment.objects.filter(task_id__in=[task.id for task in tasks]).select_related("author").order_by("created_at"),
    )
    top_level_by_task = defaultdict(list)
    replies_by_parent = defaultdict(list)
    for comment in comments:
        if comment.parent_id is None:
            top_level_by_task[comment.task_id].append(comment)
        else:
            replies_by_parent[comment.parent_id].append(comment)

    return [
        serialize_task(
            task,
            comments=[
                _serialize_comment(comment, replies_by_parent=replies_by_parent)
                for comment in top_level_by_task.get(task.id, [])
            ],
        )
        for task in tasks
    ]


def serialize_project_overview(project, *, viewer_id):
    """Serialize a project's overview: description + all updates + comments.

    Loads every update and every comment (top-level and replies) for the
    project in a fixed number of queries — updates, update reactions,
    comments, comment reactions — and nests them in Python, so the export
    stays N+1-free no matter how long the threads are. Reply nesting is
    handled recursively, so a future multi-level thread serializes too.

    Args:
        project: The :class:`~apps.projects.models.Project`; ``workspace``
            and ``lead`` should be select-related.
        viewer_id: The requesting user's id (only used to satisfy
            ``summarize_reactions``; the viewer-specific flag is dropped).

    Returns:
        A JSON-ready dict with ``project`` meta and an ``updates`` list,
        each update carrying its ``reactions`` and nested ``comments``.
    """
    updates = list(project.updates.select_related("author").order_by("created_at"))
    update_ids = [update.id for update in updates]
    update_reactions = summarize_reactions(
        target_field="project_update",
        ids=update_ids,
        user_id=viewer_id,
    )

    comments = list(
        Comment.objects.filter(project_update_id__in=update_ids).select_related("author").order_by("created_at"),
    )
    comment_reactions = summarize_reactions(
        target_field="comment",
        ids=[comment.id for comment in comments],
        user_id=viewer_id,
    )

    top_level_by_update = defaultdict(list)
    replies_by_parent = defaultdict(list)
    for comment in comments:
        if comment.parent_id is None:
            top_level_by_update[comment.project_update_id].append(comment)
        else:
            replies_by_parent[comment.parent_id].append(comment)

    return {
        "project": {
            "slug_prefix": project.slug_prefix,
            "name": project.name,
            "description": project.description or "",
            "lead": _user_ref(project.lead),
            "workspace": project.workspace.name,
            "created_at": _dt(project.created_at),
        },
        "updates": [
            {
                "id": update.id,
                "author": _user_ref(update.author),
                "health": update.health,
                "body": update.body,
                "created_at": _dt(update.created_at),
                "updated_at": _dt(update.updated_at),
                "reactions": _reactions(update_reactions.get(update.id, [])),
                "comments": [
                    _serialize_comment(
                        comment,
                        replies_by_parent=replies_by_parent,
                        reactions_by_id=comment_reactions,
                    )
                    for comment in top_level_by_update.get(update.id, [])
                ],
            }
            for update in updates
        ],
    }
