import datetime

import pytest

from apps.cycles.models import Cycle
from apps.cycles.services import current_cycle, current_index, cycle_bounds, ensure_cycles
from apps.workspaces.models import Workspace
from apps.workspaces.tests.factories import WorkspaceFactory

pytestmark = pytest.mark.django_db

ANCHOR = "2026-05-04"


def _enable(ws, *, length_weeks=2, start_date=ANCHOR):
    """Turn on cadence for ``ws`` and persist the config."""
    ws.cycle_settings = {
        "enabled": True,
        "length_weeks": length_weeks,
        "start_date": start_date,
    }
    ws.save(update_fields=["cycle_settings"])
    return ws


def test_cycle_bounds_two_week():
    anchor = datetime.date(2026, 5, 4)
    assert cycle_bounds(anchor, 2, 0) == (datetime.date(2026, 5, 4), datetime.date(2026, 5, 17))
    assert cycle_bounds(anchor, 2, 1) == (datetime.date(2026, 5, 18), datetime.date(2026, 5, 31))


def test_current_index_clamps_before_anchor():
    anchor = datetime.date(2026, 5, 4)
    assert current_index(anchor, 2, datetime.date(2026, 5, 4)) == 0
    assert current_index(anchor, 2, datetime.date(2026, 5, 17)) == 0
    assert current_index(anchor, 2, datetime.date(2026, 5, 18)) == 1
    assert current_index(anchor, 2, datetime.date(2026, 5, 1)) == 0


def test_ensure_materializes_current_and_next():
    ws = _enable(WorkspaceFactory())
    current = ensure_cycles(ws, datetime.date(2026, 5, 10))
    assert current.number == 1
    assert current.status == Cycle.ACTIVE
    cycles = list(ws.cycles.order_by("number"))
    assert [c.number for c in cycles] == [1, 2]
    assert cycles[1].status == Cycle.PLANNING


def test_ensure_disabled_returns_none():
    ws = WorkspaceFactory()
    assert ensure_cycles(ws, datetime.date(2026, 5, 10)) is None
    assert ws.cycles.count() == 0


def test_ensure_is_idempotent():
    ws = _enable(WorkspaceFactory())
    today = datetime.date(2026, 5, 10)
    ensure_cycles(ws, today)
    ensure_cycles(ws, today)
    assert ws.cycles.count() == 2


def test_rollover_completes_previous_cycle():
    ws = _enable(WorkspaceFactory())
    ensure_cycles(ws, datetime.date(2026, 5, 10))
    current = ensure_cycles(ws, datetime.date(2026, 5, 20))
    assert current.number == 2
    assert current.status == Cycle.ACTIVE
    c1 = ws.cycles.get(number=1)
    assert c1.status == Cycle.COMPLETED
    assert c1.completed_at is not None
    assert ws.cycles.filter(number=3, status=Cycle.PLANNING).exists()


def test_completed_at_is_frozen():
    ws = _enable(WorkspaceFactory())
    ensure_cycles(ws, datetime.date(2026, 5, 10))
    ensure_cycles(ws, datetime.date(2026, 5, 20))
    first = ws.cycles.get(number=1).completed_at
    assert first is not None
    ensure_cycles(ws, datetime.date(2026, 6, 1))
    assert ws.cycles.get(number=1).completed_at == first


def test_no_backfill_of_pre_load_cycles():
    """Jumping straight into cycle 2 materializes only current + next.

    ``ensure_cycles`` rolls forward from each call; it never backfills
    windows that elapsed before the app was ever opened, so an old
    cycle-1 row simply does not exist.
    """
    ws = _enable(WorkspaceFactory())
    ensure_cycles(ws, datetime.date(2026, 5, 20))
    assert sorted(ws.cycles.values_list("number", flat=True)) == [2, 3]


def test_future_anchor_has_no_active_cycle():
    ws = _enable(WorkspaceFactory(), start_date="2026-06-01")
    current = ensure_cycles(ws, datetime.date(2026, 5, 10))
    assert current.status == Cycle.PLANNING
    assert current_cycle(ws, datetime.date(2026, 5, 10)) is None


def test_days_remaining():
    ws = _enable(WorkspaceFactory())
    ensure_cycles(ws, datetime.date(2026, 5, 10))
    c1 = ws.cycles.get(number=1)
    assert c1.days_remaining(datetime.date(2026, 5, 17)) == 1
    assert c1.days_remaining(datetime.date(2026, 5, 18)) == 0


def test_cycle_config_normalization():
    ws = WorkspaceFactory()
    ws.cycle_settings = {"enabled": True, "length_weeks": 99, "start_date": ANCHOR}
    assert ws.cycle_config()["length_weeks"] == Workspace.CYCLE_MAX_LENGTH_WEEKS
    ws.cycle_settings = {"enabled": True, "length_weeks": 2}
    assert ws.cycle_config()["enabled"] is False
