"""Single source of truth for the application version.

The canonical version is ``[project].version`` in ``pyproject.toml``.
This reads it once (cached) so it lives in exactly one place — the MCP
``serverInfo`` and the sidebar version kicker both call ``get_version``
instead of hard-coding the string.
"""

import functools
import importlib.metadata
import pathlib
import tomllib

_PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


@functools.lru_cache(maxsize=1)
def get_version() -> str:
    """Return the Acta version string, e.g. ``"0.2.0"``.

    Prefers installed distribution metadata (when Acta is pip-installed);
    otherwise reads ``pyproject.toml`` from the repo root. Falls back to
    ``"0.0.0"`` if neither is available so callers never crash.

    Returns:
        The semantic version string.
    """
    try:
        return importlib.metadata.version("acta")
    except importlib.metadata.PackageNotFoundError:
        pass
    try:
        return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "0.0.0"
