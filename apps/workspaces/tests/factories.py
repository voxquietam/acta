import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.workspaces.models import Workspace, WorkspaceMember


class WorkspaceFactory(DjangoModelFactory):
    class Meta:
        model = Workspace
        skip_postgeneration_save = True

    name = factory.Sequence(lambda n: f"Workspace {n}")
    slug = factory.Sequence(lambda n: f"ws-{n}")
    owner = factory.SubFactory(UserFactory)

    @factory.post_generation
    def seed_owner_membership(self, create, extracted, **kwargs):
        """Auto-create the owner's :class:`WorkspaceMember` row.

        Mirrors what ``WorkspaceSerializer.create`` does so tests using
        the factory don't have to remember it.
        """
        if not create:
            return
        WorkspaceMember.objects.get_or_create(
            user=self.owner,
            workspace=self,
            defaults={"role": WorkspaceMember.OWNER},
        )


class WorkspaceMemberFactory(DjangoModelFactory):
    class Meta:
        model = WorkspaceMember
        django_get_or_create = ("user", "workspace")

    user = factory.SubFactory(UserFactory)
    workspace = factory.SubFactory(WorkspaceFactory)
    role = WorkspaceMember.MEMBER
