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

    @factory.post_generation
    def _seed_owner_as_member(self, create, extracted, **kwargs):
        """Auto-add the workspace owner to ``members``.

        Matches the UI flow (``create_project`` view auto-enrolls the
        creator) so the project shows up in their "Mine" tab from the
        get-go. Tests that need a project where the owner is NOT a
        member can pass ``members=[]`` to clear it.
        """
        if not create:
            return
        if extracted is not None:
            self.members.set(extracted)
            return
        owner = getattr(self.workspace, "owner", None)
        if owner is not None:
            self.members.add(owner)


class ProjectUpdateFactory(DjangoModelFactory):
    class Meta:
        model = ProjectUpdate

    project = factory.SubFactory(ProjectFactory)
    author = factory.SubFactory(UserFactory)
    health = ProjectUpdate.ON_TRACK
    body = factory.Faker("sentence")
