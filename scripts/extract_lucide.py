#!/usr/bin/env python3
"""Extract Lucide SVGs from ``node_modules/lucide-static/icons`` into a
committed JSON file (``apps/web/lucide_icons.json``).

Run via ``make extract-icons`` after ``npm install``. The output file
is committed so production / CI deploys don't need ``node_modules``.

Each icon ends up as ``{name: raw-svg-string}``. The template tag
in ``apps/web/templatetags/lucide.py`` reads this once at import
time and serves up SVG markup for the requested name.

Why we extract everything instead of a curated subset:
the file is small (~1 MB), Python loads it once at startup, and we
keep the option open for an icon picker without re-extracting on
every addition. The picker UI (when it lands) decides its own
curated whitelist.
"""
import json
from pathlib import Path


def main() -> None:
    """Walk node_modules SVGs, write a single JSON manifest."""
    repo_root = Path(__file__).resolve().parent.parent
    icons_dir = repo_root / "node_modules" / "lucide-static" / "icons"
    if not icons_dir.is_dir():
        raise SystemExit(
            f"icons dir not found: {icons_dir}\n" "Run ``make install-js`` first to populate node_modules.",
        )
    # lucide-static prefixes each SVG with an HTML license comment;
    # strip it so the manifest stores only the ``<svg>`` element. The
    # ISC licence is acknowledged repo-wide via the npm package's own
    # LICENSE entry, no need to repeat it 1500 times in the JSON.
    import re

    license_pat = re.compile(r"^<!--[^>]*-->\s*", re.DOTALL)
    icons: dict[str, str] = {}
    for svg in sorted(icons_dir.glob("*.svg")):
        body = svg.read_text().strip()
        body = license_pat.sub("", body)
        icons[svg.stem] = body
    out = repo_root / "apps" / "web" / "lucide_icons.json"
    out.write_text(json.dumps(icons, separators=(",", ":"), ensure_ascii=False))
    print(f"wrote {len(icons)} icons → {out.relative_to(repo_root)} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
