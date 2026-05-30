#!/usr/bin/env python3
"""Build ``static/sprites/lucide.svg`` from the icons referenced in the codebase.

Wave 4 §F5 follow-up to PR-1 / PR-2: replaces the per-call inline SVG
that ``{% lucide %}`` emits (~280 B each, repeated 600-1500× on heavy
pages) with a single browser-cacheable sprite + a ~80 B
``<svg><use href="…#lu-NAME"/></svg>`` wrapper. The sprite ships
``fill="none" stroke="currentColor" stroke-width="2"`` on each
``<symbol>`` so ``currentColor`` flows from the wrapping element
(matching the previous inline behaviour).

Discovery sources:

1. ``grep -roh '{% lucide "NAME"' templates/`` — every hardcoded name.
2. ``apps.projects.icons.PROJECT_ICONS`` — every name the project icon
   picker accepts (``Project.icon`` defaults to ``"folder"``).
3. ``KPI_CARDS`` / ``HEATMAP_ROWS`` constants in ``apps.dashboards`` —
   icons referenced via ``{% lucide k.icon %}`` / ``{% lucide h.icon %}``.
4. ``DEFAULT_ICON`` (``"folder"``) — always included so the unknown-name
   fallback in ``lucide.py`` resolves to a sprite symbol.

Unknown names at runtime still fall back to ``DEFAULT_ICON`` via the
template tag — they don't need to exist in the sprite.

Run via ``make build-sprite`` after touching templates or the picker
allowlist; the output file is committed so deploy doesn't need
``node_modules`` / Python build steps.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
ICONS_JSON = REPO_ROOT / "apps" / "web" / "lucide_icons.json"
OUTPUT = REPO_ROOT / "static" / "sprites" / "lucide.svg"
TEMPLATES_DIR = REPO_ROOT / "templates"
APPS_DIR = REPO_ROOT / "apps"

DEFAULT_ICON = "circle-dashed"

# Matches both ``{% lucide "name" %}`` and ``{% lucide 'name' %}`` —
# only hardcoded names. Dynamic ``{% lucide var %}`` callers must
# expose their possible values through one of the discovery sources
# below (PROJECT_ICONS / dashboard constants).
_HARDCODED = re.compile(r'\{%\s*lucide\s+["\']([a-z0-9][a-z0-9-]*)["\']')

# Inline SVG class+attr block; replaced by re.sub so we can pull body.
_SVG_OPEN_TAG = re.compile(r"<svg\b[^>]*>", re.DOTALL)
_SVG_CLOSE_TAG = re.compile(r"</svg>\s*$")


def _collect_hardcoded() -> set[str]:
    """Scan templates + apps for ``{% lucide "NAME"`` literals."""
    names: set[str] = set()
    for path in list(TEMPLATES_DIR.rglob("*.html")) + list(APPS_DIR.rglob("*.html")):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in _HARDCODED.finditer(text):
            names.add(match.group(1))
    return names


def _collect_dynamic() -> set[str]:
    """Pull the names referenced by dynamic ``{% lucide var %}`` callers.

    Each module that owns a "list of valid icon names" is hand-wired
    here; this keeps the build script free of Django bootstrap so it
    can run outside the container.
    """
    names: set[str] = {DEFAULT_ICON}

    # Project icon picker — every name the user can attach to a project.
    project_icons_module = APPS_DIR / "projects" / "icons.py"
    icons_locals: dict[str, list[str]] = {}
    exec(project_icons_module.read_text(), icons_locals)
    names.update(icons_locals.get("PROJECT_ICONS", []))

    # Dashboard KPI + heatmap rows are wired through Python constants;
    # extract any string that follows ``"icon": "…"`` in the dashboards
    # module so a future card addition picks up automatically.
    dashboards = APPS_DIR / "dashboards"
    if dashboards.is_dir():
        for path in dashboards.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r'["\']icon["\']\s*:\s*["\']([a-z][a-z0-9-]*)["\']', text):
                names.add(match.group(1))

    # Settings tabs — icon string is hardcoded per tab. The settings
    # tab partial uses ``{% lucide icon %}`` with the name passed via
    # ``with``; the template caller side is already covered by the
    # hardcoded scan, but the constants live in Python too.
    workspaces = APPS_DIR / "workspaces"
    if workspaces.is_dir():
        for path in workspaces.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r'["\']icon["\']\s*:\s*["\']([a-z][a-z0-9-]*)["\']', text):
                names.add(match.group(1))

    return names


def _strip_to_inner(svg: str) -> str:
    """Return the inner markup of an SVG with the outer ``<svg>`` removed."""
    body = _SVG_OPEN_TAG.sub("", svg, count=1)
    body = _SVG_CLOSE_TAG.sub("", body)
    return body.strip()


def build_sprite(names: set[str], icons: dict[str, str]) -> str:
    """Assemble the sprite SVG markup from the given icon set.

    Each ``<symbol>`` carries its own ``fill="none" stroke="currentColor"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round"`` —
    these attributes do NOT inherit from the parent ``<svg>`` across
    the ``<use>`` shadow boundary, so without them the path would
    render as a solid black silhouette (Lucide icons are pure stroke
    outlines). The repetition costs ~20 KB raw on a 260-symbol sprite
    but gzip flattens it to ~1 KB and the alternative (per-call attrs
    on the wrapper ``<svg>``) duplicates them 600-1500× per heavy page.
    """
    symbol_defaults = (
        'fill="none" stroke="currentColor" stroke-width="2" ' 'stroke-linecap="round" stroke-linejoin="round"'
    )
    parts: list[str] = []
    parts.append(
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'style="position:absolute;width:0;height:0;overflow:hidden" '
        'aria-hidden="true">',
    )
    missing: list[str] = []
    for name in sorted(names):
        svg = icons.get(name)
        if svg is None:
            missing.append(name)
            continue
        inner = _strip_to_inner(svg)
        parts.append(f'<symbol id="lu-{name}" viewBox="0 0 24 24" {symbol_defaults}>{inner}</symbol>')
    parts.append("</svg>")
    if missing:
        # Keep the build green but surface the gap loudly so a typo or
        # a rename can be caught at build time.
        print(f"WARNING: {len(missing)} unknown icon names skipped: {sorted(missing)[:10]}…", file=sys.stderr)
    return "".join(parts)


def main() -> None:
    """Build + write the sprite, print a size summary."""
    if not ICONS_JSON.exists():
        raise SystemExit(
            f"icons manifest not found: {ICONS_JSON}\nRun ``make extract-icons`` first.",
        )
    icons = json.loads(ICONS_JSON.read_text())
    names = _collect_hardcoded() | _collect_dynamic()
    sprite = build_sprite(names, icons)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(sprite)
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"wrote {len(names)} symbols → {OUTPUT.relative_to(REPO_ROOT)} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
