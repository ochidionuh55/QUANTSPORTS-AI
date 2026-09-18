"""A fixture appears at most once in a service's list for a day.

The bug users found by looking at the product: the same match at positions 4
and 5 of Best Home Win. Every insert satisfied the database, because
``uq_service_selection_day`` constrains ``(service_key, selection_date, rank)``
and ``_store`` asked the same question — "is rank 5 taken?" — never "is this
fixture already here?".

The scan reruns every three hours. A fixture whose probability shifts lands at
a different rank, finds that slot free, and publishes again. 1,506 tests did
not catch it because each individual insertion was valid under the rule we had
written; the rule we needed was one nobody had thought to write.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import ServiceSelection
from app.database.models.selections import PENDING

DAY = date(2026, 9, 18)
FIXTURE = "fx-12345"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as active:
        yield active
    await engine.dispose()


def _selection(rank: int, fixture: str = FIXTURE, hour: int = 6) -> ServiceSelection:
    """One published selection."""
    return ServiceSelection(
        service_key="home",
        service_label="🏆 Best Home Win",
        selection_date=DAY,
        rank=rank,
        provider_event_id=fixture,
        home_name="Shamrock Rovers",
        away_name="Waterford",
        competition="Premier Division",
        country="Ireland",
        kickoff=datetime(2026, 9, 18, 19, 0, tzinfo=UTC),
        market="Match result",
        outcome="Home",
        probability=0.61,
        score=0.61,
        coverage="fully_modelled",
        model_version="model-only-v2-dc",
        components_used=[],
        factors={},
        rationale="test",
        sample_size=90,
        published_at=datetime(2026, 9, 18, hour, 0, tzinfo=UTC),
        status=PENDING,
    )


@pytest.mark.asyncio
class TestTheRealBug:
    """Scan 1 publishes at rank 4; scan 2 recalculates the same fixture to 7."""

    async def test_same_fixture_at_a_new_rank_is_rejected(
        self, session: AsyncSession
    ) -> None:
        session.add(_selection(rank=4, hour=6))
        await session.flush()

        # Three hours later the probability shifted and it ranks 7th. Rank 7
        # is free, so the old rule permitted this insert.
        session.add(_selection(rank=7, hour=9))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_the_first_publication_survives(
        self, session: AsyncSession
    ) -> None:
        """The earliest row is canonical: it is what users saw."""
        session.add(_selection(rank=4, hour=6))
        await session.commit()

        rows = await session.execute(
            select(ServiceSelection).where(
                ServiceSelection.provider_event_id == FIXTURE
            )
        )
        found = list(rows.scalars().all())
        assert len(found) == 1
        assert found[0].rank == 4
        assert found[0].published_at.hour == 6

    async def test_different_fixtures_may_share_a_service_and_day(
        self, session: AsyncSession
    ) -> None:
        """The constraint must not stop a service listing several matches."""
        session.add(_selection(rank=1, fixture="fx-1"))
        session.add(_selection(rank=2, fixture="fx-2"))
        session.add(_selection(rank=3, fixture="fx-3"))
        await session.flush()
        rows = await session.execute(select(ServiceSelection))
        assert len(list(rows.scalars().all())) == 3

    async def test_one_fixture_may_appear_in_several_services(
        self, session: AsyncSession
    ) -> None:
        """A match can qualify for Home Win and Over 1.5 at once."""
        first = _selection(rank=1)
        second = _selection(rank=1)
        second.service_key = "over_15"
        second.service_label = "⚽ Best Over 1.5"
        session.add(first)
        session.add(second)
        await session.flush()
        rows = await session.execute(select(ServiceSelection))
        assert len(list(rows.scalars().all())) == 2

    async def test_the_same_fixture_may_appear_on_another_day(
        self, session: AsyncSession
    ) -> None:
        first = _selection(rank=1)
        second = _selection(rank=1)
        second.selection_date = date(2026, 9, 19)
        session.add(first)
        session.add(second)
        await session.flush()
        rows = await session.execute(select(ServiceSelection))
        assert len(list(rows.scalars().all())) == 2


class TestInvariantsAreDeclared:
    def test_both_constraints_exist(self) -> None:
        """Rank uniqueness stays; fixture uniqueness is added beside it."""
        names = {c.name for c in ServiceSelection.__table__.constraints}
        assert "uq_service_selection_day" in names
        assert "uq_service_selection_fixture" in names

    def test_fixture_constraint_covers_the_right_columns(self) -> None:
        table = ServiceSelection.__table__
        constraint = next(
            c for c in table.constraints if c.name == "uq_service_selection_fixture"
        )
        assert {column.name for column in constraint.columns} == {
            "service_key",
            "selection_date",
            "provider_event_id",
        }

    def test_store_checks_the_fixture_before_the_rank(self) -> None:
        """Order matters: the rank check alone is what allowed the duplicate."""
        import inspect

        import app.services.selections as selections

        source = inspect.getsource(selections.SelectionService._store)
        fixture_check = source.index("ServiceSelection.provider_event_id == analysis")
        rank_check = source.index("ServiceSelection.rank == rank")
        assert fixture_check < rank_check

    def _migration(self) -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "versions"
            / "e7b4d02f9a13_selection_fixture_uniqueness.py"
        ).read_text(encoding="utf-8")

    def test_migration_deduplicates_rather_than_refusing(self) -> None:
        """A refusal at startup is an outage, not a guard.

        The first version raised when duplicates existed. Migrations run at
        worker boot, so that turned a data problem into a dead service. It now
        performs the cleanup, which is the decision it was trying to avoid
        making implicitly — made explicitly instead.
        """
        source = self._migration()
        assert "DELETE FROM service_selections" in source
        assert "FIRST_VALUE(id) OVER" in source

    def test_migration_keeps_the_earliest_publication(self) -> None:
        """Ordered by published_at, with rank and id breaking ties."""
        source = self._migration()
        assert "ORDER BY published_at, rank, id" in source

    def test_migration_records_every_removal(self) -> None:
        """Nothing disappears without an audit row naming it."""
        source = self._migration()
        assert "INSERT INTO selection_audits" in source
        assert "DUPLICATE_FIXTURE_IN_SERVICE" in source
        assert "removed duplicate" in source

    def test_migration_verifies_before_constraining(self) -> None:
        """If cleanup failed, say so rather than failing on the constraint."""
        source = self._migration()
        verify = source.index("remaining = connection.execute")
        constrain = source.index("op.create_unique_constraint")
        assert verify < constrain
