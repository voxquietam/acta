from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Custom user model.

    Empty for now — declared from day one so future extensions (theme
    preference, display name overrides, etc.) do not require a destructive
    migration. See docs/decisions/0014-frontend-architecture.md for the
    upcoming theme preference field.
    """

    pass
