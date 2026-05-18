#!/usr/bin/env python3
"""Reject multi-line ``{# ... #}`` comments in Django templates.

Django's single-line comment syntax silently leaks literal text into
rendered HTML when the opener ``{#`` and closer ``#}`` aren't on the
same source line. The fix is to use ``{% comment %} ... {% endcomment %}``
for anything spanning more than one line.

Wired in ``.pre-commit-config.yaml`` so every ``.html`` touched in a
commit gets scanned automatically — keeps the rule reflexive even when
the author forgets.
"""

from pathlib import Path
import re
import sys

PATTERN = re.compile(r"\{#[^#]*\n[^#]*#\}", re.MULTILINE)


def main() -> int:
    bad: list[tuple[Path, str]] = []
    for arg in sys.argv[1:]:
        path = Path(arg)
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for match in PATTERN.finditer(text):
            snippet = match.group(0).replace("\n", "\\n")[:120]
            bad.append((path, snippet))

    if not bad:
        return 0

    print("Multi-line {# ... #} comments are not allowed — use", file=sys.stderr)
    print("{% comment %} ... {% endcomment %} instead.\n", file=sys.stderr)
    for path, snippet in bad:
        print(f"  {path}: {snippet}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
