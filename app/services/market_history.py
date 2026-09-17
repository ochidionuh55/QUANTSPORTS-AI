"""Per-market history: what each market filter would have returned, and how it did.

**The explorer needed a memory.** Browsing today's card by market answers
"what does the model like today". It cannot answer "does this market actually
work", which is the question a reader asks second and the one that decides
whether they trust the first answer. This module supplies it.

**Scored from the same evidence the rest of the record uses.** Every figure
comes from ``settled_predictions`` — forecasts written before kickoff and
compared against the final score. Nothing is recomputed with hindsight about
which fixtures mattered.

**Published probabilities first, reconstruction only where necessary.** The
1X2 legs, Over 2.5 and both-teams-to-score are stored exactly as they were
published, so those markets are scored against the real forecast. Double
chance and the "under"/"no" sides follow from them by arithmetic. Only the
other goals lines — 1.5 and 3.5 — are absent from storage, and those are
rebuilt from the expected-goals pair through the same scoreline grid the
original analysis used. A rebuilt figure is marked as such rather than
presented as a published one.

**A market with nothing above the bar publishes nothing.** The same rule the
boards follow: an empty day is an empty day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import SettledPrediction
from app.quant.grid import build_grid
from app.quant.markets import probability_of, settles_won
from app.services.queries import MARKET_FILTERS, MARKETS_BY_KEY, MarketFilter

logger = get_logger(__name__)

MIN_PROBABILITY: float = 0.55
"""The bar a fixture must clear to appear under a market.

Matches the threshold the live explorer uses for its counts, so the history
describes the same list the user browses rather than a more forgiving one
chosen to look better.
"""

LIVE = "live"
"""Only live forecasts are scored. Backfilled rows were produced knowing which
fixtures exist and which leagues had coverage, so they are never blended in."""


@dataclass
class MarketOutcome:
    """One fixture's result under one market."""

    fixture_id: str
    home_name: str
    away_name: str
    competition: str | None
    kickoff: datetime
    market: str
    outcome: str
    probability: float
    home_goals: int
    away_goals: int
    won: bool | None
    """``True`` hit, ``False`` missed, ``None`` unresolvable."""

    reconstructed: bool = False
    """Whether the probability was rebuilt from expected goals rather than read
    from what was published."""

    @property
    def scoreline(self) -> str:
        """Return the final score."""
        return f"{self.home_goals}-{self.away_goals}"


@dataclass
class MarketTally:
    """One market's record over a period."""

    key: str
    label: str
    won: int = 0
    played: int = 0
    pending: int = 0
    probabilities: list[float] = field(default_factory=list)

    @property
    def strike_rate(self) -> float | None:
        """Share of resolved fixtures that hit."""
        if not self.played:
            return None
        return self.won / self.played

    @property
    def expected_rate(self) -> float | None:
        """What the forecasts implied, for comparison against the strike rate."""
        if not self.probabilities:
            return None
        return sum(self.probabilities) / len(self.probabilities)

    @property
    def gap(self) -> float | None:
        """How far the outcome ran from expectation."""
        actual = self.strike_rate
        expected = self.expected_rate
        if actual is None or expected is None:
            return None
        return actual - expected


