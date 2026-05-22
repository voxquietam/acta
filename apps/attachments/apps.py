from django.apps import AppConfig


class AttachmentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.attachments"
    label = "attachments"

    def ready(self) -> None:
        """Wire the file-cleanup signal on app startup."""
        from . import signals  # noqa: F401
