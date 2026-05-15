import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task


class TaskFactory(DjangoModelFactory):
    class Meta:
        model = Task

    project = factory.SubFactory(ProjectFactory)
    title = factory.Sequence(lambda n: f"Task {n}")
    reporter = factory.SubFactory(UserFactory)
    status = Task.STATUS_TODO
    priority = Task.NO_PRIORITY
