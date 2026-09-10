"""Fixture queries.

A structured filter describing what a user asked for, and a repository that
applies it. Everything user-facing — buttons, commands, natural language —
produces one of these, so the bot never builds SQL and a new filter reaches
every entry point at once.

**Markets are declared, not hardcoded.** Adding "Over 4.5" or a corners market
means one entry in :data:`MARKET_FILTERS`, not a change to the query builder,
the bot, or the parser.

**Filtering happens in Python, not SQL.** Market probabilities live in a JSON
column, and expressing "Over 2.5 above 60%" as portable JSON SQL across
PostgreSQL and SQLite would be fragile for no gain — a day's card is at most a
few hundred rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.competitions import BY_CODE, COMPETITIONS
from app.database.models import StoredAnalysis

COVERAGE_ORDER: Final[dict[str, int]] = {
    "fully_modelled": 3,
    "partially_modelled": 2,
    "data_only": 1,
    "unsupported": 0,
}


@dataclass(frozen=True)
class MarketFilter:
    """A market outcome a user can filter on.

    Attributes:
        key: Stable identifier used in commands and callbacks.
        label: Human name.
        market: Market name as stored in the analysis.
        outcome: Outcome name within that market.
    """

    key: str
    label: str
    market: str
    outcome: str


MARKET_FILTERS: Final[tuple[MarketFilter, ...]] = (
    MarketFilter("home", "Home win", "1X2", "Home"),
    MarketFilter("draw", "Draw", "1X2", "Draw"),
    MarketFilter("away", "Away win", "1X2", "Away"),
    MarketFilter("1x", "Home or draw", "Double chance", "1X (home or draw)"),
    MarketFilter("12", "Home or away", "Double chance", "12 (home or away)"),
    MarketFilter("x2", "Draw or away", "Double chance", "X2 (draw or away)"),
    MarketFilter("over15", "Over 1.5 goals", "Goals", "Over 1.5"),
    MarketFilter("under15", "Under 1.5 goals", "Goals", "Under 1.5"),
    MarketFilter("over25", "Over 2.5 goals", "Goals", "Over 2.5"),
    MarketFilter("under25", "Under 2.5 goals", "Goals", "Under 2.5"),
    MarketFilter("over35", "Over 3.5 goals", "Goals", "Over 3.5"),
    MarketFilter("under35", "Under 3.5 goals", "Goals", "Under 3.5"),
    MarketFilter("btts", "Both teams to score", "Both teams to score", "Yes"),
    MarketFilter("nobtts", "Not both to score", "Both teams to score", "No"),
)

MARKETS_BY_KEY: Final[dict[str, MarketFilter]] = {m.key: m for m in MARKET_FILTERS}


@dataclass
class FixtureQuery:
    """What a user asked to see.

    Every field is optional; an empty query means "today's card".
    """

    competitions: tuple[str, ...] = ()
    """Division codes, e.g. ``("E0", "E1")``."""

    team: str | None = None
    on_date: date | None = None
    after_time: time | None = None
    before_time: time | None = None

    market: str | None = None
    """A key from :data:`MARKETS_BY_KEY`."""

    min_probability: float = 0.0
    min_coverage: str | None = None
    limit: int = 25

    applied: list[str] = field(default_factory=list)
    """Human descriptions of what was applied, so the bot can echo the
    interpretation back. A filter that silently does nothing is worse than one
    that fails loudly."""

    @property
    def is_empty(self) -> bool:
        """Whether this is just today's card."""
        return not (
            self.competitions
            or self.team
            or self.on_date
            or self.after_time
            or self.before_time
            or self.market
            or self.min_coverage
        )

    def describe(self) -> str:
        """Return a one-line summary of the filters in force."""
        return ", ".join(self.applied) if self.applied else "all upcoming fixtures"


class FixtureQueryService:
    """Applies a query to stored analyses."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(
        self, query: FixtureQuery, now: datetime | None = None
    ) -> list[StoredAnalysis]:
        """Return matching fixtures, soonest first."""
        moment = now or datetime.now(UTC)

        statement = select(StoredAnalysis).where(
            StoredAnalysis.kickoff >= moment - timedelta(hours=2)
        )
        if query.competitions:
            names = [BY_CODE[code].name for code in query.competitions if code in BY_CODE]
            if names:
                statement = statement.where(StoredAnalysis.competition.in_(names))

        statement = statement.order_by(StoredAnalysis.kickoff)
        records = list((await self._session.execute(statement)).scalars().all())

        return [r for r in records if self._matches(r, query)][: query.limit]

    def _matches(self, record: StoredAnalysis, query: FixtureQuery) -> bool:
        """Apply the filters that cannot be expressed portably in SQL."""
        kickoff = record.kickoff
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=UTC)

        if query.on_date and kickoff.date() != query.on_date:
            return False
        if query.after_time and kickoff.time() < query.after_time:
            return False
        if query.before_time and kickoff.time() > query.before_time:
            return False

        if query.team:
            needle = query.team.lower()
            haystack = f"{record.home_name} {record.away_name}".lower()
            if needle not in haystack:
                return False

        if query.min_coverage:
            required = COVERAGE_ORDER.get(query.min_coverage, 0)
            if COVERAGE_ORDER.get(record.coverage, 0) < required:
                return False

        if query.market:
            probability = probability_for(record, query.market)
            if probability is None:
                return False
            if probability < query.min_probability:
                return False

        return True


def probability_for(record: StoredAnalysis, market_key: str) -> float | None:
    """Return a fixture's probability for a market key, if published.

    Returns ``None`` when the market was not produced for this fixture, which
    is different from zero: a fixture we could not model has no opinion, and
    treating that as 0% would rank it as confidently against.
    """
    definition = MARKETS_BY_KEY.get(market_key)
    if definition is None:
        return None

    markets = record.markets or {}
    if not isinstance(markets, dict):
        return None
    outcomes = markets.get(definition.market)
    if not isinstance(outcomes, dict):
        return None

    raw = outcomes.get(definition.outcome)
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


MIN_FRAGMENT_LENGTH: Final[int] = 4
"""Shortest fragment allowed to match a competition by substring.

Below this, ordinary words collide with league names: the "2" in "over 2.5"
matched both "Ligue 2" and "2. Bundesliga", turning a goals filter into a
competition filter. Exact division codes are still matched at any length.
"""


def competition_codes_for(name_fragment: str) -> tuple[str, ...]:
    """Return division codes matching a fragment, most specific first.

    An exact name match wins outright. "Premier League" otherwise matches both
    England and Russia, and returning both applies a filter the user did not
    ask for.
    """
    needle = name_fragment.strip().lower()
    if not needle:
        return ()

    exact_code = tuple(c.code for c in COMPETITIONS if needle == c.code.lower())
    if exact_code:
        return exact_code

    exact_name = tuple(c.code for c in COMPETITIONS if needle == c.name.lower())
    if exact_name:
        return exact_name

    if len(needle) < MIN_FRAGMENT_LENGTH:
        return ()

    exact_country = tuple(c.code for c in COMPETITIONS if needle == c.country.lower())
    if exact_country:
        return exact_country

    return tuple(
        c.code for c in COMPETITIONS if needle in c.name.lower() or needle in c.country.lower()
    )
