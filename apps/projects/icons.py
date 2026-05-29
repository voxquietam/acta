"""Curated Lucide icon subset for the project icon picker.

The full Lucide set ships ~1500 icons (see ``apps/web/lucide_icons.json``).
Exposing all of them in the picker would be visual noise; this module
exports a tight list tuned to the kinds of projects Vox builds — code /
ops / research / docs / marketing / community / launches / etc.

If a user needs a glyph that's not here, they can still set
``Project.icon`` to any Lucide name via Django admin — the renderer
falls back to ``folder`` on unknown names regardless. The picker is
just the curated affordance.
"""

PROJECT_ICONS: list[str] = [
    # Containers / general
    "folder",
    "folders",
    "folder-open",
    "box",
    "boxes",
    "package",
    "package-2",
    "layers",
    "archive",
    "inbox",
    # Work / planning
    "briefcase",
    "clipboard-list",
    "clipboard-check",
    "list-checks",
    "calendar",
    "calendar-days",
    "calendar-check",
    "target",
    "crosshair",
    "flag",
    "rocket",
    "trophy",
    "award",
    "kanban",
    "milestone",
    "route",
    "workflow",
    "hourglass",
    "timer",
    # Engineering / ops
    "code",
    "code-xml",
    "braces",
    "terminal",
    "cpu",
    "server",
    "database",
    "hard-drive",
    "cloud",
    "cloud-upload",
    "cloud-download",
    "network",
    "router",
    "git-branch",
    "git-merge",
    "git-pull-request",
    "github",
    "bug",
    "wrench",
    "hammer",
    "settings",
    "settings-2",
    "sliders-horizontal",
    "shield",
    "shield-check",
    "lock",
    "key",
    "zap",
    "plug",
    "circuit-board",
    # Knowledge / content
    "book",
    "book-open",
    "bookmark",
    "library",
    "file-text",
    "files",
    "newspaper",
    "scroll",
    "notebook",
    "notebook-pen",
    "pen-line",
    "graduation-cap",
    "flask-conical",
    "microscope",
    "atom",
    "brain",
    "search",
    # People / communications
    "users",
    "users-round",
    "circle-user",
    "user-plus",
    "user-check",
    "handshake",
    "message-square",
    "message-circle",
    "messages-square",
    "mail",
    "send",
    "megaphone",
    "bell",
    "phone",
    "video",
    "mic",
    # Money / commerce
    "shopping-cart",
    "shopping-bag",
    "store",
    "credit-card",
    "wallet",
    "banknote",
    "coins",
    "receipt",
    "percent",
    "scale",
    # Analytics
    "chart-bar",
    "chart-line",
    "chart-pie",
    "trending-up",
    "activity",
    "gauge",
    # Tables / data layouts
    "table",
    "table-2",
    "table-of-contents",
    "file-spreadsheet",
    "grid-2x2",
    "grid-3x3",
    "layout-grid",
    "columns-2",
    "columns-3",
    "rows-3",
    "list",
    "list-ordered",
    # Status / time
    "clock",
    "alarm-clock",
    "play",
    "pause",
    "circle-check",
    "circle-alert",
    "triangle-alert",
    "octagon-alert",
    "info",
    "ban",
    # Shapes / markers
    "circle",
    "square",
    "triangle",
    "diamond",
    "hexagon",
    "octagon",
    "pin",
    "tag",
    "tags",
    # Security / identity
    "fingerprint",
    "id-card",
    "badge",
    "badge-check",
    "medal",
    "shield-alert",
    "siren",
    "eye",
    "eye-off",
    # Health / lab
    "thermometer",
    "stethoscope",
    "pill",
    "syringe",
    "beaker",
    # I/O
    "download",
    "upload",
    "import",
    "share-2",
    "link",
    "external-link",
    "filter",
    "filter-x",
    # Visual / design
    "palette",
    "paintbrush",
    "pen-tool",
    "image",
    "images",
    "camera",
    "film",
    "music",
    # Devices
    "monitor",
    "laptop",
    "smartphone",
    "tablet",
    "headphones",
    "qr-code",
    # Travel / places / nature
    "compass",
    "map",
    "map-pin",
    "globe",
    "plane",
    "car",
    "truck",
    "ship",
    "house",
    "building",
    "school",
    "hospital",
    "factory",
    "tree-pine",
    "leaf",
    "sun",
    "moon",
    "mountain",
    "droplet",
    "flame",
    # Misc
    "heart",
    "star",
    "sparkles",
    "lightbulb",
    "gift",
    "coffee",
    "pizza",
    "gem",
    "crown",
    "puzzle",
    "scissors",
]


def is_curated(name: str) -> bool:
    """Return whether ``name`` belongs to the curated picker list.

    Used by the ``set_project_icon`` endpoint to reject submissions
    from outside the curated set without forcing the model to validate
    against an enum (admins keep full Lucide freedom).
    """
    return name in set(PROJECT_ICONS)


# Curated colour palette keys for project icons. The picker offers
# exactly these tokens; ``set_project_icon`` rejects anything else.
# Empty string is also accepted — clears to the default neutral tint.
PROJECT_ICON_COLORS: list[str] = [
    # Reds → oranges → yellows
    "red",
    "orange",
    "amber",
    "yellow",
    # Greens
    "lime",
    "green",
    "emerald",
    "teal",
    # Blues
    "cyan",
    "sky",
    "blue",
    "indigo",
    # Purples → pinks
    "violet",
    "purple",
    "fuchsia",
    "pink",
    "rose",
    # Neutrals
    "slate",
    "gray",
    "zinc",
    "stone",
]


def is_curated_color(color: str) -> bool:
    """Return whether ``color`` is a valid picker palette key.

    Empty string is intentionally NOT a member here — the caller
    should accept ``""`` explicitly when clearing.
    """
    return color in set(PROJECT_ICON_COLORS)


# Tailwind class fragment per colour key. Used by templates when
# rendering the icon — keeps the palette mapping in one place so a
# theme refresh can re-tune all surfaces at once.
ICON_COLOR_CLASSES: dict[str, str] = {
    "": "text-subtle-foreground",
    "red": "text-red-500",
    "orange": "text-orange-500",
    "amber": "text-amber-500",
    "yellow": "text-yellow-500",
    "lime": "text-lime-500",
    "green": "text-green-500",
    "emerald": "text-emerald-500",
    "teal": "text-teal-500",
    "cyan": "text-cyan-500",
    "sky": "text-sky-500",
    "blue": "text-blue-500",
    "indigo": "text-indigo-500",
    "violet": "text-violet-500",
    "purple": "text-purple-500",
    "fuchsia": "text-fuchsia-500",
    "pink": "text-pink-500",
    "rose": "text-rose-500",
    "slate": "text-slate-500",
    "gray": "text-gray-500",
    "zinc": "text-zinc-500",
    "stone": "text-stone-500",
}


def color_class(color: str) -> str:
    """Return the Tailwind text-colour class for a colour token, with
    fallback to the neutral subtle-foreground when ``color`` is unknown
    or empty."""
    return ICON_COLOR_CLASSES.get(color, ICON_COLOR_CLASSES[""])
