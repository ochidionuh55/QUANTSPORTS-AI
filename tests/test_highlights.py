"""Daily highlight tests.

The track record is the product's honesty made checkable. If a selection can be
written after the result, edited once the outcome is known, or invented to fill
an empty day, the record produces confidence rather than knowledge — which is
worse than having none.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import HighlightSelection, SettledPrediction, StoredAnalysis
from app.services.highlights import (
    LIVE,
    RECONSTRUCTED,
    HighlightService,
    _settle_outcome,
)

NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)


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


async def _analysis(
    session: AsyncSession,
    fixture_id: str = "1",
    hours: int = 6,
    draw: str = "0.62",
    coverage: str = "fully_modelled",
) -> StoredAnalysis:
    """Insert an analysis strong enough to qualify."""
    home = (1 - float(draw)) / 2
    record = StoredAnalysis(
        provider_name="api_football",
        provider_event_id=fixture_id,
        home_name=f"Alpha{fixture_id}",
        away_name=f"Bravo{fixture_id}",
        competition="Championship",
        kickoff=NOW + timedelta(hours=hours),
        coverage=coverage,
        markets={
            "1X2": {
                "Home": str(home),
                "Draw": draw,
                "Away": str(home),
            }
        },
        market_probabilities={
            "home": str(home),
            "draw": draw,
            "away": str(home),
        },
        home_stats={"matches": 45},
        away_stats={"matches": 45},
        components_used=["poisson", "elo", "form"],
        components_dropped=[],
        computed_at=NOW,
    )
    session.add(record)
    await session.flush()
    return record


class TestOutcomeSettlement:
    """Every published outcome resolves from the scoreline."""

    @pytest.mark.parametrize(
        ("outcome", "home", "away", "expected"),
        [
            ("Home", 2, 1, "won"),
            ("Home", 1, 2, "lost"),
            ("Draw", 1, 1, "won"),
            ("Draw", 2, 1, "lost"),
            ("Away", 0, 3, "won"),
            ("1X (home or draw)", 1, 1, "won"),
            ("1X (home or draw)", 0, 1, "lost"),
            ("X2 (draw or away)", 0, 1, "won"),
            ("Over 2.5", 2, 1, "won"),
            ("Over 2.5", 1, 1, "lost"),
            ("Under 2.5", 1, 1, "won"),
            ("Under 1.5", 1, 0, "won"),
            ("Yes", 1, 1, "won"),
            ("Yes", 3, 0, "lost"),
            ("No", 3, 0, "won"),
        ],
    )
    def test_resolves_from_the_scoreline(
        self, outcome: str, home: int, away: int, expected: str
    ) -> None:
        assert _settle_outcome("market", outcome, home, away) == expected

    def test_unknown_outcome_is_voided_not_guessed(self) -> None:
        """Marking it lost understates the record; won inflates it."""
        assert _settle_outcome("Corners", "Over 9.5", 2, 1) == "void"


class TestSelection:
    """Highlights are recorded before kickoff, or not at all."""

    async def test_records_qualifying_fixtures(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        report = await HighlightService(session).record_daily(now=NOW)

        assert report.recorded >= 1
        stored = (await session.execute(select(HighlightSelection))).scalars().all()
        assert stored
        assert all(s.source == LIVE for s in stored)

    async def test_selection_predates_kickoff(self, session: AsyncSession) -> None:
        """The property that makes the whole record trustworthy."""
        await _analysis(session, "1")
        await HighlightService(session).record_daily(now=NOW)

        selection = (await session.execute(select(HighlightSelection))).scalars().first()
        assert selection is not None
        assert selection.was_recorded_before_kickoff

    async def test_started_fixtures_are_never_selected(self, session: AsyncSession) -> None:
        """A selection made with the match under way is not a prediction."""
        await _analysis(session, "1", hours=-3)
        report = await HighlightService(session).record_daily(now=NOW)

        assert report.considered == 0
        assert report.recorded == 0

    async def test_nothing_is_invented_to_fill_the_day(self, session: AsyncSession) -> None:
        """A feature that must produce something eventually produces
        something worthless."""
        await _analysis(session, "1", draw="0.34", coverage="data_only")
        report = await HighlightService(session).record_daily(now=NOW)

        assert report.recorded == 0

    async def test_rerunning_does_not_duplicate(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        service = HighlightService(session)
        await service.record_daily(now=NOW)
        second = await service.record_daily(now=NOW + timedelta(minutes=30))

        assert second.recorded == 0
        assert second.already_present >= 1
        count = int(
            (
                await session.execute(select(func.count()).select_from(HighlightSelection))
            ).scalar_one()
        )
        assert count <= 3

    async def test_rationale_and_factors_are_stored(self, session: AsyncSession) -> None:
        """ "Why this?" must have a real answer."""
        await _analysis(session, "1")
        await HighlightService(session).record_daily(now=NOW)

        selection = (await session.execute(select(HighlightSelection))).scalars().first()
        assert selection is not None
        assert selection.rationale
        assert "overall" in selection.factors
        assert selection.base_rate
        assert selection.model_version


class TestHighlightSettlement:
    """Results are filled in afterwards; nothing else changes."""

    async def _settled(self, session: AsyncSession, fixture_id: str, home: int, away: int) -> None:
        session.add(
            SettledPrediction(
                provider_name="api_football",
                provider_event_id=fixture_id,
                source="live",
                home_name="Alpha",
                away_name="Bravo",
                competition="Championship",
                kickoff=NOW + timedelta(hours=6),
                coverage="fully_modelled",
                model_version="v1",
                components_used=[],
                predicted_home=0.19,
                predicted_draw=0.62,
                predicted_away=0.19,
                home_goals=home,
                away_goals=away,
                actual_result="draw" if home == away else "home",
                predicted_favourite="draw",
                favourite_won=home == away,
                settled_at=NOW,
            )
        )
        await session.flush()

    async def test_settles_from_the_result(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        service = HighlightService(session)
        await service.record_daily(now=NOW)
        await self._settled(session, "1", 1, 1)

        settled = await service.settle(now=NOW + timedelta(hours=9))
        assert settled == 1

        selection = (await session.execute(select(HighlightSelection))).scalars().first()
        assert selection is not None
        assert selection.status == "won"
        assert selection.home_goals == 1

    async def test_a_loss_is_recorded_as_readily_as_a_win(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        service = HighlightService(session)
        await service.record_daily(now=NOW)
        await self._settled(session, "1", 3, 0)

        await service.settle(now=NOW + timedelta(hours=9))
        selection = (await session.execute(select(HighlightSelection))).scalars().first()
        assert selection is not None
        assert selection.status == "lost"

    async def test_unfinished_matches_stay_pending(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        service = HighlightService(session)
        await service.record_daily(now=NOW)

        assert await service.settle(now=NOW + timedelta(hours=6)) == 0


class TestTrackRecord:
    """Performance is reported with its sample size, always."""

    async def _one(self, session: AsyncSession, fixture_id: str, won: bool) -> None:
        session.add(
            HighlightSelection(
                source=LIVE,
                selection_date=NOW.date(),
                rank=1,
                provider_event_id=fixture_id,
                home_name="Alpha",
                away_name="Bravo",
                competition="Championship",
                kickoff=NOW + timedelta(hours=4),
                market="1X2",
                outcome="Draw",
                probability=0.62,
                base_rate=0.26,
                score=0.6,
                confidence=0.5,
                coverage="fully_modelled",
                model_version="v1",
                components_used=[],
                rationale="because",
                factors={"overall": 0.6},
                recorded_at=NOW,
                status="won" if won else "lost",
                settled_at=NOW,
            )
        )
        await session.flush()

    async def test_reports_wins_over_sample(self, session: AsyncSession) -> None:
        for index in range(10):
            await self._one(session, str(index), won=index < 6)

        record = await HighlightService(session).track_record("All", now=NOW)
        assert record.won == 6
        assert record.settled == 10
        assert record.strike_rate == pytest.approx(0.6)
        assert "6/10" in record.describe()

    async def test_expected_rate_is_reported_for_comparison(self, session: AsyncSession) -> None:
        """Winning at the implied rate is calibration, not edge."""
        for index in range(10):
            await self._one(session, str(index), won=index < 6)

        record = await HighlightService(session).track_record("All", now=NOW)
        assert record.expected_rate == pytest.approx(0.62)

    async def test_no_selections_reports_no_rate(self, session: AsyncSession) -> None:
        record = await HighlightService(session).track_record("All", now=NOW)
        assert record.strike_rate is None
        assert "no settled selections" in record.describe()

    async def test_by_market_breakdown(self, session: AsyncSession) -> None:
        for index in range(4):
            await self._one(session, str(index), won=index < 3)

        record = await HighlightService(session).track_record("All", now=NOW)
        assert record.by_market["1X2"] == (3, 4)


class TestReconstruction:
    """Rebuilt history is honest, and kept apart."""

    async def _prediction(
        self, session: AsyncSession, fixture_id: str, days_ago: int, draw: float
    ) -> None:
        session.add(
            SettledPrediction(
                provider_name="csv_historical",
                provider_event_id=fixture_id,
                source="backfill",
                home_name=f"Alpha{fixture_id}",
                away_name=f"Bravo{fixture_id}",
                competition="Championship",
                kickoff=NOW - timedelta(days=days_ago),
                coverage="fully_modelled",
                model_version="v1",
                components_used=["poisson", "elo", "form"],
                predicted_home=(1 - draw) / 2,
                predicted_draw=draw,
                predicted_away=(1 - draw) / 2,
                market_home=(1 - draw) / 2,
                market_draw=draw,
                market_away=(1 - draw) / 2,
                home_goals=1,
                away_goals=1,
                actual_result="draw",
                predicted_favourite="draw",
                favourite_won=True,
                settled_at=NOW,
            )
        )
        await session.flush()

    async def test_rebuilds_from_settled_predictions(self, session: AsyncSession) -> None:
        for index in range(3):
            await self._prediction(session, str(index), days_ago=index + 1, draw=0.62)

        created = await HighlightService(session).reconstruct(days=30, now=NOW)
        assert created > 0

    async def test_marked_reconstructed_not_live(self, session: AsyncSession) -> None:
        """A selection nobody saw is evidence about the method, not the
        product."""
        await self._prediction(session, "1", days_ago=2, draw=0.62)
        await HighlightService(session).reconstruct(days=30, now=NOW)

        stored = (await session.execute(select(HighlightSelection))).scalars().all()
        assert stored
        assert all(s.source == RECONSTRUCTED for s in stored)

    async def test_live_track_record_excludes_reconstruction(self, session: AsyncSession) -> None:
        await self._prediction(session, "1", days_ago=2, draw=0.62)
        service = HighlightService(session)
        await service.reconstruct(days=30, now=NOW)

        live = await service.track_record("Live", now=NOW, source=LIVE)
        rebuilt = await service.track_record("Rebuilt", now=NOW, source=RECONSTRUCTED)
        assert live.total == 0
        assert rebuilt.total > 0

    async def test_reconstruction_is_settled_immediately(self, session: AsyncSession) -> None:
        """The result is already known, so nothing is left pending."""
        await self._prediction(session, "1", days_ago=2, draw=0.62)
        await HighlightService(session).reconstruct(days=30, now=NOW)

        stored = (await session.execute(select(HighlightSelection))).scalars().all()
        assert all(s.is_settled for s in stored)

    async def test_rerunning_does_not_duplicate(self, session: AsyncSession) -> None:
        await self._prediction(session, "1", days_ago=2, draw=0.62)
        service = HighlightService(session)
        first = await service.reconstruct(days=30, now=NOW)
        second = await service.reconstruct(days=30, now=NOW)

        assert first > 0
        assert second == 0
