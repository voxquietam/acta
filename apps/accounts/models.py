from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


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
