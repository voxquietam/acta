import pytest


@pytest.fixture(autouse=True)
def _isolated_media_root(tmp_path, settings):
    """Redirect MEDIA_ROOT to a per-test tmp dir.

    Keeps file-writing tests from polluting the repo's ``media/`` folder
    and isolates each test's uploads from the next.
    """
    settings.MEDIA_ROOT = str(tmp_path / "media")
