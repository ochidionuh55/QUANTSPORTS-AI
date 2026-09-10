"""Historical backfill tests.

The leakage test is the one that matters. A backfill that lets a fixture inform
its own forecast produces excellent numbers and is worthless, and the mistake
leaves no trace in the output — so it has to be caught here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import SettledPrediction
from app.historical.models import HistoricalMatch, HistoricalTeamRef
from app.services.backfill import BACKFILL_SOURCE, BackfillService
from app.services.settlement import PerformanceService

TEAMS = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")


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


def _season(rounds: int = 6) -> list[HistoricalMatch]:
    """Build a deterministic season with odds on every fixture."""
    matches: list[HistoricalMatch] = []
    index = 0
    for _ in range(rounds):
        for home in TEAMS:
            for away in TEAMS:
                if home == away:
                    continue
                played = date(2023, 8, 1) + timedelta(days=index // 3)
                matches.append(
                    HistoricalMatch(
                        provider_name="csv",
                        external_id=f"bf-{index:05d}",
                        competition_external_id="E1",
                        season="2023/2024",
                        match_date=played,
                        kickoff=datetime(played.year, played.month, played.day, 15, tzinfo=UTC),
                        home_team=HistoricalTeamRef(source_name=home),
                        away_team=HistoricalTeamRef(source_name=away),
                        home_goals=(index % 4),
                        away_goals=((index + 1) % 3),
                        closing_odds={
                            "market_avg": {
                                "H": Decimal("2.10"),
                                "D": Decimal("3.40"),
                                "A": Decimal("3.60"),
                            }
                        },
                    )
                )
                index += 1
    return matches


class TestBackfill:
    """Replays history into permanent settled predictions."""

    async def test_stores_predictions(self, session: AsyncSession) -> None:
        report = await BackfillService(session).backfill(
            _season(), "Championship", reference_bookmaker="market_avg"
        )
        assert report.stored > 0
        count = int(
            (
                await session.execute(select(func.count()).select_from(SettledPrediction))
            ).scalar_one()
        )
        assert count == report.stored

    async def test_marked_as_backfill(self, session: AsyncSession) -> None:
        """Never blended with live results."""
        await BackfillService(session).backfill(
            _season(), "Championship", reference_bookmaker="market_avg"
        )
        rows = (await session.execute(select(SettledPrediction))).scalars().all()
        assert all(r.source == BACKFILL_SOURCE for r in rows)

    async def test_early_fixtures_are_skipped(self, session: AsyncSession) -> None:
        """Before enough history exists, the models return their priors."""
        report = await BackfillService(session).backfill(
            _season(), "Championship", reference_bookmaker="market_avg"
        )
        assert report.skipped.get("insufficient_history", 0) > 0

    async def test_fixtures_without_odds_are_skipped(self, session: AsyncSession) -> None:
        """No market prior means nothing to measure the model against."""
        matches = [m.model_copy(update={"closing_odds": {}}) for m in _season()]
        report = await BackfillService(session).backfill(
            matches, "Championship", reference_bookmaker="market_avg"
        )
        assert report.stored == 0
        assert report.skipped["no_reference_odds"] == len(matches)

    async def test_rerunning_is_idempotent(self, session: AsyncSession) -> None:
        service = BackfillService(session)
        matches = _season()
        first = await service.backfill(matches, "Championship", reference_bookmaker="market_avg")
        second = await service.backfill(matches, "Championship", reference_bookmaker="market_avg")

        assert second.stored == 0
        assert second.already_present == first.stored
        count = int(
            (
                await session.execute(select(func.count()).select_from(SettledPrediction))
            ).scalar_one()
        )
        assert count == first.stored

    async def test_records_model_version_and_coverage(self, session: AsyncSession) -> None:
        await BackfillService(session).backfill(
            _season(), "Championship", reference_bookmaker="market_avg"
        )
        row = (await session.execute(select(SettledPrediction))).scalars().first()
        assert row is not None
        assert row.model_version
        assert row.coverage in {"fully_modelled", "partially_modelled"}
        assert row.components_used

    async def test_market_baseline_is_recorded(self, session: AsyncSession) -> None:
        await BackfillService(session).backfill(
            _season(), "Championship", reference_bookmaker="market_avg"
        )
        row = (await session.execute(select(SettledPrediction))).scalars().first()
        assert row is not None
        assert row.market_home is not None
        assert abs(row.market_home + row.market_draw + row.market_away - 1) < 0.01

    async def test_results_are_recorded_faithfully(self, session: AsyncSession) -> None:
        matches = _season()
        await BackfillService(session).backfill(
            matches, "Championship", reference_bookmaker="market_avg"
        )
        by_id = {m.external_id: m for m in matches}
        rows = (await session.execute(select(SettledPrediction))).scalars().all()
        for row in rows:
            source = by_id[row.provider_event_id]
            assert row.home_goals == source.home_goals
            assert row.away_goals == source.away_goals


class TestNoLeakage:
    """A fixture must never inform its own forecast."""

    async def test_predictions_are_not_suspiciously_accurate(self, session: AsyncSession) -> None:
        """Leakage shows up as accuracy no honest model achieves.

        Against a market prior with zero model influence, the forecast is the
        price. Anything approaching certainty means the result leaked in.
        """
        await BackfillService(session).backfill(
            _season(rounds=8), "Championship", reference_bookmaker="market_avg"
        )
        summary = await PerformanceService(session).summarise(source="backfill")

        assert summary.brier is not None
        assert summary.brier > 0.10, (
            "A Brier score this low on real fixtures indicates the outcome "
            "leaked into the forecast."
        )

    async def test_chronological_order_is_independent_of_input_order(
        self, session: AsyncSession
    ) -> None:
        """Shuffled input must produce identical output.

        If it does not, something is depending on arrival order rather than
        match date, and the replay is not reproducible.
        """
        import random

        ordered = _season()
        shuffled = list(ordered)
        random.Random(3).shuffle(shuffled)

        service = BackfillService(session)
        first = await service.backfill(ordered, "Championship", reference_bookmaker="market_avg")

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession)
        async with factory() as other:
            second = await BackfillService(other).backfill(
                shuffled, "Championship", reference_bookmaker="market_avg"
            )
        await engine.dispose()

        assert first.stored == second.stored
        assert first.skipped == second.skipped


class TestSourceSeparation:
    """Backfill and live are reported separately, always."""

    async def test_live_query_excludes_backfill(self, session: AsyncSession) -> None:
        await BackfillService(session).backfill(
            _season(), "Championship", reference_bookmaker="market_avg"
        )
        service = PerformanceService(session)

        assert (await service.summarise(source="live")).sample == 0
        assert (await service.summarise(source="backfill")).sample > 0

    async def test_counts_by_source(self, session: AsyncSession) -> None:
        await BackfillService(session).backfill(
            _season(), "Championship", reference_bookmaker="market_avg"
        )
        counts = await PerformanceService(session).counts_by_source()
        assert counts[BACKFILL_SOURCE] > 0
        assert "live" not in counts

    async def test_breakdowns_respect_source(self, session: AsyncSession) -> None:
        await BackfillService(session).backfill(
            _season(), "Championship", reference_bookmaker="market_avg"
        )
        service = PerformanceService(session)

        assert await service.by_competition(source="live") == {}
        assert "Championship" in await service.by_competition(source="backfill")

    async def test_default_is_live_not_blended(self, session: AsyncSession) -> None:
        """Blending by accident would misrepresent both sets."""
        await BackfillService(session).backfill(
            _season(), "Championship", reference_bookmaker="market_avg"
        )
        assert (await PerformanceService(session).summarise()).sample == 0
