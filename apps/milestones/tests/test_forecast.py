"""The forecast, tested on series whose answer is known by hand.

A simulation is easy to write and easy to get quietly wrong, so these
feed it days whose shape dictates the outcome: a steady team, a bursty
one with the same average, a team that has stopped, and a milestone too
young to say anything about. The point of the method is that the first
two get different answers, and the test that would have caught the old
average is the one that pins that.
"""

import datetime

import pytest

from apps.milestones import forecast

TODAY = datetime.date(2026, 10, 7)


def steady(per_day=2, days=forecast.WINDOW_DAYS):
    """A team closing the same amount every day."""
    return [per_day] * days


def bursty(total=56, days=forecast.WINDOW_DAYS):
    """The same total, delivered in four bursts and otherwise nothing."""
    series = [0] * days
    for index in range(4):
        series[index * 7] = total // 4
    return series


def run(closes, remaining, target_in=30, history_days=forecast.WINDOW_DAYS):
    """Forecast with the fixtures' defaults."""
    return forecast.forecast(
        closes=closes,
        remaining=remaining,
        history_days=history_days,
        today=TODAY,
        target=TODAY + datetime.timedelta(days=target_in),
        seed=1,
    )


class TestWhenItRefusesToAnswer:
    """Saying nothing is a valid answer and the old code could not."""

    def test_nothing_left_is_not_a_forecast(self):
        assert run(steady(), remaining=0)["state"] == "done"

    def test_too_few_closes_is_not_enough_to_replay(self):
        result = run([1] * 5 + [0] * 23, remaining=10)

        assert result["state"] == "thin"
        assert result["closed"] == 5

    def test_a_young_milestone_is_not_enough_to_replay(self):
        """Ten closes in two days says nothing about the next month."""
        result = run(steady(per_day=5, days=28), remaining=10, history_days=2)

        assert result["state"] == "thin"
        assert result["window_days"] == 2

    def test_the_threshold_is_the_threshold(self):
        assert run([1] * 10 + [0] * 18, remaining=10)["state"] == "ready"
        assert run([1] * 9 + [0] * 19, remaining=10)["state"] == "thin"


class TestTheShapeOfTheHistoryChangesTheAnswer:
    """The whole reason for the method: two teams, one average."""

    def test_steady_and_bursty_agree_on_the_middle(self):
        """56 closed either way, 28 left — about a fortnight for both."""
        even = run(steady(per_day=2), remaining=28)
        spiky = run(bursty(total=56), remaining=28)

        assert 12 <= (even["p50"] - TODAY).days <= 16
        assert 12 <= (spiky["p50"] - TODAY).days <= 18

    def test_but_not_on_the_risk(self):
        """The bursty team's 85th percentile sits further out.

        Same throughput, different reliability — which is exactly what an
        average cannot say and the only thing worth saying.
        """
        even = run(steady(per_day=2), remaining=28)
        spiky = run(bursty(total=56), remaining=28)

        assert (spiky["p85"] - TODAY).days > (even["p85"] - TODAY).days

    def test_a_steady_team_is_nearly_certain_of_a_date_it_clears(self):
        result = run(steady(per_day=2), remaining=10, target_in=30)

        assert result["chance"] >= 99

    def test_and_has_no_chance_of_one_it_cannot(self):
        result = run(steady(per_day=1), remaining=60, target_in=7)

        assert result["chance"] == 0


class TestTheDateItself:
    """What the percentages are measured against."""

    def test_a_date_already_gone_is_past_whatever_the_pace(self):
        result = run(steady(per_day=5), remaining=3, target_in=-4)

        assert result["passed"] is True
        assert result["chance"] == 0
        assert forecast.reading(result) == "unlikely"

    def test_p85_is_never_before_p50(self):
        result = run(bursty(), remaining=40)

        assert result["p85"] >= result["p50"]

    @pytest.mark.parametrize(
        ("chance", "expected"),
        [
            (95, "likely"),
            (70, "likely"),
            (69, "coin"),
            (31, "coin"),
            (30, "unlikely"),
            (4, "unlikely"),
        ],
    )
    def test_the_three_readings_split_at_70_and_30(self, chance, expected):
        result = {"state": "ready", "chance": chance, "passed": False}

        assert forecast.reading(result) == expected


class TestItIsTheSameAnswerTwice:
    """A refresh must not shuffle the numbers under the reader."""

    def test_the_seed_pins_the_result(self):
        first = run(bursty(), remaining=40)
        second = run(bursty(), remaining=40)

        assert (first["p50"], first["p85"], first["chance"]) == (
            second["p50"],
            second["p85"],
            second["chance"],
        )

    def test_a_different_seed_is_allowed_to_differ_a_little(self):
        """Not a contract — a guard that the seed is actually used."""
        kwargs = dict(
            closes=bursty(),
            remaining=40,
            history_days=forecast.WINDOW_DAYS,
            today=TODAY,
            target=TODAY + datetime.timedelta(days=30),
        )
        one = forecast.forecast(seed=1, **kwargs)
        two = forecast.forecast(seed=99, **kwargs)

        assert abs(one["chance"] - two["chance"]) <= 5


class TestAStalledMilestone:
    """Nothing closing is the case the old floor invented a date for."""

    def test_a_dead_month_runs_out_of_patience_rather_than_guessing(self):
        """The old code divided by a 0.05 floor and printed 865 days."""
        result = run([0] * forecast.WINDOW_DAYS, remaining=46)

        assert result["state"] == "thin"

    def test_a_nearly_dead_one_says_the_date_is_hopeless(self):
        """Ten closes early on, then three silent weeks."""
        result = run([2] * 5 + [0] * 23, remaining=46, target_in=20)

        assert result["state"] == "ready"
        assert result["chance"] == 0
        assert (result["p85"] - TODAY).days > 60


class TestBucketing:
    """Turning close dates into the window the replay walks."""

    def test_each_day_lands_in_its_own_bucket_with_today_last(self):
        done = {
            1: TODAY,
            2: TODAY,
            3: TODAY - datetime.timedelta(days=1),
            4: TODAY - datetime.timedelta(days=27),
        }

        counts = forecast.daily_closes(done, TODAY)

        assert len(counts) == forecast.WINDOW_DAYS
        assert counts[-1] == 2
        assert counts[-2] == 1
        assert counts[0] == 1

    def test_anything_older_than_the_window_is_dropped(self):
        done = {1: TODAY - datetime.timedelta(days=forecast.WINDOW_DAYS)}

        assert sum(forecast.daily_closes(done, TODAY)) == 0

    def test_a_future_close_is_not_history(self):
        done = {1: TODAY + datetime.timedelta(days=1)}

        assert sum(forecast.daily_closes(done, TODAY)) == 0
