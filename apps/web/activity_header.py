"""The header of My Activity: a window, what it held, and its rhythm.

The feed below it answers "what did I do"; this answers "what came of
it". Five counts over a chosen window, a neutral comparison with the
window before, and one bar per day.

Descriptive, never evaluative. There is no target to hit here and no
good or bad number, so the delta is grey whichever way it points and
nothing is coloured by whether it rose. A personal page that grades the
person reading it stops being read.

See docs/decisions/0016-dashboards.md for the surface this borrows its
shape from; unlike the dashboard, nothing here is about the team.
"""

import datetime

from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from apps.tasks.models import Task

#: The windows the page offers, longest last.
RANGE_DAYS = {
    "7d": 7,
    "14d": 14,
    "30d": 30,
    "90d": 90,
}

#: A month, where the dashboard defaults to a fortnight. One person's
#: trail is thinner than a workspace's, and a fortnight of it can be two
#: busy afternoons and nothing else.
DEFAULT_RANGE = "30d"

#: The tallest bar in the rhythm strip, in pixels.
RHYTHM_HEIGHT = 36

#: The shortest bar that still reads as a bar. Below this a quiet day
#: and a dead day look alike, and the difference is the whole point.
RHYTHM_FLOOR = 3

_STATUS_CHANGED = "task.status_changed"

#: What the five tiles count, and which filter each one opens when
#: clicked. Order is the order they render in.
_TILES = [
    {
        "key": "closed",
        "label": _("Closed"),
        "icon": "circle-check",
        "match": Q(event_type=_STATUS_CHANGED, payload__to=Task.STATUS_DONE),
        "tab": "activity",
        "types": [
            "status",
        ],
    },
    {
        "key": "created",
        "label": _("Created"),
        "icon": "plus",
        "match": Q(event_type="task.created"),
        "tab": "activity",
        "types": [
            "created",
        ],
    },
    {
        "key": "comments",
        "label": _("Comments"),
        "icon": "message-square",
        "match": None,
        "tab": "comments",
        "types": [],
    },
    {
        "key": "review",
        "label": _("Moved to review"),
        "icon": "eye",
        "match": Q(event_type=_STATUS_CHANGED, payload__to=Task.STATUS_IN_REVIEW),
        "tab": "activity",
        "types": [
            "status",
        ],
    },
    {
        "key": "reopened",
        "label": _("Reopened"),
        "icon": "rotate-ccw",
        "match": Q(event_type=_STATUS_CHANGED, payload__from=Task.STATUS_DONE) & ~Q(payload__to=Task.STATUS_DONE),
        "tab": "activity",
        "types": [
            "status",
        ],
    },
]


def resolve_range(key: str) -> dict:
    """Return the window a range key names, falling back to the default.

    Args:
        key: One of :data:`RANGE_DAYS`, or anything at all — an unknown
            key is the default rather than an error, because this
            arrives from a query string.

    Returns:
        ``{"key", "days", "today", "first_day", "since", "before"}``,
        where ``since`` is midnight local on the first day and ``before``
        is the same distance again further back, for the comparison.
    """
    key = key if key in RANGE_DAYS else DEFAULT_RANGE
    days = RANGE_DAYS[key]
    today = timezone.localdate()
    first_day = today - datetime.timedelta(days=days - 1)
    since = timezone.make_aware(datetime.datetime.combine(first_day, datetime.time.min))
    return {
        "key": key,
        "days": days,
        "today": today,
        "first_day": first_day,
        "since": since,
        "before": since - datetime.timedelta(days=days),
    }


def day_heading(day: datetime.date, today: datetime.date) -> str:
    """Return the label a day gets where the feed changes date.

    Args:
        day: The day being labelled.
        today: Reference date.

    Returns:
        ``Today`` / ``Yesterday`` for the two days a reader holds in
        their head, and ``Oct 3 · Fri`` beyond them — the weekday is
        what makes a gap in the feed read as a weekend.
    """
    if day == today:
        return str(_("Today"))
    if day == today - datetime.timedelta(days=1):
        return str(_("Yesterday"))
    return date_format(day, "M j · D")


def _short_day(day: datetime.date, today: datetime.date) -> str:
    """Return the day's label where there is no room for a weekday.

    Args:
        day: The day being labelled.
        today: Reference date.

    Returns:
        ``Today`` / ``Yesterday`` / ``Oct 3``.
    """
    if day == today:
        return str(_("Today"))
    if day == today - datetime.timedelta(days=1):
        return str(_("Yesterday"))
    return date_format(day, "M j")


def _delta(now: int, before: int) -> str:
    """Return the signed difference between two windows, as text.

    Args:
        now: The count inside the chosen window.
        before: The count inside the window before it.

    Returns:
        ``+3`` / ``−3`` / ``±0``, with a real minus sign rather than a
        hyphen. The sign is the whole statement — the page does not say
        whether more was better.
    """
    difference = now - before
    if difference > 0:
        return f"+{difference}"
    if difference < 0:
        return f"−{-difference}"
    return "±0"


