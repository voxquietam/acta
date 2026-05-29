"""Factories for :mod:`apps.activity` test data.

Wave 2 C6 §F4 flagged the absence of an ``ActivityLog`` factory: the
existing test suite calls ``log_event()`` directly, which is correct for
exercising the writer path but heavyweight when a test only needs a
seeded row (e.g. perf tests, filter parity, full-history pagination).
This factory creates the row without touching the ``on_commit`` SSE
broadcast or the diff machinery.
"""

import factory
from factory.django import DjangoModelFactory

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.workspaces.tests.factories import WorkspaceFactory


class ActivityLogFactory(DjangoModelFactory):
    """Build a single :class:`ActivityLog` row.

    Defaults to a ``task.created`` event on a brand-new workspace; pass
    ``workspace=`` / ``actor=`` / ``event_type=`` / ``target_type=`` /
    ``target_id=`` / ``payload=`` to override. The factory never invokes
    :func:`apps.activity.services.log_event`, so it bypasses the SSE
    broadcast and diff capture — tests of those behaviours must still
    call ``log_event`` directly.
    """

    class Meta:
        model = ActivityLog

    workspace = factory.SubFactory(WorkspaceFactory)
    actor = factory.SubFactory(UserFactory)
    target_type = ActivityLog.TARGET_TASK
    target_id = factory.Sequence(lambda n: n + 1)
    event_type = "task.created"
    payload = factory.LazyFunction(dict)
