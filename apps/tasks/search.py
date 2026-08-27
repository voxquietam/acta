"""Forgiving typeahead search over tasks.

Shared by the task-detail link picker (:func:`apps.web.views.task_link_search`)
and the meeting editor's task picker
(:func:`apps.web.views_meetings.meeting_task_search`), which previously carried
two copies of this logic and drifted apart.

The matching rules, in order of how a user actually types:

- **Slug reference** — ``ST-42`` pins prefix *and* number. A half-typed
  ``ST-`` pins just the prefix. These are AND-ed with the rest of the query,
  so ``"ST-42 report"`` means "task 42 of project ST whose title says
  report" rather than "anything in ST, or anything titled report".
- **Free words** — every remaining word must appear in the title *or* in the
  assignee's first / last / user name. AND of words, so order and gaps don't
  matter: ``"звіт двв"`` finds *"…звіту по ДВВ"*.
- **Ambiguous shorthand** — a lone word (``"rep"``), a lone number (``"42"``)
  or a word-then-number pair (``"ST 42"``) could be either a slug or title
  text, so those are OR-ed against both interpretations and separated by
  ranking instead of by filtering.

Results are ranked before truncation. Recency alone used to bury an exact
slug hit under ten freshly-touched tasks, which read to users as "the task
isn't there".
"""

from django.db.models import Case, IntegerField, Q, TextField, Value, When
from django.db.models.functions import Cast

# Task numbers are integers, but a typeahead sees them mid-typing: "AUD-16"
# on the way to "AUD-169". Comparing as text lets a partial number match by
# prefix. ``search_tasks`` annotates this alias, so every clause built here
# may rely on it.
NUMBER_AS_TEXT = "number_str"

RESULT_LIMIT = 25

# Ranking tiers — lower sorts first.
RANK_EXACT_SLUG = 0
RANK_TITLE_PREFIX = 1
RANK_OTHER = 2


def _word_q(word):
    """Build the title-or-assignee clause for a single free-text word.

    Args:
        word: One whitespace-separated token from the query.

    Returns:
        A ``Q`` matching the word against the title or any assignee name part.
    """
    return (
        Q(title__icontains=word)
        | Q(assignee__first_name__icontains=word)
        | Q(assignee__last_name__icontains=word)
        | Q(assignee__username__icontains=word)
    )


def _slug_ref(prefix, num=None):
    """Build the clause for a slug reference such as ``ST-42``.

    Both halves match by prefix, because a typeahead query is usually still
    being typed: ``AUD-16`` has to surface ``AUD-169`` on the way to it. An
    exact hit is not lost in the crowd — :func:`rank_case` floats it to the
    top rather than filtering the near-misses out.

    Args:
        prefix: Project slug prefix as typed; matched with ``istartswith`` so a
            half-typed key still resolves.
        num: Task number as a digit string, or ``None`` when the user has only
            typed the prefix.

    Returns:
        A ``Q`` matching the referenced task(s).
    """
    clause = Q(project__slug_prefix__istartswith=prefix)
    if num:
        clause &= Q(**{f"{NUMBER_AS_TEXT}__startswith": num})
    return clause


def parse_query(query):
    """Split a raw query into slug references and free-text words.

    Args:
        query: The user's raw search string.

    Returns:
        A tuple ``(slug_refs, words)`` where ``slug_refs`` is a list of
        ``(prefix, number_string_or_None)`` pairs parsed from unambiguous
        ``PREFIX-`` tokens, and ``words`` holds everything else. The number
        stays a string so a partially typed one can match by prefix.
    """
    tokens = query.split()
    slug_refs = []
    words = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        prefix, dash, num = token.rpartition("-")
        # Only a token that actually carries a dash is an unambiguous slug
        # reference. Anything else stays a word and is disambiguated by the
        # OR-ed shorthand clauses in ``build_query``.
        if dash and prefix:
            if num.isdigit():
                slug_refs.append((prefix, num))
            elif not num and i + 1 < len(tokens) and tokens[i + 1].isdigit():
                # "ST- 42" — the number landed in the next token.
                slug_refs.append((prefix, tokens[i + 1]))
                i += 1
            else:
                slug_refs.append((prefix, None))
        else:
            words.append(token)
        i += 1
    return slug_refs, words


