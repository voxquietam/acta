"""Factory for :mod:`apps.reactions` test data.

Reactions are polymorphic across ``task`` / ``comment`` /
``project_update``; the factory defaults to a ``task`` target via
:class:`TaskFactory`. Pass ``task=None, comment=...`` or
``project_update=...`` to flip the polymorphic FK — the model's
``CheckConstraint`` enforces exactly-one-target on save.
"""

import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.reactions.models import Reaction
from apps.tasks.tests.factories import TaskFactory


class ReactionFactory(DjangoModelFactory):
    """Build one emoji :class:`Reaction` on a task by default."""

    class Meta:
        model = Reaction

    task = factory.SubFactory(TaskFactory)
    user = factory.SubFactory(UserFactory)
    emoji = "👍"
