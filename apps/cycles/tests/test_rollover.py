"""Auto roll-over: unfinished tasks follow the team into the next cycle."""

import datetime

import pytest

from apps.cycles.services import ensure_cycles
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory

IN_CYCLE_1 = datetime.date(2026, 5, 10)  # inside cycle 1 (05-04..05-17)
IN_CYCLE_2 = datetime.date(2026, 5, 20)  # inside cycle 2 (05-18..05-31) → cycle 1 done


def _ws(auto_rollover):
    ws = WorkspaceFactory()
    ws.cycle_settings = {
        "enabled": True,
        "length_weeks": 2,
        "start_date": "2026-05-04",
        "auto_rollover": auto_rollover,
    }
    ws.save(update_fields=["cycle_settings"])
    return ws


@pytest.mark.django_db
class TestAutoRollover:

    def _setup_cycle1_tasks(self, ws):
        """Seed cycle 1 with one unfinished + one done + one cancelled task."""
        project = ProjectFactory(workspace=ws)
        ensure_cycles(ws, IN_CYCLE_1)
        c1 = ws.cycles.get(number=1)
        unfinished = TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS, cycle=c1)
        done = TaskFactory(project=project, status=Task.STATUS_DONE, cycle=c1)
        cancelled = TaskFactory(project=project, status=Task.STATUS_CANCELLED, cycle=c1)
        return c1, unfinished, done, cancelled

    def test_rollover_moves_only_unfinished(self):
        ws = _ws(auto_rollover=True)
        c1, unfinished, done, cancelled = self._setup_cycle1_tasks(ws)
        # Advance into cycle 2 → cycle 1 completes → roll-over fires.
        ensure_cycles(ws, IN_CYCLE_2)
        c2 = ws.cycles.get(number=2)
        unfinished.refresh_from_db()
        done.refresh_from_db()
        cancelled.refresh_from_db()
        assert unfinished.cycle_id == c2.id  # moved forward
        assert done.cycle_id == c1.id  # stays (velocity/history)
        assert cancelled.cycle_id == c1.id  # stays

    def test_no_rollover_when_disabled(self):
        ws = _ws(auto_rollover=False)
        c1, unfinished, done, cancelled = self._setup_cycle1_tasks(ws)
        ensure_cycles(ws, IN_CYCLE_2)
        unfinished.refresh_from_db()
        assert unfinished.cycle_id == c1.id  # untouched

    def test_rollover_emits_cycle_changed_event(self):
        from apps.activity.models import ActivityLog

        ws = _ws(auto_rollover=True)
        c1, unfinished, done, cancelled = self._setup_cycle1_tasks(ws)
        ensure_cycles(ws, IN_CYCLE_2)
        assert ActivityLog.objects.filter(
            target_type=ActivityLog.TARGET_TASK,
            target_id=unfinished.id,
            event_type="task.cycle_changed",
        ).exists()

    def test_rollover_is_idempotent(self):
        ws = _ws(auto_rollover=True)
        c1, unfinished, done, cancelled = self._setup_cycle1_tasks(ws)
        ensure_cycles(ws, IN_CYCLE_2)
        c2 = ws.cycles.get(number=2)
        # A second pass at the same date must not move it again / re-fire.
        from apps.activity.models import ActivityLog

        before = ActivityLog.objects.filter(event_type="task.cycle_changed").count()
        ensure_cycles(ws, IN_CYCLE_2)
        after = ActivityLog.objects.filter(event_type="task.cycle_changed").count()
        unfinished.refresh_from_db()
        assert unfinished.cycle_id == c2.id
        assert after == before  # no duplicate roll-over
