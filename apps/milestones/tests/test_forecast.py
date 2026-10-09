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


def run(
    closes,
    remaining,
    target_in=30,
    history_days=forecast.WINDOW_DAYS,
    arrivals=None,
    arrival_history_days=forecast.WINDOW_DAYS,
):
    """Forecast with the fixtures' defaults, a settled milestone among them."""
    return forecast.forecast(
        closes=closes,
        remaining=remaining,
        history_days=history_days,
        today=TODAY,
        target=TODAY + datetime.timedelta(days=target_in),
        seed=1,
        arrivals=arrivals,
        arrival_history_days=arrival_history_days,
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
        # The window is the window the count was taken over, so the count
        # and the window the page names are one measurement. How long the
        # closes span is the gate, and the gate is what refused here.
        assert result["window_days"] == forecast.WINDOW_DAYS

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

    def test_the_pace_is_the_arithmetic_behind_the_dates(self):
        """The number that lets a reader dismiss a date without dividing."""
        result = run(steady(per_day=2, days=28), remaining=20)

        assert result["closed"] == 56
        assert result["per_day"] == 2.0
        assert result["window_days"] == forecast.WINDOW_DAYS

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


class TestWorkArrivingWhileYouWork:
    """The bucket is not sealed, and the first cut assumed it was."""

    def test_a_sealed_replay_is_the_answer_it_always_was(self):
        """Passing no arrivals must not move a single number."""
        sealed = run(steady(per_day=2), remaining=28)
        explicit = run(steady(per_day=2), remaining=28, arrivals=[0] * forecast.WINDOW_DAYS)

        assert (sealed["p50"], sealed["p85"], sealed["chance"]) == (
            explicit["p50"],
            explicit["p85"],
            explicit["chance"],
        )

    def test_arrivals_push_the_band_out(self):
        """Half the throughput goes on refilling, so it takes twice as long."""
        sealed = run(steady(per_day=2), remaining=28)
        filling = run(steady(per_day=2), remaining=28, arrivals=[1] * forecast.WINDOW_DAYS)

        assert (filling["p50"] - TODAY).days > (sealed["p50"] - TODAY).days

    def test_work_leaving_pulls_it_in(self):
        """Scope moves both ways, and a replay of one way only is a bias."""
        sealed = run(steady(per_day=2), remaining=28)
        shedding = run(steady(per_day=2), remaining=28, arrivals=[-1] * forecast.WINDOW_DAYS)

        assert (shedding["p50"] - TODAY).days < (sealed["p50"] - TODAY).days

    def test_a_milestone_filling_as_fast_as_it_empties_has_no_date(self):
        result = run(steady(per_day=3), remaining=40, arrivals=[3] * forecast.WINDOW_DAYS)

        assert result["diverges"] is True
        assert result["p50"] is None
        assert result["stalled"] == 100
        assert forecast.reading(result) == "never"

    def test_the_day_is_the_unit_of_observation(self):
        """The same totals on both sides, paired differently, disagree.

        56 closed and 56 arrived either way. Aligned, every day nets zero
        and the work never clears; opposed, every other day nets four and
        a four-task remainder goes in one of them. Sampling the two sides
        apart would average both into the same answer, which is the one
        thing a day-level replay is for.
        """
        aligned = run([4, 0] * 14, remaining=4, arrivals=[4, 0] * 14)
        opposed = run([4, 0] * 14, remaining=4, arrivals=[0, 4] * 14)

        assert forecast.reading(aligned) == "never"
        assert (opposed["p50"] - TODAY).days <= 3

    def test_both_paces_are_reported_so_the_verdict_is_checkable(self):
        result = run(steady(per_day=2), remaining=40, arrivals=[1] * forecast.WINDOW_DAYS)

        assert result["per_day"] == 2.0
        assert result["arrive_per_day"] == 1.0

    def test_a_sealed_replay_has_no_arrival_pace_to_report(self):
        """``None``, not zero: nothing was measured, rather than nothing moved."""
        assert run(steady(), remaining=10)["arrive_per_day"] is None


class TestTheHorizonIsNotAnAnswer:
    """``MAX_DAYS`` was a guard that got read back as a date."""

    def test_a_run_that_never_finished_is_not_a_run_that_took_400_days(self):
        """1 000 left at one a day: every run hits the horizon.

        The old code appended ``MAX_DAYS`` to the same list as the honest
        finishes, so the percentiles came back as a date 400 days out —
        a number produced by the guard, not by anything that happened.
        """
        result = run([1] * forecast.WINDOW_DAYS, remaining=1000)

        assert result["p50"] is None
        assert result["p85"] is None
        assert result["stalled"] == 100
        assert result["diverges"] is True
        assert forecast.reading(result) == "never"

    def test_a_partial_stall_keeps_its_median_and_counts_the_rest(self):
        """Half the runs can land while some never do, and both get said."""
        result = run([5] * 7 + [0] * 21, remaining=80, arrivals=[1] * forecast.WINDOW_DAYS)

        assert result["p50"] is not None
        assert result["p85"] is None
        assert result["diverges"] is False
        assert 0 < result["stalled"] < 50

    def test_the_percentile_is_over_every_run_not_the_ones_that_landed(self):
        """Otherwise a stalled majority would hand its median to the page."""
        result = run([5] * 7 + [0] * 21, remaining=80, arrivals=[1] * forecast.WINDOW_DAYS)
        landed = 100 - result["stalled"]

        assert landed > 50
        assert landed < 85

    def test_never_outranks_the_arithmetic_about_the_date(self):
        """A date gone is not the headline when there is no date to miss."""
        result = run(steady(per_day=3), remaining=40, arrivals=[3] * 28, target_in=-5)

        assert result["passed"] is True
        assert forecast.reading(result) == "never"


class TestBucketingArrivals:
    """Turning membership spans into the other half of each day."""

    def test_joining_adds_and_leaving_subtracts(self):
        spans = {
            1: [[TODAY - datetime.timedelta(days=3), None]],
            2: [
                [
                    TODAY - datetime.timedelta(days=5),
                    TODAY - datetime.timedelta(days=1),
                ],
            ],
        }

        moves = forecast.daily_arrivals(spans, {}, TODAY)

        assert len(moves) == forecast.WINDOW_DAYS
        assert moves[-4] == 1
        assert moves[-6] == 1
        assert moves[-2] == -1

    def test_arriving_and_closing_the_same_day_cancel(self):
        """It gained a task and finished it; the remainder never moved."""
        day = TODAY - datetime.timedelta(days=2)

        moves = forecast.daily_arrivals({1: [[day, None]]}, {1: day}, TODAY)
        closes = forecast.daily_closes({1: day}, TODAY)

        assert moves[-3] == 1
        assert closes[-3] == 1
        assert [close - arrived for close, arrived in zip(closes, moves)] == [0] * forecast.WINDOW_DAYS

    def test_work_that_arrives_already_done_is_not_work(self):
        closed = TODAY - datetime.timedelta(days=10)
        joined = TODAY - datetime.timedelta(days=4)

        moves = forecast.daily_arrivals({1: [[joined, None]]}, {1: closed}, TODAY)

        assert moves == [0] * forecast.WINDOW_DAYS

    def test_leaving_after_it_closed_takes_nothing_away(self):
        joined = TODAY - datetime.timedelta(days=20)
        closed = TODAY - datetime.timedelta(days=10)
        left = TODAY - datetime.timedelta(days=3)

        moves = forecast.daily_arrivals({1: [[joined, left]]}, {1: closed}, TODAY)

        assert moves[-21] == 1
        assert moves[-4] == 0

    def test_outside_the_window_is_not_history(self):
        old = TODAY - datetime.timedelta(days=forecast.WINDOW_DAYS)
        ahead = TODAY + datetime.timedelta(days=1)

        moves = forecast.daily_arrivals({1: [[old, None]], 2: [[ahead, None]]}, {}, TODAY)

        assert moves == [0] * forecast.WINDOW_DAYS

    def test_points_move_instead_of_counts_when_the_replay_weighs_them(self):
        day = TODAY - datetime.timedelta(days=2)

        moves = forecast.daily_arrivals({7: [[day, None]]}, {}, TODAY, weights={7: 5})

        assert moves[-3] == 5

    def test_filling_the_milestone_is_not_the_scope_growing(self):
        """That work is the remainder; drawing its days counts it twice.

        Without this, a milestone filled with forty tasks a fortnight ago
        reads as a milestone that takes in forty tasks on a typical day,
        and no amount of throughput converges against it.
        """
        opened = TODAY - datetime.timedelta(days=14)
        later = TODAY - datetime.timedelta(days=3)
        spans = {
            1: [[opened, None]],
            2: [[opened, None]],
            3: [[later, None]],
        }

        moves = forecast.daily_arrivals(spans, {}, TODAY, settled=forecast.settled_from(spans, opened))

        assert moves[-15] == 0
        assert moves[-4] == 1

    def test_a_fill_that_took_two_days_is_excluded_on_both(self):
        """The defect that reached production: only day one was excluded.

        Someone opens a milestone and spends two afternoons deciding what
        belongs in it. Counting the second one divides an afternoon of
        planning by four weeks and calls the answer a pace.
        """
        opened = TODAY - datetime.timedelta(days=3)
        second = TODAY - datetime.timedelta(days=2)
        spans = {
            1: [[opened, None]],
            2: [[second, None]],
            3: [[second, None]],
        }

        moves = forecast.daily_arrivals(spans, {}, TODAY, settled=forecast.settled_from(spans, opened))

        assert moves == [0] * forecast.WINDOW_DAYS

    def test_nothing_moves_at_all_while_the_milestone_is_being_filled(self):
        """Pulling work back out mid-fill is the plan being drawn, too."""
        opened = TODAY - datetime.timedelta(days=9)
        spans = {1: [[opened, opened]]}

        moves = forecast.daily_arrivals(spans, {}, TODAY, settled=forecast.settled_from(spans, opened))

        assert moves == [0] * forecast.WINDOW_DAYS

    def test_with_no_opening_day_every_arrival_counts(self):
        day = TODAY - datetime.timedelta(days=6)

        moves = forecast.daily_arrivals({1: [[day, None]]}, {}, TODAY)

        assert moves[-7] == 1


class TestWhereTheFillEnds:
    """Filling is an episode, and the data says how long it ran."""

    def test_the_run_ends_on_the_first_day_that_took_nothing_in(self):
        opened = TODAY - datetime.timedelta(days=10)
        spans = {
            1: [[opened, None]],
            2: [[opened + datetime.timedelta(days=1), None]],
            3: [[opened + datetime.timedelta(days=2), None]],
        }

        assert forecast.settled_from(spans, opened) == opened + datetime.timedelta(days=3)

    def test_coming_back_after_a_pause_is_a_top_up_not_the_fill(self):
        """A day's silence ends it — which is the whole point of catching it."""
        opened = TODAY - datetime.timedelta(days=10)
        spans = {
            1: [[opened, None]],
            2: [[opened + datetime.timedelta(days=4), None]],
        }

        assert forecast.settled_from(spans, opened) == opened + datetime.timedelta(days=1)

    def test_without_an_opening_day_there_is_nothing_to_walk_from(self):
        assert forecast.settled_from({1: [[TODAY, None]]}, None) is None


class TestAMilestoneTooYoungToJudgeItsOwnInflow:
    """Closes can predate the container; arrivals cannot. That asymmetry bites."""

    def test_arrivals_are_dropped_while_the_milestone_is_new(self):
        """Three days of a milestone's life say nothing about the next month."""
        young = run(steady(per_day=3), remaining=40, arrivals=[3] * 28, arrival_history_days=3)
        sealed = run(steady(per_day=3), remaining=40)

        assert young["p50"] == sealed["p50"]
        assert young["arrive_per_day"] is None
        assert forecast.reading(young) != "never"

    def test_and_the_page_is_told_the_scope_moved_unreplayed(self):
        """Quietly falling back to a sealed bucket would be the old lie."""
        young = run(steady(per_day=3), remaining=40, arrivals=[3] * 28, arrival_history_days=3)

        assert young["arrivals_young"] is True

    def test_a_still_milestone_has_nothing_to_warn_about(self):
        young = run(steady(per_day=3), remaining=40, arrivals=[0] * 28, arrival_history_days=3)

        assert young["arrivals_young"] is False

    def test_once_it_has_lived_long_enough_they_count(self):
        grown = run(
            steady(per_day=3),
            remaining=40,
            arrivals=[3] * 28,
            arrival_history_days=forecast.NEED_HISTORY_DAYS,
        )

        assert grown["arrive_per_day"] == 3.0
        assert forecast.reading(grown) == "never"
