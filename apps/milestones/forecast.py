"""When a set of work will be finished, answered as a distribution.

The old answer was an average: tasks closed divided by days elapsed,
remainder divided by that rate, a date printed. It read as a promise it
could not keep — teams close work in bursts, and the same average hides
"give or take three days" and "give or take three months".

This replays the team's own recent days instead. Each trial walks
forward a day at a time, drawing a day at random from the last four
weeks of closes and subtracting what was closed on it, until the work
runs out. Ten thousand trials give ten thousand finishing dates, and the
useful numbers fall out of the pile: the date half of them beat, the
date 85 of 100 beat, and the share that land on or before the target.

Zeros matter and are kept. A quiet weekend is two days in seven that
close nothing, and a method that averaged them away would promise work
on a Sunday.

See docs/decisions/0038-forecasting.md.
"""

import datetime
import random

#: How far back the replay looks. Four weeks is long enough to hold a
#: team's rhythm, including its quiet days, and short enough to forget a
#: quarter that has nothing to do with how they work now.
WINDOW_DAYS = 28

#: Closes needed before a forecast is offered at all.
NEED_CLOSES = 10

#: Days of history needed before a forecast is offered at all. Ten closes
#: in two days says nothing about the next month.
NEED_HISTORY_DAYS = 21

#: Trials per forecast. Two thousand holds the percentiles steady to the
#: day — the determinism test pins that — and keeps the whole simulation
#: inside a few milliseconds, which is what it has to cost on a page that
#: also draws a board.
TRIALS = 2_000

#: Beyond this the verdict is settled and only the day is in question,
#: so the simulation trades samples for speed.
FINE_HORIZON_DAYS = 90

#: A trial that has not finished by here stops. Nothing useful is said
#: about a year out, and an unbounded loop on a stalled milestone is a
#: hung request.
MAX_DAYS = 400

#: Where the three readings part company.
LIKELY_AT = 70
UNLIKELY_AT = 30


def forecast(*, closes, remaining, history_days, today, target, seed) -> dict:
    """Return what the recent past says about finishing the remaining work.

    Args:
        closes: One entry per day of the window, oldest first — how many
            tasks closed on that day. Zeros included and significant.
        remaining: Counted work still unfinished.
        history_days: How long this milestone has held work at all, which
            is not the same as the window: a fortnight-old milestone
            cannot be replayed over four weeks.
        today: Reference date.
        target: The date being aimed at.
        seed: Any integer; the same seed gives the same answer, so a
            refresh does not shuffle the numbers under the reader.

    Returns:
        ``{"state": …}`` and whatever that state carries. ``done`` when
        nothing is left, ``thin`` when there is too little to replay, and
        ``ready`` with ``chance`` / ``p50`` / ``p85`` / ``passed``.
    """
    if remaining <= 0:
        return {"state": "done"}
    closed = sum(closes)
    if closed < NEED_CLOSES or history_days < NEED_HISTORY_DAYS:
        return {
            "state": "thin",
            "closed": closed,
            "window_days": min(history_days, WINDOW_DAYS),
        }
    lengths = _simulate(closes, remaining, seed)
    to_target = (target - today).days
    beat = sum(1 for days in lengths if days <= to_target) if to_target >= 0 else 0
    return {
        "state": "ready",
        "chance": round(beat / len(lengths) * 100),
        "p50": today + datetime.timedelta(days=_percentile(lengths, 0.5)),
        "p85": today + datetime.timedelta(days=_percentile(lengths, 0.85)),
        "passed": to_target < 0,
        "window_days": min(history_days, WINDOW_DAYS),
        "closed": closed,
    }


def _simulate(closes, remaining: int, seed: int) -> list[int]:
    """Run the trials and return how many days each one took, sorted.

    Every trial needs a stream of random days, so they are all drawn in
    one ``choices`` call — that is the part worth doing in C — and each
    trial then walks its own stretch of that stream with a running total.
    Drawing day by day instead cost about a hundred milliseconds, which
    is a page render spent on arithmetic.

    A trial that reaches the end of its stretch without burning the work
    down draws more rather than giving up, so the answer never depends on
    how generous the first guess was.

    Args:
        closes: The window's per-day closes.
        remaining: Work to burn through.
        seed: Seed for the generator.

    Returns:
        Trial lengths in days, ascending.
    """
    rng = random.Random(seed)
    per_day = sum(closes) / len(closes)
    expected = remaining / per_day if per_day else MAX_DAYS
    # Cost is trials × days, so a milestone months out is the expensive
    # one — and the one that needs the least precision, because its
    # verdict is not in doubt. Spend the samples where a day either way
    # changes what the page says.
    trials = TRIALS if expected <= FINE_HORIZON_DAYS else TRIALS // 4
    stride = min(MAX_DAYS, max(16, int(expected * 1.5) + 8))
    stream = rng.choices(closes, k=trials * stride)
    lengths = []
    cursor = 0
    for _trial in range(trials):
        left = remaining
        days = 0
        end = cursor + stride
        while left > 0 and days < MAX_DAYS:
            if cursor == end:
                # This trial is a straggler; give it another stretch.
                stream.extend(rng.choices(closes, k=stride))
                end += stride
            left -= stream[cursor]
            cursor += 1
            days += 1
        cursor = end
        lengths.append(days)
    lengths.sort()
    return lengths


def _percentile(sorted_lengths: list[int], share: float) -> int:
    """Return the trial length that ``share`` of the trials came in under.

    Args:
        sorted_lengths: Trial lengths, ascending.
        share: A fraction between 0 and 1.

    Returns:
        The length in days.
    """
    index = min(len(sorted_lengths) - 1, int(share * len(sorted_lengths)))
    return sorted_lengths[index]


def reading(result: dict) -> str:
    """Return the key the page branches its tone and wording on.

    Args:
        result: What :func:`forecast` returned.

    Returns:
        ``done`` / ``thin`` / ``likely`` / ``coin`` / ``unlikely``. A date
        already behind is ``unlikely`` whatever the arithmetic says —
        there is nothing to be optimistic about once it has gone.
    """
    state = result["state"]
    if state != "ready":
        return state
    if result["passed"] or result["chance"] <= UNLIKELY_AT:
        return "unlikely"
    if result["chance"] >= LIKELY_AT:
        return "likely"
    return "coin"


def daily_closes(done_days: dict, today, window=WINDOW_DAYS) -> list[int]:
    """Bucket the days work was finished into the replay window.

    Args:
        done_days: ``{task_id: date}`` of when each finished task closed.
        today: Reference date.
        window: How many days back to cover.

    Returns:
        One count per day, oldest first, length ``window``.
    """
    counts = [0] * window
    for day in done_days.values():
        offset = (today - day).days
        if 0 <= offset < window:
            counts[window - 1 - offset] += 1
    return counts
