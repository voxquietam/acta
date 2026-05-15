from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Custom user model.

    Empty extension of :class:`AbstractUser` so future fields (theme
    preference, display-name overrides, etc.) can be added without a
    destructive migration. Declared from day one is the recommended
    Django practice; see docs/decisions/0002-auth.md and
    docs/decisions/0014-frontend-architecture.md for the upcoming theme
    preference field.
    """

    pass