def _stored_probability(
    prediction: SettledPrediction, definition: MarketFilter
) -> tuple[float, bool] | None:
    """Return this market's probability and whether it had to be rebuilt.

    Published values are preferred everywhere they exist. Falling back to the
    grid for a market that was stored would mean scoring a number nobody ever
    saw.
    """
    home = prediction.predicted_home
    draw = prediction.predicted_draw
    away = prediction.predicted_away
    over_25 = prediction.predicted_over_2_5
    btts = prediction.predicted_btts

    direct: dict[str, float | None] = {
        "home": home,
        "draw": draw,
        "away": away,
        "over25": over_25,
        "btts": btts,
        "1x": None if home is None or draw is None else home + draw,
        "12": None if home is None or away is None else home + away,
        "x2": None if draw is None or away is None else draw + away,
        "under25": None if over_25 is None else 1.0 - over_25,
        "nobtts": None if btts is None else 1.0 - btts,
    }

    if definition.key in direct:
        value = direct[definition.key]
        return (float(value), False) if value is not None else None

    # The remaining goals lines were never stored. Rebuild them from the
    # expected-goals pair through the same grid the analysis used.
    lambda_home = prediction.expected_home_goals
    lambda_away = prediction.expected_away_goals
    if lambda_home is None or lambda_away is None:
        return None

    grid = build_grid(float(lambda_home), float(lambda_away))
    probability = probability_of(grid, definition.market, definition.outcome)
    if probability is None:
        return None
    return (float(probability), True)


