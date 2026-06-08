import datetime

import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.recurring.models import RecurringTask


class RecurringTaskFactory(DjangoModelFactory):
    class Meta:
        model = RecurringTask

    project = factory.SubFactory(ProjectFactory)
    workspace = factory.LazyAttribute(lambda o: o.project.workspace)
    created_by = factory.SubFactory(UserFactory)
    title = factory.Sequence(lambda n: f"Recurring {n}")
    freq = RecurringTask.Freq.WEEKLY
    interval = 1
    weekdays = factory.LazyFunction(list)
    start_date = factory.LazyFunction(lambda: datetime.date(2026, 1, 1))
