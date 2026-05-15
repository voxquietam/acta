import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.comments.models import Comment
from apps.tasks.tests.factories import TaskFactory


class CommentFactory(DjangoModelFactory):
    class Meta:
        model = Comment

    task = factory.SubFactory(TaskFactory)
    author = factory.SubFactory(UserFactory)
    body = factory.Faker("paragraph")