def build_query(query):
    """Build the filter ``Q`` for a typeahead query.

    Args:
        query: The user's raw search string; assumed non-empty and stripped.

    Returns:
        A ``Q`` combining slug references (AND) with free-text words (AND),
        plus OR-ed shorthand interpretations for ambiguous input.
    """
    slug_refs, words = parse_query(query)
    tokens = query.split()

    match = Q()
    for prefix, num in slug_refs:
        match &= _slug_ref(prefix, num)
    for word in words:
        match &= _word_q(word)

    # Shorthand that could mean either a slug or title text. OR it in so
    # neither reading is lost; ``rank_case`` puts the slug hit on top.
    if not slug_refs:
        if len(tokens) == 1 and tokens[0].isdigit():
            match |= Q(**{f"{NUMBER_AS_TEXT}__startswith": tokens[0]})
        elif len(tokens) == 1:
            match |= Q(project__slug_prefix__istartswith=tokens[0])
        elif len(tokens) == 2 and tokens[1].isdigit() and not tokens[0].isdigit():
            # "ST 42" — the dash-less spelling of a slug reference.
            match |= _slug_ref(tokens[0], tokens[1])

    return match


def rank_case(query):
    """Build the relevance-ranking annotation for a query.

    An exact slug hit outranks a title that merely starts with the query,
    which in turn outranks an incidental substring match. Ties fall back to
    recency at the call site.

    Args:
        query: The user's raw search string.

    Returns:
        A ``Case`` expression yielding the rank tier as an integer.
    """
    slug_refs, _ = parse_query(query)
    tokens = query.split()
    if not slug_refs and len(tokens) == 2 and tokens[1].isdigit() and not tokens[0].isdigit():
        slug_refs = [
            (tokens[0], tokens[1]),
        ]

    whens = []
    # A bare number matches by prefix too ("16" reaches AUD-169), so give the
    # task actually numbered 16 the top spot.
    if not slug_refs and len(tokens) == 1 and tokens[0].isdigit():
        whens.append(
            When(
                number=int(tokens[0]),
                then=Value(RANK_EXACT_SLUG),
            )
        )
    for prefix, num in slug_refs:
        if num:
            # The filter matches numbers by prefix, so "AUD-16" also returns
            # AUD-169. Rank the literal number first, so typing the whole key
            # puts that exact task at the top of the list.
            whens.append(
                When(
                    Q(project__slug_prefix__iexact=prefix) & Q(number=int(num)),
                    then=Value(RANK_EXACT_SLUG),
                )
            )
    whens.append(
        When(
            title__istartswith=query,
            then=Value(RANK_TITLE_PREFIX),
        )
    )
    return Case(
        *whens,
        default=Value(RANK_OTHER),
        output_field=IntegerField(),
    )


def search_tasks(qs, query, limit=RESULT_LIMIT):
    """Filter, rank and truncate a task queryset for typeahead search.

    Args:
        qs: Base queryset, already scoped to what the user may see.
        query: Raw search string; blank returns the most recent tasks.
        limit: Maximum rows to return.

    Returns:
        A sliced queryset ordered by relevance, then recency.
    """
    query = (query or "").strip()
    if not query:
        return qs.order_by("-updated_at")[:limit]
    return (
        # The text form of ``number`` has to exist before the filter runs —
        # partial numbers ("AUD-16" reaching for AUD-169) match by prefix.
        qs.annotate(
            **{NUMBER_AS_TEXT: Cast("number", TextField())},
        )
        .filter(build_query(query))
        .annotate(
            match_rank=rank_case(query),
        )
        .order_by(
            "match_rank",
            "-updated_at",
        )[:limit]
    )
