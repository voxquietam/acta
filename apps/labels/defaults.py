"""Default :class:`LabelGroup` set seeded for every workspace.

Three empty groups land on every workspace the moment it's created
(via :mod:`apps.labels.signals`) and on every workspace that already
existed when the seeding migration ran (``0003_*``). Groups stay empty
until the team fills them through admin or the future labels UI —
the seed only provides the taxonomy scaffolding and a short
``description`` telling the team what each group is for.

Owners / admins are free to rename, recolour, repurpose, or delete the
seeded groups outright; the seeder only **creates missing** groups, it
never overwrites or revives ones the team has tweaked.
"""

from __future__ import annotations

from typing import Iterable, TypedDict


class LabelGroupSeed(TypedDict):
    """One default label group entry — name, description, exclusivity flag."""

    name: str
    description: str
    is_exclusive: bool


DEFAULT_LABEL_GROUPS: tuple[LabelGroupSeed, ...] = (
    {
        "name": "Type",
        "description": (
            "What kind of work this is. Examples: feature, bug, refactor, chore, spike, docs. "
            "Pick exactly one per task."
        ),
        "is_exclusive": True,
    },
    {
        "name": "Area",
        "description": (
            "Which product area or module this touches. Examples: employees, journal, chat, auth. "
            "A task may span several areas."
        ),
        "is_exclusive": False,
    },
    {
        "name": "Layer",
        "description": (
            "Which discipline owns this — frontend, backend, devops, design, qa, mobile. "
            "Useful for full-stack teams to see which surface a task lives on."
        ),
        "is_exclusive": False,
    },
)


def seed_default_label_groups(workspace, *, group_model=None) -> int:
    """Create any missing default groups on ``workspace``.

    Idempotent: groups already present (by name) are left untouched, so a
    team that renamed ``Type`` to ``Kind`` won't get a duplicate ``Type``
    back, and a team that deleted ``Layer`` keeps it deleted on
    subsequent calls.

    Args:
        workspace: The :class:`~apps.workspaces.models.Workspace` to seed.
        group_model: Optional :class:`LabelGroup` class — passed in by the
            data migration which can't import the live model. Defaults
            to the live model.

    Returns:
        Number of groups actually created (``0`` if all three already existed).
    """
    if group_model is None:
        from apps.labels.models import LabelGroup

        group_model = LabelGroup
    existing = set(group_model.objects.filter(workspace=workspace).values_list("name", flat=True))
    to_create = [seed for seed in DEFAULT_LABEL_GROUPS if seed["name"] not in existing]
    if not to_create:
        return 0
    group_model.objects.bulk_create(
        [
            group_model(
                workspace=workspace,
                name=seed["name"],
                description=seed["description"],
                is_exclusive=seed["is_exclusive"],
            )
            for seed in to_create
        ],
    )
    return len(to_create)


def iter_default_group_names() -> Iterable[str]:
    """Yield the canonical names of the seeded groups (for tests / admin hints)."""
    for seed in DEFAULT_LABEL_GROUPS:
        yield seed["name"]
