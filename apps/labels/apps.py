from django.apps import AppConfig


class LabelsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.labels"
    label = "labels"

    def ready(self) -> None:
        """Wire signal handlers (default-group seeder on new workspaces)."""
        from . import signals  # noqa: F401 — import registers receivers
