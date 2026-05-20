import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import Notification
from apps.workspaces.tests.factories import WorkspaceFactory


class NotificationFactory(DjangoModelFactory):
    class Meta:
        model = Notification

    recipient = factory.SubFactory(UserFactory)
    workspace = factory.SubFactory(WorkspaceFactory)
    actor = factory.SubFactory(UserFactory)
    kind = Notification.Kind.COMMENT
    preview = factory.Sequence(lambda n: f"preview {n}")
