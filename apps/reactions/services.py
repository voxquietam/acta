from __future__ import annotations

from collections import OrderedDict
from typing import Iterable

from .models import Reaction

# Map the public ``target_type`` token (URL param / template arg) to the
# model FK field it stands for. The web layer never exposes the raw field
# name ``project_update`` — it uses the shorter ``update``.
TARGET_TYPES = {
    "task": "task",
    "comment": "comment",
    "update": "project_update",
}


def summarize_reactions(*, target_field: str, ids: Iterable[int], user_id: int | None) -> dict[int, list[dict]]:
    """Aggregate reactions for many targets of one type in a single query.

    Groups every reaction row for the given target ids by ``(target,
    emoji)`` in Python — one ``SELECT`` regardless of how many targets or
    reactions there are — so rendering a long comment thread never fans
    out into per-row queries (the no-N+1 rule).

    Args:
        target_field: The ``Reaction`` FK field to group on — one of
            ``task`` / ``comment`` / ``project_update``.
        ids: Target ids to summarize.
        user_id: The viewer's id, used to flag which emoji they reacted
            with (so the UI can highlight the pill). ``None`` for an
            anonymous viewer — nothing is flagged ``mine``.

    Returns:
        A dict mapping target id to an ordered list of summary dicts
        ``{"emoji", "count", "mine", "names"}``. Emoji keep first-reacted
        order; ``names`` powers the "who reacted" hover. Targets with no
        reactions are absent from the dict.
    """
    ids = list(ids)
    if not ids:
        return {}
    rows = Reaction.objects.filter(**{f"{target_field}__in": ids}).select_related("user").order_by("created_at", "id")
    summary: dict[int, OrderedDict] = {}
    for reaction in rows:
        target_id = getattr(reaction, f"{target_field}_id")
        buckets = summary.setdefault(target_id, OrderedDict())
        bucket = buckets.get(reaction.emoji)
        if bucket is None:
            bucket = {"emoji": reaction.emoji, "count": 0, "mine": False, "names": []}
            buckets[reaction.emoji] = bucket
        bucket["count"] += 1
        if user_id is not None and reaction.user_id == user_id:
            bucket["mine"] = True
        if reaction.user_id is not None:
            bucket["names"].append(reaction.user.display_name)
    return {target_id: list(buckets.values()) for target_id, buckets in summary.items()}


def attach_reactions(*, objs, target_field: str, user_id: int | None):
    """Attach a ``reaction_summary`` attribute to each object in ``objs``.

    A thin wrapper over :func:`summarize_reactions` for the common case of
    decorating a list of model instances before they hit a template. Each
    object gets ``obj.reaction_summary`` — the list the ``_reaction_bar``
    partial renders, or ``[]`` when the object has no reactions.

    Args:
        objs: An iterable of model instances sharing the same target type.
        target_field: The ``Reaction`` FK field for these objects.
        user_id: The viewer's id (see :func:`summarize_reactions`).

    Returns:
        The materialized list of objects, each carrying ``reaction_summary``.
    """
    objs = list(objs)
    summary = summarize_reactions(
        target_field=target_field,
        ids=[obj.id for obj in objs],
        user_id=user_id,
    )
    for obj in objs:
        obj.reaction_summary = summary.get(obj.id, [])
    return objs


def toggle_reaction(*, user, target_field: str, target, emoji: str) -> bool:
    """Add the user's ``emoji`` reaction on ``target``, or remove it if present.

    Idempotent per click: the second click of the same emoji by the same
    user removes the reaction. The per-target ``(user, emoji)`` uniqueness
    constraint guarantees there is at most one row to find.

    Args:
        user: The reacting :class:`User`.
        target_field: The ``Reaction`` FK field for ``target``.
        target: The target model instance (task / comment / project update).
        emoji: The emoji grapheme to toggle.

    Returns:
        ``True`` if the reaction was added, ``False`` if it was removed.
    """
    lookup = {
        "user": user,
        "emoji": emoji,
        target_field: target,
    }
    existing = Reaction.objects.filter(**lookup).first()
    if existing is not None:
        existing.delete()
        return False
    Reaction.objects.create(**lookup)
    return True
