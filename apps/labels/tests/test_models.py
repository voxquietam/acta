"""Tests for :mod:`apps.labels.models`."""

from django.core.exceptions import ValidationError

import pytest

from apps.labels.models import Label
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestLabelColorValidation:
    """The ``color`` field rejects anything that isn't a 6 or 8 digit
    hex code (with leading #). Empty / placeholder / shortened strings
    must fail ``full_clean`` so the admin form surfaces a validation
    error before the row hits the database."""

    def _label(self, color):
        return Label(workspace=WorkspaceFactory(), name="x", color=color)

    @pytest.mark.parametrize(
        "color",
        [
            "#a855f7",
            "#A855F7",
            "#000000",
            "#ffffff",
            "#a855f7ff",  # RRGGBBAA with full alpha
        ],
    )
    def test_valid_hex(self, color):
        label = self._label(color)
        label.full_clean()  # must not raise

    @pytest.mark.parametrize(
        "color",
        [
            "",
            "#",
            "#aaa",  # shorthand — not accepted
            "#RRGGBB",  # placeholder text from an unfilled form
            "a855f7",  # missing leading #
            "#a855f7z",  # non-hex character
            "rgb(168,85,247)",  # not hex
            "purple",  # CSS name
        ],
    )
    def test_invalid_hex_raises(self, color):
        label = self._label(color)
        with pytest.raises(ValidationError):
            label.full_clean()
