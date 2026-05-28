"""Curated hex-colour palette for label pills.

The labels management UI offers exactly these colours in its picker;
admins typing a hex into Django admin can still pick anything (the
:class:`HEX_COLOR_VALIDATOR` regex remains permissive). Keeping the
front-end palette tight stops the workspace from collecting twenty
near-identical shades of teal that read as the same chip in lists.

Colours are mid-saturation Tailwind 400/500-ish hues — bright enough
to stand out against the ``zinc-900`` surfaces, dim enough that two
adjacent pills don't strobe.
"""

from __future__ import annotations

LABEL_COLORS: tuple[str, ...] = (
    # Reds → oranges → yellows
    "#ef4444",  # red-500
    "#f97316",  # orange-500
    "#f59e0b",  # amber-500
    "#eab308",  # yellow-500
    # Greens
    "#84cc16",  # lime-500
    "#22c55e",  # green-500
    "#10b981",  # emerald-500
    "#14b8a6",  # teal-500
    # Blues
    "#06b6d4",  # cyan-500
    "#0ea5e9",  # sky-500
    "#3b82f6",  # blue-500
    "#6366f1",  # indigo-500
    # Purples → pinks
    "#8b5cf6",  # violet-500
    "#a855f7",  # purple-500
    "#d946ef",  # fuchsia-500
    "#ec4899",  # pink-500
    "#f43f5e",  # rose-500
    # Neutrals
    "#64748b",  # slate-500
    "#737373",  # neutral-500
    "#71717a",  # zinc-500
)


def is_curated_label_color(color: str) -> bool:
    """Return whether ``color`` is one of the picker's curated hex values."""
    return color.lower() in {c.lower() for c in LABEL_COLORS}
