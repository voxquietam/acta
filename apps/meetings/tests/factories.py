import datetime

import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.meetings.models import Meeting
from apps.projects.tests.factories import ProjectFactory


class MeetingFactory(DjangoModelFactory):
    class Meta:
        model = Meeting

    project = factory.SubFactory(ProjectFactory)
    workspace = factory.LazyAttribute(lambda o: o.project.workspace)
    created_by = factory.SubFactory(UserFactory)
    title = factory.Sequence(lambda n: f"Call {n}")
    happened_at = factory.LazyFunction(lambda: datetime.datetime(2026, 6, 1, 14, 0, tzinfo=datetime.timezone.utc))
    duration_minutes = 30
