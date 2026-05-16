import hashlib

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """Custom user model.

    Extension of :class:`AbstractUser` declared from day one so future
    fields (theme preference, display-name overrides) can be added
    without a destructive migration. Currently carries a per-user UI
    language preference; see docs/decisions/0018-i18n.md.
    """

    language = models.CharField(
        max_length=8,
        blank=True,
        choices=settings.LANGUAGES,
        help_text="Preferred UI language. Overrides browser cookie and Accept-Language",
    )

    class Meta(AbstractUser.Meta):
        verbose_name = _("User")
        verbose_name_plural = _("Users")

    @property
    def display_name(self) -> str:
        """Human-readable name shown across the UI.

        Returns ``First Last`` when either name field is populated,
        otherwise falls back to ``username``. Usernames stay reserved
        for ``@mention`` autocomplete and form-value identifiers.
        """
        full = self.get_full_name()
        return full or self.username

    @property
    def avatar_color(self) -> str:
        """Deterministic HSL colour for the no-photo avatar circle.

        Hashed from ``username`` (immutable identifier) so every page
        renders the same colour for a given user and renaming the
        display name doesn't shift the palette. Saturation / lightness
        are tuned so white initials stay readable on top.
        """
        digest = hashlib.md5(self.username.encode("utf-8")).hexdigest()
        hue = int(digest[:6], 16) % 360
        return f"hsl({hue}, 60%, 40%)"
