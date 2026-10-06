import datetime

from django.utils import timezone

import factory
from factory.django import DjangoModelFactory

from apps.milestones.models import Milestone
from apps.workspaces.tests.factories import WorkspaceFactory


class MilestoneFactory(DjangoModelFactory):
    class Meta:
        model = Milestone
        skip_postgeneration_save = True

    workspace = factory.SubFactory(WorkspaceFactory)
    name = factory.Sequence(lambda n: f"Milestone {n}")
    target_date = factory.LazyFunction(lambda: timezone.localdate() + datetime.timedelta(days=14))

    @factory.post_generation
    def projects(self, create, extracted, **kwargs):
        """Attach the given projects to the milestone's scope."""
        if create and extracted:
            self.projects.set(extracted)
