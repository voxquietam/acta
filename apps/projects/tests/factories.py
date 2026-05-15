import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.projects.models import Project, ProjectUpdate
from apps.workspaces.tests.factories import WorkspaceFactory


class ProjectFactory(DjangoModelFactory):
    class Meta:
        model = Project

    workspace = factory.SubFactory(WorkspaceFactory)
    name = factory.Sequence(lambda n: f"Project {n}")
    slug_prefix = factory.Sequence(lambda n: f"P{n:03d}"[:6])
    archived = False


class ProjectUpdateFactory(DjangoModelFactory):
    class Meta:
        model = ProjectUpdate

    project = factory.SubFactory(ProjectFactory)
    author = factory.SubFactory(UserFactory)
    health = ProjectUpdate.ON_TRACK
    body = factory.Faker("sentence")
