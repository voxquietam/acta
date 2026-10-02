from django.apps import AppConfig


class TasksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tasks"
    label = "tasks"

    def ready(self):
        """Wire the signal that keeps similarity vectors current.

        A signal rather than a call in each writer: tasks are saved from
        the web views, the REST API, the MCP tools, the recurring-task
        generator and the Kaneo import, and a vector that silently stops
        matching its task is worse than no vector at all.
        """
        from django.db.models.signals import post_save

        from apps.tasks.models import Task
        from apps.tasks.similarity import on_task_saved

        post_save.connect(on_task_saved, sender=Task, dispatch_uid="tasks.similarity")
