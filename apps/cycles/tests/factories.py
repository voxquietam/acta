import datetime

import factory
from factory.django import DjangoModelFactory

from apps.cycles.models import Cycle
from apps.workspaces.tests.factories import WorkspaceFactory


class CycleFactory(DjangoModelFactory):
    class Meta:
        model = Cycle

    workspace = factory.SubFactory(WorkspaceFactory)
    number = factory.Sequence(lambda n: n + 1)
    start_date = factory.LazyFunction(lambda: datetime.date(2026, 5, 4))
    end_date = factory.LazyFunction(lambda: datetime.date(2026, 5, 17))
    status = Cycle.ACTIVE
