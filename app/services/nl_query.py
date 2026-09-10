"""Natural language to structured filters.

Turns "show today's BTTS analysis in the Premier League after 5pm" into a
:class:`FixtureQuery`.

**Rule-based, not a language model, and that is the design.** The parser can
only ever produce a ``FixtureQuery``, whose fields are a fixed set of
filters over already-computed analyses. There is no path from user text to a
model parameter, a probability, or the value-detection flag — so no phrasing
can talk the system into producing a selection it is not allowed to produce.
A generative layer could be added later to *suggest* a query, but it must
still hand back one of these objects rather than reaching past it.

**Unrecognised input is reported, never guessed.** If nothing matches, the
query comes back empty with a note saying so, and the caller shows today's card
rather than silently applying a filter the user did not ask for.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from typing import Final

from app.services.queries import (
    MARKET_FILTERS,
    FixtureQuery,
    competition_codes_for,
)

# Phrases mapped to market keys, longest first so "over 2.5" is not shadowed by
# a bare "over".
MARKET_PHRASES: Final[tuple[tuple[str, str], ...]] = (
    ("both teams to score", "btts"),
    ("both to score", "btts"),
    ("btts", "btts"),
    ("no btts", "nobtts"),
    ("clean sheet", "nobtts"),
    ("over 3.5", "over35"),
    ("under 3.5", "under35"),
    ("over 2.5", "over25"),
    ("under 2.5", "under25"),
    ("over 1.5", "over15"),
    ("under 1.5", "under15"),
    ("home or draw", "1x"),
    ("draw or away", "x2"),
    ("double chance", "1x"),
    ("home win", "home"),
    ("away win", "away"),
    ("home", "home"),
    ("away", "away"),
    ("draw", "draw"),
)

COVERAGE_PHRASES: Final[tuple[tuple[str, str], ...]] = (
    ("fully modelled", "fully_modelled"),
    ("fully modeled", "fully_modelled"),
    ("full coverage", "fully_modelled"),
    ("green", "fully_modelled"),
    ("partial", "partially_modelled"),
)

_TIME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:(after|before|from)\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b"
)
_PERCENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(\d{2,3})\s*%")
_TEAM_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:for|about|team)\s+([a-z][a-z\s'.-]{2,30})", re.IGNORECASE
)

DEFAULT_MIN_PROBABILITY: Final[float] = 0.55
"""Threshold when a market is named without one.

Not zero: "show me BTTS games" means the ones where it is likely, not every
fixture with a BTTS number attached.
"""


def parse(text: str, now: datetime | None = None) -> FixtureQuery:
    """Translate a request into structured filters.

    Args:
        text: What the user typed.
        now: Clock override, for tests.

    Returns:
        A query with ``applied`` describing every filter recognised, so the
        caller can show the user how their words were interpreted.
    """
    moment = now or datetime.now(UTC)
    lowered = " ".join(text.lower().split())
    query = FixtureQuery()

    _apply_date(query, lowered, moment)
    _apply_time(query, lowered)
    _apply_competition(query, lowered)
    _apply_market(query, lowered)
    _apply_coverage(query, lowered)
    _apply_team(query, text)

    if "strongest" in lowered or "best" in lowered or "highlight" in lowered:
        query.min_coverage = query.min_coverage or "partially_modelled"
        query.applied.append("strongest analyses first")

    if not query.applied:
        query.applied.append("no filters recognised — showing everything upcoming")
    return query


def _apply_date(query: FixtureQuery, text: str, now: datetime) -> None:
    """Recognise today and tomorrow."""
    if "tomorrow" in text:
        query.on_date = (now + timedelta(days=1)).date()
        query.applied.append("tomorrow")
    elif "today" in text or "tonight" in text:
        query.on_date = now.date()
        query.applied.append("today")


def _apply_time(query: FixtureQuery, text: str) -> None:
    """Recognise a kickoff time bound."""
    match = _TIME_PATTERN.search(text)
    if match is None:
        return

    direction, hour_text, minute_text, meridiem = match.groups()
    if direction is None:
        return

    hour = int(hour_text)
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23:
        return

    boundary = time(hour=hour, minute=int(minute_text or 0))
    if direction == "before":
        query.before_time = boundary
        query.applied.append(f"kicking off before {boundary:%H:%M}")
    else:
        query.after_time = boundary
        query.applied.append(f"kicking off after {boundary:%H:%M}")


def _apply_competition(query: FixtureQuery, text: str) -> None:
    """Recognise a competition or country by name."""
    for phrase in _competition_phrases(text):
        codes = competition_codes_for(phrase)
        if codes:
            query.competitions = codes
            query.applied.append(f"{phrase.title()} only")
            return


def _competition_phrases(text: str) -> list[str]:
    """Return candidate competition phrases, longest first.

    Longest first so "premier league" is tried before "league", which would
    otherwise match several divisions and pick an arbitrary one.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
    phrases: list[str] = []
    for size in (3, 2, 1):
        for index in range(len(words) - size + 1):
            phrases.append(" ".join(words[index : index + size]))
    return phrases


def _apply_market(query: FixtureQuery, text: str) -> None:
    """Recognise a market and any probability threshold."""
    for phrase, key in MARKET_PHRASES:
        if phrase in text:
            query.market = key
            label = next((m.label for m in MARKET_FILTERS if m.key == key), phrase)

            percent = _PERCENT_PATTERN.search(text)
            if percent:
                query.min_probability = min(int(percent.group(1)) / 100, 0.99)
                query.applied.append(f"{label} above {query.min_probability:.0%}")
            else:
                query.min_probability = DEFAULT_MIN_PROBABILITY
                query.applied.append(f"{label} above {DEFAULT_MIN_PROBABILITY:.0%}")
            return


def _apply_coverage(query: FixtureQuery, text: str) -> None:
    """Recognise a minimum coverage grade."""
    for phrase, grade in COVERAGE_PHRASES:
        if phrase in text:
            query.min_coverage = grade
            query.applied.append(f"{phrase} fixtures only")
            return


def _apply_team(query: FixtureQuery, original: str) -> None:
    """Recognise an explicit team reference.

    Requires a lead-in word — "for Arsenal", "team Arsenal". Guessing that any
    capitalised word is a club would match competitions, countries and ordinary
    sentence starts.
    """
    match = _TEAM_PATTERN.search(original)
    if match is None:
        return
    candidate = match.group(1).strip()
    if competition_codes_for(candidate):
        return
    query.team = candidate
    query.applied.append(f"team matching '{candidate}'")


def suggestions() -> list[str]:
    """Return example queries for the help text."""
    return [
        "today's BTTS analysis",
        "Premier League only",
        "fully modelled games after 5pm",
        "over 2.5 above 60%",
        "Championship tomorrow",
        "for Arsenal",
    ]