def summary_tiles(events, comments, window: dict) -> list[dict]:
    """Return the five counts above the feed, with their comparisons.

    Both windows are counted in one pass per source — two aggregate
    queries in total, whatever the number of tiles — because a tile per
    query is how a header starts costing more than the page under it.

    The querysets are scoped to the viewer, the workspace and the
    project filter, but never to the search box or the type chips: the
    tiles describe the period, not the current view of it. A reader who
    filtered down to one project and then saw the tiles move with every
    chip would have no fixed thing to compare against.

    Args:
        events: The viewer's activity-log rows, unfiltered by type.
        comments: The viewer's comments.
        window: What :func:`resolve_range` returned.

    Returns:
        One dict per tile, in render order, carrying its label, icon,
        value, delta and the filter it opens.
    """
    inside = Q(created_at__gte=window["since"])
    earlier = Q(created_at__gte=window["before"], created_at__lt=window["since"])
    counters = {}
    for tile in _TILES:
        if tile["match"] is None:
            continue
        counters[f"{tile['key']}_now"] = Count("id", filter=tile["match"] & inside)
        counters[f"{tile['key']}_before"] = Count("id", filter=tile["match"] & earlier)
    counted = events.filter(created_at__gte=window["before"]).aggregate(**counters)
    said = comments.filter(created_at__gte=window["before"]).aggregate(
        now=Count("id", filter=inside),
        before=Count("id", filter=earlier),
    )
    tiles = []
    for tile in _TILES:
        if tile["match"] is None:
            now, before = said["now"], said["before"]
        else:
            now, before = counted[f"{tile['key']}_now"], counted[f"{tile['key']}_before"]
        tiles.append(
            {
                "key": tile["key"],
                "label": tile["label"],
                "icon": tile["icon"],
                "value": now,
                "delta": _delta(now, before),
                "before": before,
                "tab": tile["tab"],
                "types": tile["types"],
            },
        )
    return tiles


def rhythm(feed, window: dict) -> dict:
    """Return one bar per day of the window, over whatever the feed shows.

    Unlike the tiles, this follows the filters: it is a picture of the
    list underneath it, so the two have to agree.

    A day that held nothing is a hairline rather than a gap, because a
    row of bars with holes in it reads as missing data, while a row with
    hairlines reads as quiet days — which is what they were.

    Args:
        feed: The queryset the feed is built from, already filtered.
        window: What :func:`resolve_range` returned.

    Returns:
        ``{"bars", "read", "first_day"}``, where ``read`` is the line of
        text beside the heading and each bar carries its height in
        pixels and a tooltip.
    """
    rows = (
        feed.filter(created_at__gte=window["since"])
        .annotate(day=TruncDate("created_at", tzinfo=timezone.get_current_timezone()))
        .order_by()
        .values("day")
        .annotate(count=Count("id"))
    )
    per_day = {row["day"]: row["count"] for row in rows}
    today = window["today"]
    peak = max(per_day.values(), default=0)
    bars = []
    for offset in range(window["days"]):
        day = window["first_day"] + datetime.timedelta(days=offset)
        count = per_day.get(day, 0)
        bars.append(
            {
                "day": day,
                "count": count,
                "height": max(RHYTHM_FLOOR, round(count / peak * RHYTHM_HEIGHT)) if count else 1,
                "tip": "%(day)s · %(events)s"
                % {
                    "day": _short_day(day, today),
                    "events": ngettext("%(count)d event", "%(count)d events", count) % {"count": count},
                },
            },
        )
    return {
        "bars": bars,
        "read": _readout(bars, window),
        "first_day": date_format(window["first_day"], "M j"),
    }


def _readout(bars: list[dict], window: dict) -> str:
    """Return the sentence beside the rhythm heading.

    Built as one message rather than assembled from fragments: a
    translator needs to move the pieces around, and three concatenated
    phrases cannot be reordered into Ukrainian.

    Args:
        bars: What :func:`rhythm` built.
        window: What :func:`resolve_range` returned.

    Returns:
        A translated line naming the total, how many days of the window
        held anything, and the busiest one.
    """
    total = sum(bar["count"] for bar in bars)
    if not total:
        return str(_("no events in this range"))
    busiest = max(bars, key=lambda bar: (bar["count"], bar["day"]))
    return ngettext(
        "%(total)d event · %(active)d of %(days)d days · busiest %(day)s (%(peak)d)",
        "%(total)d events · %(active)d of %(days)d days · busiest %(day)s (%(peak)d)",
        total,
    ) % {
        "total": total,
        "active": sum(1 for bar in bars if bar["count"]),
        "days": window["days"],
        "day": _short_day(busiest["day"], window["today"]),
        "peak": busiest["count"],
    }
