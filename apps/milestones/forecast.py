"""When a set of work will be finished, answered as a distribution.

The old answer was an average: tasks closed divided by days elapsed,
remainder divided by that rate, a date printed. It read as a promise it
could not keep — teams close work in bursts, and the same average hides
"give or take three days" and "give or take three months".

This replays the team's own recent days instead. Each trial walks
forward a day at a time, drawing a day at random from the last four
weeks and settling that day's account: what was closed on it comes off
the remainder, what joined the milestone on it goes back on. Two
thousand trials give two thousand finishing dates, and the useful
numbers fall out of the pile: the date half of them beat, the date 85 of
100 beat, and the share that land on or before the target.

A day is one observation and is never split. The arrivals drawn are the
arrivals of the day whose closes were drawn, because a day that closed
seven and took in six is evidence about that team, and averaging the two
sides separately would invent a team that had neither.

Zeros matter and are kept. A quiet weekend is two days in seven that
close nothing, and a method that averaged them away would promise work
on a Sunday.

Some milestones take in work faster than they let it out. Those trials
never finish, and the answer says so rather than naming the horizon as
a date.

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

#: Where a trial gives up. A run still unfinished here did not finish:
#: it is counted as a run with no date, never as a run that landed on
#: day 400. Reporting the horizon as an answer is how a milestone that
#: takes in more than it closes used to get a date at all.
MAX_DAYS = 400

#: Where the three readings part company.
LIKELY_AT = 70
UNLIKELY_AT = 30

#: How much of the work — closed and open alike — has to carry an
#: estimate before the replay counts points instead of tasks. Both sides
#: have to clear it: a pace in points over a remainder in tasks is not a
#: ratio of anything. The rest are imputed at the median, so the bar is
#: about having a sample worth taking a median of, not about perfection.
NEED_SIZED = 0.7


def forecast(
    *,
    closes,
    remaining,
    history_days,
    today,
    target,
    seed,
    closed_count=None,
    unit="tasks",
    arrivals=None,
) -> dict:
    """Return what the recent past says about finishing the remaining work.

    Args:
        closes: One entry per day of the window, oldest first — how much
            was closed on that day, in whatever ``unit`` says. Zeros
            included and significant.
        remaining: Work still unfinished, in the same unit.
        history_days: The span the closes cover, which gates whether an
            answer is offered at all — ten closes inside two days say
            nothing about the next month. Not the width of the replay:
            that is always the window, zeros and all.
        today: Reference date.
        target: The date being aimed at.
        seed: Any integer; the same seed gives the same answer, so a
            refresh does not shuffle the numbers under the reader.
        closed_count: How many tasks closed, when that is not what
            ``closes`` holds. The refusal is about having seen enough
            separate pieces of work to replay; ten points could be one
            task, and one task is not a rhythm.
        unit: ``tasks`` or ``points`` — what was counted, for the page
            to say so.
        arrivals: The same window again, holding how much unfinished work
            the milestone took in net of what it gave away that day.
            Paired with ``closes`` by position and never resampled apart
            from it. ``None`` replays a sealed bucket, which is only
            right where there is no membership history to read.

    Returns:
        ``{"state": …}`` and whatever that state carries. ``done`` when
        nothing is left, ``thin`` when there is too little to replay, and
        ``ready`` with ``chance`` / ``p50`` / ``p85`` / ``passed``. A
        percentile is ``None`` when that share of the runs never
        finished, and ``diverges`` says the median run is one of them.
    """
    if remaining <= 0:
        return {"state": "done"}
    closed = sum(closes) if closed_count is None else closed_count
    if closed < NEED_CLOSES or history_days < NEED_HISTORY_DAYS:
        return {
            "state": "thin",
            "closed": closed,
            "window_days": len(closes),
            "unit": unit,
        }
    net = closes if arrivals is None else [close - arrived for close, arrived in zip(closes, arrivals)]
    lengths, stalled = _simulate(net, remaining, seed)
    trials = len(lengths) + stalled
    to_target = (target - today).days
    beat = sum(1 for days in lengths if days <= to_target) if to_target >= 0 else 0
    p50 = _percentile(lengths, trials, 0.5)
    p85 = _percentile(lengths, trials, 0.85)
    return {
        "state": "ready",
        "chance": round(beat / trials * 100),
        "p50": today + datetime.timedelta(days=p50) if p50 is not None else None,
        "p85": today + datetime.timedelta(days=p85) if p85 is not None else None,
        "passed": to_target < 0,
        # The median run not finishing is the sixth thing the page can
        # say, and it is a different statement from "late": no date is
        # being missed, the remainder is not shrinking.
        "diverges": p50 is None,
        "stalled": round(stalled / trials * 100),
        "window_days": len(closes),
        "closed": closed,
        "unit": unit,
        # The pace is the one number that makes every other one on the
        # page checkable: a reader who knows they do not close two a day
        # can dismiss a confident date without doing the division
        # themselves. It is the simulation's own input, closes over the
        # whole window — quiet days included, because the simulation
        # draws those too.
        "per_day": round(sum(closes) / len(closes), 1),
        # The other half of that division, and the whole of the answer
        # when the two are close: a milestone taking in 2.6 a day while
        # closing 2.1 does not have a late date, it has no date.
        "arrive_per_day": None if arrivals is None else round(sum(arrivals) / len(arrivals), 1),
    }


def _simulate(net, remaining: int, seed: int) -> tuple[list[int], int]:
    """Run the trials and return the lengths that finished, plus the rest.

    Every trial needs a stream of random days, so they are all drawn in
    one ``choices`` call — that is the part worth doing in C — and each
    trial then walks its own stretch of that stream with a running total.
    Drawing day by day instead cost about a hundred milliseconds, which
    is a page render spent on arithmetic.

    A trial that reaches the end of its stretch without burning the work
    down draws more rather than giving up, so the answer never depends on
    how generous the first guess was. A trial that reaches ``MAX_DAYS``
    is a different matter: it is over, and it finished nothing. Counting
    its length would make the horizon look like a date.

    Args:
        net: The window's per-day movement — closed minus arrived, which
            is negative on a day the milestone grew.
        remaining: Work to burn through.
        seed: Seed for the generator.

    Returns:
        ``(lengths, stalled)`` — the finishing lengths in days ascending,
        and how many runs never finished.
    """
    rng = random.Random(seed)
    per_day = sum(net) / len(net)
    expected = remaining / per_day if per_day > 0 else MAX_DAYS
    # Cost is trials × days, so a milestone months out is the expensive
    # one — and the one that needs the least precision, because its
    # verdict is not in doubt. Spend the samples where a day either way
    # changes what the page says.
    trials = TRIALS if expected <= FINE_HORIZON_DAYS else TRIALS // 4
    stride = min(MAX_DAYS, max(16, int(expected * 1.5) + 8))
    stream = rng.choices(net, k=trials * stride)
    lengths = []
    stalled = 0
    cursor = 0
    for _trial in range(trials):
        left = remaining
        days = 0
        end = cursor + stride
        while left > 0 and days < MAX_DAYS:
            if cursor == end:
                # This trial is a straggler; give it another stretch.
                stream.extend(rng.choices(net, k=stride))
                end += stride
            left -= stream[cursor]
            cursor += 1
            days += 1
        cursor = end
        if left > 0:
            stalled += 1
        else:
            lengths.append(days)
    lengths.sort()
    return lengths, stalled


def _percentile(sorted_lengths: list[int], trials: int, share: float) -> int | None:
    """Return the trial length that ``share`` of the trials came in under.

    The share is of every trial run, not of the ones that finished. A
    run that never finished is still a run, and it sits past the far end
    of the sorted lengths — so when too many of them stalled, the
    percentile falls off that end and there is no date to report.

    Args:
        sorted_lengths: Finishing lengths, ascending.
        trials: How many trials ran, stalled ones included.
        share: A fraction between 0 and 1.

    Returns:
        The length in days, or ``None`` when that share of the runs did
        not finish inside the horizon.
    """
    index = min(trials - 1, int(share * trials))
    if index >= len(sorted_lengths):
        return None
    return sorted_lengths[index]


def reading(result: dict) -> str:
    """Return the key the page branches its tone and wording on.

    Args:
        result: What :func:`forecast` returned.

    Returns:
        ``done`` / ``thin`` / ``never`` / ``likely`` / ``coin`` /
        ``unlikely``. ``never`` outranks the arithmetic about the date,
        because a milestone whose remainder is not shrinking is not
        missing a date — there is no date to miss. A date already behind
        is ``unlikely`` whatever the pace says: there is nothing to be
        optimistic about once it has gone.
    """
    state = result["state"]
    if state != "ready":
        return state
    if result.get("diverges"):
        return "never"
    if result["passed"] or result["chance"] <= UNLIKELY_AT:
        return "unlikely"
    if result["chance"] >= LIKELY_AT:
        return "likely"
    return "coin"


def daily_closes(done_days: dict, today, window=WINDOW_DAYS, weights=None) -> list:
    """Bucket the days work was finished into the replay window.

    Args:
        done_days: ``{task_id: date}`` of when each finished task closed.
        today: Reference date.
        window: How many days back to cover.
        weights: ``{task_id: points}`` to add instead of one per task,
            when the replay runs on estimates rather than on counts.

    Returns:
        One total per day, oldest first, length ``window``.
    """
    counts = [0] * window
    for task_id, day in done_days.items():
        index = _bucket(day, today, window)
        if index is not None:
            counts[index] += 1 if weights is None else weights.get(task_id, 0)
    return counts


def daily_arrivals(spans: dict, done_days: dict, today, window=WINDOW_DAYS, weights=None, opened=None) -> list:
    """Bucket the unfinished work that joined the milestone, net of what left.

    The scope line on the chart is drawn from these same spans, so this
    is the page counting what it already draws: a day it says the scope
    moved on is a day the replay can draw as a day the scope moved.

    Only unfinished work counts on either side. A task that arrives
    already done adds nothing to burn through, and one that leaves after
    it closed takes nothing away. Work that arrived and closed the same
    day registers on both sides and cancels, which is the honest reading
    of a day that gained a task and finished it.

    Leaving is counted as well as joining, so the replay is symmetric
    with the scope line it mirrors: milestones are topped up and also
    emptied out, and a team that regularly moves work elsewhere would
    otherwise be forecast as though it never did.

    Filling the milestone in the first place is not an arrival. That
    work is the remainder — it is already on the other side of the sum —
    and drawing its day again as a day more work turns up would count it
    twice, which is enough on its own to tell a milestone filled a
    fortnight ago that it will never finish. So the day the scope first
    existed is excluded, and it is excluded by the one line that is not
    arbitrary: before it there was no scope to add to.

    Args:
        spans: ``{task_id: [[joined, left_or_None], ...]}`` — the
            membership history, as :func:`_membership_history` replays it.
        done_days: ``{task_id: date}`` of when each finished task closed.
        today: Reference date.
        window: How many days back to cover.
        weights: ``{task_id: points}`` to move instead of one per task,
            matching whatever :func:`daily_closes` was given.
        opened: The day the milestone first held anything. Arrivals on it
            are the scope being defined, not scope growing. ``None``
            counts every day, which is right only where the caller knows
            the first fill is off the back of the window anyway.

    Returns:
        One net total per day, oldest first, length ``window``. Negative
        on a day the milestone shed more than it took in.
    """
    moves = [0] * window
    for task_id, task_spans in spans.items():
        weight = 1 if weights is None else weights.get(task_id, 0)
        done_day = done_days.get(task_id)
        for joined, left in task_spans:
            first_fill = opened is not None and joined <= opened
            if not first_fill and (done_day is None or done_day >= joined):
                index = _bucket(joined, today, window)
                if index is not None:
                    moves[index] += weight
            if left is not None and (done_day is None or done_day >= left):
                index = _bucket(left, today, window)
                if index is not None:
                    moves[index] -= weight
    return moves


def _bucket(day, today, window: int) -> int | None:
    """Return which slot of the window a day falls in, oldest first.

    Args:
        day: The day to place.
        today: Reference date; the last slot.
        window: How many slots there are.

    Returns:
        The index, or ``None`` when the day is outside the window —
        older than it reaches, or still in the future.
    """
    offset = (today - day).days
    return window - 1 - offset if 0 <= offset < window else None
