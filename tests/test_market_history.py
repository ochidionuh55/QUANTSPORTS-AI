"""Market history: reconstruction, scoring and aggregation.

The property that matters is that a market's record is built from what was
published, scored against what happened, with no path by which a result can
influence the probability it is scored against.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import SettledPrediction
from app.services.market_history import (
    MIN_PROBABILITY,
    MarketHistoryService,
    _stored_probability,
)
from app.services.queries import MARKETS_BY_KEY


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Provide a session against a fresh in-memory schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as active:
        yield active
    await engine.dispose()


def _prediction(
    event_id: str,
    kickoff: datetime,
    home_goals: int,
    away_goals: int,
    *,
    home: float = 0.60,
    draw: float = 0.25,
    away: float = 0.15,
    over_25: float | None = 0.58,
    btts: float | None = 0.52,
    expected_home: float | None = 1.8,
    expected_away: float | None = 1.1,
    source: str = "live",
) -> SettledPrediction:
    """Build a settled prediction with sensible defaults."""
    result = "home" if home_goals > away_goals else ("away" if away_goals > home_goals else "draw")
    return SettledPrediction(
        provider_name="test",
        provider_event_id=event_id,
        home_name="Home FC",
        away_name="Away FC",
        competition="Test League",
        kickoff=kickoff,
        coverage="fully_modelled",
        source=source,
        model_version="test-1",
        components_used=["poisson"],
        predicted_home=home,
        predicted_draw=draw,
        predicted_away=away,
        predicted_over_2_5=over_25,
        predicted_btts=btts,
        expected_home_goals=expected_home,
        expected_away_goals=expected_away,
        home_goals=home_goals,
        away_goals=away_goals,
        actual_result=result,
        predicted_favourite="home",
        favourite_won=result == "home",
        over_2_5_hit=(home_goals + away_goals) > 2.5,
        btts_hit=home_goals > 0 and away_goals > 0,
        settled_at=kickoff + timedelta(hours=2),
    )


class TestStoredProbability:
    """Published figures are preferred; only unstored lines are rebuilt."""

    def test_uses_published_value_for_stored_markets(self) -> None:
        prediction = _prediction("a", datetime.now(UTC), 2, 1, home=0.72)
        found = _stored_probability(prediction, MARKETS_BY_KEY["home"])
        assert found is not None
        probability, reconstructed = found
        assert probability == pytest.approx(0.72)
        assert reconstructed is False

    def test_double_chance_follows_from_published_legs(self) -> None:
        prediction = _prediction("a", datetime.now(UTC), 2, 1, home=0.60, draw=0.25)
        found = _stored_probability(prediction, MARKETS_BY_KEY["1x"])
        assert found is not None
        probability, reconstructed = found
        assert probability == pytest.approx(0.85)
        assert reconstructed is False

    def test_under_is_complement_of_published_over(self) -> None:
        prediction = _prediction("a", datetime.now(UTC), 1, 0, over_25=0.58)
        found = _stored_probability(prediction, MARKETS_BY_KEY["under25"])
        assert found is not None
        probability, _ = found
        assert probability == pytest.approx(0.42)

    def test_unstored_goals_line_is_rebuilt_and_marked(self) -> None:
        prediction = _prediction("a", datetime.now(UTC), 2, 1)
        found = _stored_probability(prediction, MARKETS_BY_KEY["over15"])
        assert found is not None
        probability, reconstructed = found
        assert 0.0 < probability < 1.0
        assert reconstructed is True

    def test_returns_none_without_expected_goals(self) -> None:
        prediction = _prediction(
            "a", datetime.now(UTC), 2, 1, expected_home=None, expected_away=None
        )
        assert _stored_probability(prediction, MARKETS_BY_KEY["over15"]) is None


@pytest.mark.asyncio
class TestMarketHistory:
    """Day views and aggregates."""

    async def test_qualifying_fixtures_are_scored_against_the_result(
        self, session: AsyncSession
    ) -> None:
        kickoff = datetime.now(UTC) - timedelta(days=1)
        # Home at 72%, and the home side won.
        session.add(_prediction("won", kickoff, 3, 0, home=0.72))
        # Home at 70%, and the home side lost.
        session.add(_prediction("lost", kickoff, 0, 2, home=0.70))
        await session.flush()

        service = MarketHistoryService(session)
        outcomes = await service.for_day_market(kickoff.date(), "home")

        assert len(outcomes) == 2
        by_id = {o.fixture_id: o for o in outcomes}
        assert by_id["won"].won is True
        assert by_id["won"].scoreline == "3-0"
        assert by_id["lost"].won is False

    async def test_below_the_bar_is_excluded(self, session: AsyncSession) -> None:
        kickoff = datetime.now(UTC) - timedelta(days=1)
        session.add(_prediction("weak", kickoff, 1, 0, home=MIN_PROBABILITY - 0.05))
        await session.flush()

        outcomes = await MarketHistoryService(session).for_day_market(kickoff.date(), "home")
        assert outcomes == []

    async def test_backfilled_rows_are_never_counted(
        self, session: AsyncSession
    ) -> None:
        kickoff = datetime.now(UTC) - timedelta(days=1)
        session.add(_prediction("backfill", kickoff, 3, 0, home=0.80, source="backfill"))
        await session.flush()

        service = MarketHistoryService(session)
        assert await service.for_day_market(kickoff.date(), "home") == []
        assert await service.available_days() == []

    async def test_tallies_rank_by_sample_then_wins(self, session: AsyncSession) -> None:
        kickoff = datetime.now(UTC) - timedelta(days=1)
        for index in range(3):
            session.add(_prediction(f"f{index}", kickoff, 2, 1, home=0.75))
        await session.flush()

        tallies = await MarketHistoryService(session).tallies_for_day(kickoff.date())
        assert tallies
        top = tallies[0]
        assert top.played >= 3
        assert top.strike_rate is not None

    async def test_track_record_aggregates_across_days(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        session.add(_prediction("d1", now - timedelta(days=1), 2, 0, home=0.70))
        session.add(_prediction("d2", now - timedelta(days=2), 2, 0, home=0.70))
        session.add(_prediction("d3", now - timedelta(days=3), 0, 1, home=0.70))
        await session.flush()

        tally = await MarketHistoryService(session).track_record("home", days=30)
        assert tally.played == 3
        assert tally.won == 2
        assert tally.strike_rate == pytest.approx(2 / 3)
        assert tally.expected_rate == pytest.approx(0.70)

    async def test_available_days_are_newest_first(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        for offset in (1, 3, 2):
            session.add(_prediction(f"d{offset}", now - timedelta(days=offset), 2, 1))
        await session.flush()

        days = await MarketHistoryService(session).available_days()
        assert days == sorted(days, reverse=True)
        assert len(days) == 3