class MarketHistoryService:
    """Reads per-market historical performance from settled predictions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def available_days(self, limit: int = 30) -> list[date]:
        """Return the dates that have settled forecasts, newest first."""
        cutoff = datetime.now(UTC) - timedelta(days=limit * 2)
        rows = await self._session.execute(
            select(SettledPrediction.kickoff)
            .where(
                SettledPrediction.source == LIVE,
                SettledPrediction.kickoff >= cutoff,
            )
            .order_by(SettledPrediction.kickoff.desc())
        )
        days: list[date] = []
        seen: set[date] = set()
        for (kickoff,) in rows.all():
            moment = kickoff if kickoff.tzinfo else kickoff.replace(tzinfo=UTC)
            day = moment.date()
            if day not in seen:
                seen.add(day)
                days.append(day)
            if len(days) >= limit:
                break
        return days

    async def _predictions_for_day(self, day: date) -> list[SettledPrediction]:
        """Return every live settled forecast kicking off on one date."""
        start = datetime.combine(day, datetime.min.time()).replace(tzinfo=UTC)
        end = start + timedelta(days=1)
        rows = await self._session.execute(
            select(SettledPrediction)
            .where(
                SettledPrediction.source == LIVE,
                SettledPrediction.kickoff >= start,
                SettledPrediction.kickoff < end,
            )
            .order_by(SettledPrediction.kickoff)
        )
        return list(rows.scalars().all())

    async def for_day(
        self, day: date, min_probability: float = MIN_PROBABILITY
    ) -> dict[str, list[MarketOutcome]]:
        """Return every market's qualifying fixtures for one date.

        Keyed by market key. A market absent from the result had nothing above
        the bar that day, which is reported as an empty day rather than filled
        with weaker entries.
        """
        predictions = await self._predictions_for_day(day)
        if not predictions:
            return {}

        results: dict[str, list[MarketOutcome]] = {}
        for prediction in predictions:
            kickoff = prediction.kickoff
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=UTC)

            for definition in MARKET_FILTERS:
                found = _stored_probability(prediction, definition)
                if found is None:
                    continue
                probability, reconstructed = found
                if probability < min_probability:
                    continue

                won = settles_won(
                    definition.market,
                    definition.outcome,
                    prediction.home_goals,
                    prediction.away_goals,
                )
                results.setdefault(definition.key, []).append(
                    MarketOutcome(
                        fixture_id=prediction.provider_event_id,
                        home_name=prediction.home_name,
                        away_name=prediction.away_name,
                        competition=prediction.competition,
                        kickoff=kickoff,
                        market=definition.market,
                        outcome=definition.outcome,
                        probability=probability,
                        home_goals=prediction.home_goals,
                        away_goals=prediction.away_goals,
                        won=won,
                        reconstructed=reconstructed,
                    )
                )

        # Strongest first inside each market, matching how the live explorer
        # orders the same list.
        for entries in results.values():
            entries.sort(key=lambda entry: entry.probability, reverse=True)
        return results

    async def tallies_for_day(
        self, day: date, min_probability: float = MIN_PROBABILITY
    ) -> list[MarketTally]:
        """Return each market's record on one date, strongest sample first."""
        by_market = await self.for_day(day, min_probability=min_probability)

        tallies: list[MarketTally] = []
        for key, entries in by_market.items():
            definition = MARKETS_BY_KEY.get(key)
            if definition is None:
                continue
            tally = MarketTally(key=key, label=definition.label)
            for entry in entries:
                if entry.won is None:
                    tally.pending += 1
                    continue
                tally.played += 1
                tally.probabilities.append(entry.probability)
                if entry.won:
                    tally.won += 1
            tallies.append(tally)

        tallies.sort(key=lambda t: (-t.played, -t.won, t.label))
        return tallies

    async def for_day_market(
        self, day: date, key: str, min_probability: float = MIN_PROBABILITY
    ) -> list[MarketOutcome]:
        """Return one market's qualifying fixtures for one date."""
        by_market = await self.for_day(day, min_probability=min_probability)
        return by_market.get(key, [])

    async def track_record(
        self,
        key: str,
        days: int | None = 30,
        min_probability: float = MIN_PROBABILITY,
        now: datetime | None = None,
    ) -> MarketTally:
        """Summarise one market's performance over a window.

        Args:
            key: A market key from :data:`MARKETS_BY_KEY`.
            days: How far back to look, or ``None`` for everything on record.
            min_probability: The bar a fixture must clear to count.
            now: Injected clock, for tests.
        """
        definition = MARKETS_BY_KEY.get(key)
        tally = MarketTally(key=key, label=definition.label if definition else key)
        if definition is None:
            return tally

        moment = now or datetime.now(UTC)
        statement = select(SettledPrediction).where(SettledPrediction.source == LIVE)
        if days is not None:
            statement = statement.where(
                SettledPrediction.kickoff >= moment - timedelta(days=days)
            )

        rows = await self._session.execute(statement)
        for prediction in rows.scalars().all():
            found = _stored_probability(prediction, definition)
            if found is None:
                continue
            probability, _ = found
            if probability < min_probability:
                continue

            won = settles_won(
                definition.market,
                definition.outcome,
                prediction.home_goals,
                prediction.away_goals,
            )
            if won is None:
                tally.pending += 1
                continue
            tally.played += 1
            tally.probabilities.append(probability)
            if won:
                tally.won += 1

        return tally

    async def all_track_records(
        self, days: int | None = 30, min_probability: float = MIN_PROBABILITY
    ) -> list[MarketTally]:
        """Summarise every market over one window, strongest sample first.

        One pass over the period rather than one query per market: fourteen
        separate scans of the same rows is wasteful when a single read answers
        all of them.
        """
        moment = datetime.now(UTC)
        statement = select(SettledPrediction).where(SettledPrediction.source == LIVE)
        if days is not None:
            statement = statement.where(
                SettledPrediction.kickoff >= moment - timedelta(days=days)
            )

        rows = await self._session.execute(statement)
        predictions = list(rows.scalars().all())

        tallies: dict[str, MarketTally] = {
            definition.key: MarketTally(key=definition.key, label=definition.label)
            for definition in MARKET_FILTERS
        }

        for prediction in predictions:
            for definition in MARKET_FILTERS:
                found = _stored_probability(prediction, definition)
                if found is None:
                    continue
                probability, _ = found
                if probability < min_probability:
                    continue

                tally = tallies[definition.key]
                won = settles_won(
                    definition.market,
                    definition.outcome,
                    prediction.home_goals,
                    prediction.away_goals,
                )
                if won is None:
                    tally.pending += 1
                    continue
                tally.played += 1
                tally.probabilities.append(probability)
                if won:
                    tally.won += 1

        ordered = [t for t in tallies.values() if t.played or t.pending]
        ordered.sort(key=lambda t: (-t.played, -t.won, t.label))
        return ordered


__all__ = [
    "MIN_PROBABILITY",
    "MarketHistoryService",
    "MarketOutcome",
    "MarketTally",
]
