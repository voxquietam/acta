import factory
from factory.django import DjangoModelFactory

from apps.labels.models import Label, LabelGroup
from apps.workspaces.tests.factories import WorkspaceFactory


class LabelGroupFactory(DjangoModelFactory):
    class Meta:
        model = LabelGroup

    workspace = factory.SubFactory(WorkspaceFactory)
    name = factory.Sequence(lambda n: f"Group {n}")
    is_exclusive = False


class LabelFactory(DjangoModelFactory):
    class Meta:
        model = Label

    workspace = factory.SubFactory(WorkspaceFactory)
    name = factory.Sequence(lambda n: f"label-{n}")
    color = "#888888"
