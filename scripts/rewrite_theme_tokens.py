#!/usr/bin/env python3
"""One-off rewrite: zinc utility class pairs → semantic theme tokens.

Walks every ``*.html`` under ``templates/`` and ``apps/**/templates/``
and applies the mapping below. Idempotent — running twice changes
nothing on the second pass.

Run from repo root:

    python3 scripts/rewrite_theme_tokens.py

Prints a per-mapping count summary at the end so we can spot any
mapping that hit zero (likely a pattern variant we missed) or any
hit count that looks suspiciously high (likely false positive).
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import re

# Order matters: more specific patterns first so we don't trip a
# shorter substring before its longer cousin gets a chance. Patterns
# are written as regex, escaped where needed.
#
# The ``(?:/\d{1,3})?`` suffix on most patterns absorbs Tailwind's
# opacity modifier (``bg-zinc-100/60``) — the new token inherits the
# same modifier via backreference. We can't drop the modifier because
# muted/60 is a legitimately different visual than plain muted.

OPACITY = r"(?:/\d{1,3})?"

MAPPINGS: list[tuple[str, str]] = [
    # --- Backgrounds (with optional opacity suffix) -----------------
    (rf"\bbg-white{OPACITY} dark:bg-zinc-950(?P<o1>{OPACITY})\b", r"bg-background\g<o1>"),
    (rf"\bbg-white{OPACITY} dark:bg-zinc-900(?P<o1>{OPACITY})\b", r"bg-card\g<o1>"),
    (rf"\bbg-zinc-100(?P<o1>{OPACITY}) dark:bg-zinc-800{OPACITY}\b", r"bg-muted\g<o1>"),
    (rf"\bbg-zinc-50(?P<o1>{OPACITY}) dark:bg-zinc-900{OPACITY}\b", r"bg-subtle\g<o1>"),
    (rf"\bbg-zinc-200(?P<o1>{OPACITY}) dark:bg-zinc-800{OPACITY}\b", r"bg-muted\g<o1>"),
    # hover: counterparts. Note ``hover:`` prefix appears on both
    # light and dark sides in canonical Tailwind ordering.
    (rf"\bhover:bg-zinc-100(?P<o1>{OPACITY}) dark:hover:bg-zinc-800{OPACITY}\b", r"hover:bg-muted\g<o1>"),
    (rf"\bhover:bg-zinc-200(?P<o1>{OPACITY}) dark:hover:bg-zinc-800{OPACITY}\b", r"hover:bg-muted\g<o1>"),
    (rf"\bhover:bg-zinc-50(?P<o1>{OPACITY}) dark:hover:bg-zinc-900{OPACITY}\b", r"hover:bg-subtle\g<o1>"),
    (rf"\bhover:bg-white{OPACITY} dark:hover:bg-zinc-900(?P<o1>{OPACITY})\b", r"hover:bg-card\g<o1>"),
    # --- Text colours -----------------------------------------------
    (r"\btext-zinc-900 dark:text-zinc-100\b", "text-foreground"),
    (r"\btext-zinc-800 dark:text-zinc-200\b", "text-foreground"),
    (r"\btext-zinc-700 dark:text-zinc-300\b", "text-muted-foreground"),
    (r"\btext-zinc-600 dark:text-zinc-400\b", "text-subtle-foreground"),
    (r"\btext-zinc-500 dark:text-zinc-600\b", "text-placeholder-foreground"),
    (r"\bhover:text-zinc-900 dark:hover:text-zinc-100\b", "hover:text-foreground"),
    (r"\bhover:text-zinc-900 dark:hover:text-zinc-200\b", "hover:text-foreground"),
    (r"\bhover:text-zinc-800 dark:hover:text-zinc-200\b", "hover:text-foreground"),
    (r"\bhover:text-zinc-700 dark:hover:text-zinc-300\b", "hover:text-muted-foreground"),
    (r"\bhover:text-zinc-600 dark:hover:text-zinc-400\b", "hover:text-subtle-foreground"),
    # --- Borders ----------------------------------------------------
    (rf"\bborder-zinc-200(?P<o1>{OPACITY}) dark:border-zinc-800{OPACITY}\b", r"border-border\g<o1>"),
    (rf"\bborder-zinc-300(?P<o1>{OPACITY}) dark:border-zinc-700{OPACITY}\b", r"border-border-strong\g<o1>"),
    (r"\bhover:border-zinc-300 dark:hover:border-zinc-700\b", "hover:border-border-strong"),
    (r"\bhover:border-zinc-400 dark:hover:border-zinc-600\b", "hover:border-border-strong"),
]

# Cleanup pass — historical ``dark:text-zinc-N dark:text-zinc-M`` /
# ``dark:bg-zinc-N dark:bg-zinc-M`` duplicates left over from prior
# partial fixes get fused with the new tokens. The first replacement
# above keeps one ``dark:`` declaration; cleanup strips any
# trailing dark-zinc duplicate that follows our new token. Last
# declaration wins in CSS, so leaving them would silently override
# the semantic token in dark mode.
_FG = r"foreground|muted-foreground|subtle-foreground|placeholder-foreground"
_BG = r"background|card|muted|subtle"
_BR = r"border|border-strong"
_OP = r"(?:/\d{1,3})?"

CLEANUP: list[tuple[str, str]] = [
    (rf"\b(text-(?:{_FG}))(\s+dark:text-zinc-\d+)+\b", r"\1"),
    (rf"\b(bg-(?:{_BG}))({_OP})(\s+dark:bg-zinc-\d+{_OP})+\b", r"\1\2"),
    (rf"\b(border-(?:{_BR}))({_OP})(\s+dark:border-zinc-\d+{_OP})+\b", r"\1\2"),
    (rf"\b(hover:text-(?:{_FG}))(\s+dark:hover:text-zinc-\d+)+\b", r"\1"),
    (rf"\b(hover:bg-(?:{_BG}))({_OP})(\s+dark:hover:bg-zinc-\d+{_OP})+\b", r"\1\2"),
]

# Second-wave mappings — pairs that the templates were already using
# *inconsistently* (e.g. ``text-zinc-500 dark:text-zinc-400`` — neither
# placeholder nor subtle in our canon) plus lone zinc utilities that
# should become theme-aware. Run after the canonical CLEANUP so we
# don't double-tag anything.
#
# Lone ``text-zinc-500`` → ``text-placeholder-foreground``: in our
# palette ``zinc-500`` is the canonical "label / metadata" colour
# regardless of theme intent, and the new token's dark side is
# ``zinc-600``, only a half-step darker. Use negative-lookahead to
# skip cases already followed by ``dark:`` (those got caught by the
# pair mapping or are intentionally one-shade).
STRAGGLERS: list[tuple[str, str]] = [
    (r"\btext-zinc-500 dark:text-subtle-foreground\b", "text-subtle-foreground"),
    (r"\btext-zinc-500 dark:text-zinc-400\b", "text-subtle-foreground"),
    (r"\bhover:text-zinc-900 dark:hover:text-zinc-300\b", "hover:text-foreground"),
    (r"\bhover:text-zinc-700 dark:hover:text-zinc-200\b", "hover:text-foreground"),
    (r"\btext-zinc-500\b(?!\s+dark:)", "text-placeholder-foreground"),
    # Half-pair leftovers: dark side got rewritten in an earlier pass
    # but the light side stayed literal because the regex required
    # ``dark:hover:bg-zinc-800`` and the original was ``zinc-100`` on
    # both sides (a bug from a partial fix).
    (r"\bhover:bg-zinc-100/60 dark:hover:bg-muted/60\b", "hover:bg-muted/60"),
    (r"\bhover:bg-zinc-100/60 dark:hover:bg-muted\b", "hover:bg-muted/60"),
    # Inverted-contrast pair: light=zinc-300 stays bright on white,
    # dark=zinc-700 stays muted on near-black. Same intent as
    # ``border-strong`` (zinc-300/zinc-700), used here as text color.
    (r"\btext-zinc-300 dark:text-zinc-700\b", "text-border-strong"),
    # Empty-slot placeholders (assignee, lead, labels, etc.).
    (r"\bborder-zinc-400 dark:border-zinc-600\b", "border-border-strong"),
    # Toggle switch off-state background.
    (r"\bbg-zinc-300 dark:bg-zinc-700\b", "bg-border-strong"),
    # Status-cell hover-darker shade (planned key).
    (r"\bhover:bg-zinc-200 dark:hover:bg-zinc-700\b", "hover:bg-border-strong"),
    # Card-hover border (project list, task card) — light side was
    # missing originally, now becomes theme-aware.
    (r"\bhover:border-zinc-700\b", "hover:border-border-strong"),
    # Mixed dark-transparent pair on overview member chip.
    (r"\bborder-zinc-200 dark:border-transparent\b", "border-border dark:border-transparent"),
    # Breadcrumb separator left as zinc-700 in light-only context.
    (r"\btext-zinc-700\b(?!\s+dark:)", "text-muted-foreground"),
]


def template_roots(repo: Path) -> list[Path]:
    """Return every directory that may contain Django HTML templates."""
    roots = [repo / "templates"]
    for app_templates in (repo / "apps").glob("*/templates"):
        roots.append(app_templates)
    return [r for r in roots if r.is_dir()]


def main() -> None:
    """Run the rewrite, log per-mapping counts, leave a sane summary."""
    repo = Path(__file__).resolve().parent.parent
    roots = template_roots(repo)
    files = [p for root in roots for p in root.rglob("*.html")]

    counts: Counter[str] = Counter()
    files_changed = 0

    for path in files:
        text = path.read_text()
        new_text = text
        for pattern, replacement in MAPPINGS:
            new_text, n = re.subn(pattern, replacement, new_text)
            if n:
                counts[replacement] += n
        for pattern, replacement in CLEANUP:
            new_text, n = re.subn(pattern, replacement, new_text)
            if n:
                counts["[cleanup duplicates]"] += n
        for pattern, replacement in STRAGGLERS:
            new_text, n = re.subn(pattern, replacement, new_text)
            if n:
                counts[f"[stragglers] {replacement}"] += n
        if new_text != text:
            path.write_text(new_text)
            files_changed += 1

    print(f"\nRewrote {files_changed} files across {len(roots)} template roots.")
    print(f"Total replacements: {sum(counts.values())}\n")
    for token, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  → {token}")


if __name__ == "__main__":
    main()
