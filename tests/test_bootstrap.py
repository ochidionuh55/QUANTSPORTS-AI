"""First-run bootstrap tests.

A deployment that needs someone to run three long console jobs in the right
order is a deployment that will be done wrong. These tests check the worker
does it itself, and — more importantly — that a restart does not redo work or
duplicate data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import HistoricalMatch, SettledPrediction, Team
from app.services.team_names import normalize_team_name
from app.worker.bootstrap import (
    MIN_MATCHES,
    MIN_SETTLED,
    BootstrapReport,
    _count,
    bootstrap,
)

NOW = datetime(2026, 3, 1, tzinfo=UTC)


class _Database:
    """Minimal stand-in exposing the session factory bootstrap needs."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    def session(self):
        """Return a session context manager."""
        return self._factory.begin()


@pytest_asyncio.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch) -> None:
    """Point the bootstrap at an empty directory.

    Without this every test would ingest the real 113,000-match dataset that
    ships with the image, which takes minutes and is not what any of these
    tests are checking.
    """
    monkeypatch.setattr("app.worker.bootstrap.data_dir", lambda: tmp_path)


@pytest_asyncio.fixture
async def database() -> AsyncIterator[_Database]:
    """Provide a bootstrap-compatible database over an in-memory schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield _Database(factory)
    await engine.dispose()


class TestReport:
    """The summary must read plainly in a log."""

    def test_empty_report_says_so(self) -> None:
        report = BootstrapReport()
        assert "nothing to do" in report.summary()

    def test_work_is_described(self) -> None:
        report = BootstrapReport(ingested=100, backfilled=50, highlights=10)
        summary = report.summary()
        assert "100 matches" in summary
        assert "50 predictions" in summary
        assert "10 highlights" in summary


class TestSkipsWhenAlreadyLoaded:
    """A restart must be cheap, not a second ingestion."""

    async def test_existing_history_is_not_reingested(self, database: _Database) -> None:
        async with database.session() as session:
            home = Team(
                canonical_name="Alpha",
                normalized_name=normalize_team_name("Alpha"),
                sport="football",
            )
            away = Team(
                canonical_name="Bravo",
                normalized_name=normalize_team_name("Bravo"),
                sport="football",
            )
            session.add_all([home, away])
            await session.flush()

            for index in range(MIN_MATCHES + 1):
                session.add(
                    HistoricalMatch(
                        provider_name="csv",
                        provider_match_id=f"m{index}",
                        home_team_id=home.id,
                        away_team_id=away.id,
                        season="2025/2026",
                        match_date=(NOW - timedelta(days=index % 300)).date(),
                        home_goals=1,
                        away_goals=0,
                        data_version="v",
                        ingested_at=NOW,
                    )
                )

        before = await _count(database, HistoricalMatch)  # type: ignore[arg-type]
        report = await bootstrap(database)  # type: ignore[arg-type]
        after = await _count(database, HistoricalMatch)  # type: ignore[arg-type]

        assert report.ingested == 0
        assert after == before

    async def test_missing_data_directory_is_reported_not_fatal(
        self, database: _Database, monkeypatch
    ) -> None:
        """A deployment without CSVs must still start."""
        monkeypatch.setattr("app.worker.bootstrap.data_dir", lambda: Path("/nonexistent"))
        report = await bootstrap(database)  # type: ignore[arg-type]

        assert report.ingested == 0
        assert report.skipped


class TestThresholds:
    """The guards that make a restart safe."""

    def test_history_threshold_is_not_zero(self) -> None:
        """A partial load must continue, not be mistaken for a complete one."""
        assert MIN_MATCHES > 0

    def test_settlement_threshold_is_not_zero(self) -> None:
        assert MIN_SETTLED > 0


class TestOrdering:
    """Later stages depend on earlier ones."""

    async def test_backfill_waits_for_history(self, database: _Database) -> None:
        """Backfilling an empty database would produce nothing but noise."""
        report = await bootstrap(database)  # type: ignore[arg-type]
        assert report.backfilled == 0

    async def test_reconstruction_waits_for_predictions(self, database: _Database) -> None:
        report = await bootstrap(database)  # type: ignore[arg-type]
        assert report.highlights == 0

        async with database.session() as session:
            count = (
                await session.execute(select(func.count()).select_from(SettledPrediction))
            ).scalar_one()
        assert count == 0
