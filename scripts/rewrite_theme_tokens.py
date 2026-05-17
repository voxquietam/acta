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
        if new_text != text:
            path.write_text(new_text)
            files_changed += 1

    print(f"\nRewrote {files_changed} files across {len(roots)} template roots.")
    print(f"Total replacements: {sum(counts.values())}\n")
    for token, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  → {token}")


if __name__ == "__main__":
    main()
