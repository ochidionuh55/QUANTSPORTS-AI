"""Scan telemetry: the invariants that make the funnel trustworthy.

The first diagnostic reported a funnel where later stages exceeded earlier
ones — 14 analyses, 445 published — and printed a verdict anyway. The cause was
not arithmetic: ``stored_analyses`` is pruned once a fixture finishes, so the
population had been deleted, and selections were counted as rows while
everything upstream counted fixtures.

These tests pin the properties that make that impossible to repeat.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import ScanDecision, ScanRun
from app.database.models.scan_telemetry import (
    STAGE_ORDER,
    RejectionCode,
    ScanRunStatus,
    ScanStage,
)
from app.services.scan_telemetry import RETENTION_DAYS, ScanTelemetry

NOW = datetime(2026, 9, 17, 6, 30, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session against a fresh in-memory schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as active:
        yield active
    await engine.dispose()


async def _started(session: AsyncSession) -> ScanTelemetry:
    telemetry = ScanTelemetry(session)
    await telemetry.start(provider="api_football", hours_ahead=48, now=NOW)
    return telemetry


@pytest.mark.asyncio
class TestUniqueFixtureCounting:
    """The funnel's unit is the fixture, never the selection row."""

    async def test_counts_fixtures_once(self, session: AsyncSession) -> None:
        telemetry = await _started(session)
        for index in range(5):
            telemetry.record(
                f"fx{index}",
                competition_supported=True,
                identity_resolved=True,
                history_sufficient=True,
                model_produced=True,
                fully_modelled=True,
            )
        run = await telemetry.finish(now=NOW)
        assert run is not None
        assert run.fixtures_seen == 5
        assert run.fully_modelled == 5

    async def test_recording_the_same_fixture_twice_replaces_it(
        self, session: AsyncSession
    ) -> None:
        """A retried scan must not inflate the funnel."""
        telemetry = await _started(session)
        telemetry.record("fx1", competition_supported=True)
        telemetry.record(
            "fx1",
            competition_supported=True,
            identity_resolved=True,
            history_sufficient=True,
            model_produced=True,
            fully_modelled=True,
        )
        run = await telemetry.finish(now=NOW)
        assert run is not None
        assert run.fixtures_seen == 1
        assert run.fully_modelled == 1

    async def test_many_selections_from_one_fixture_count_once(
        self, session: AsyncSession
    ) -> None:
        """The exact shape of the original bug.

        One fixture publishing in six services is one fixture in the funnel and
        six in ``selections_published``. Conflating them is what made the first
        report show 445 published against 14 analysed.
        """
        telemetry = await _started(session)
        telemetry.record(
            "fx1",
            competition_supported=True,
            identity_resolved=True,
            history_sufficient=True,
            model_produced=True,
            fully_modelled=True,
        )
        run = await telemetry.finish(now=NOW)
        assert run is not None
        await telemetry.attach_publication({"fx1": 6}, run_id=run.id)

        refreshed = await session.get(ScanRun, run.id)
        assert refreshed is not None
        assert refreshed.fixtures_with_qualifying_selection == 1
        assert refreshed.selections_published == 6
        assert refreshed.funnel_is_monotonic()

    async def test_duplicates_do_not_enter_the_funnel(self, session: AsyncSession) -> None:
        telemetry = await _started(session)
        telemetry.record("fx1", competition_supported=True)
        telemetry.record_duplicate("fx1")
        telemetry.record_duplicate("fx1")
        run = await telemetry.finish(fixtures_seen=3, now=NOW)
        assert run is not None
        assert run.duplicates_skipped == 2
        assert run.supported == 1


@pytest.mark.asyncio
class TestFunnelMonotonicity:
    """Each stage must be no larger than the one before it."""

    async def test_realistic_funnel_is_monotonic(self, session: AsyncSession) -> None:
        telemetry = await _started(session)
        for index in range(20):
            telemetry.record(f"unsupported{index}")
        for index in range(5):
            telemetry.record(f"identity{index}", competition_supported=True)
        for index in range(3):
            telemetry.record(
                f"history{index}", competition_supported=True, identity_resolved=True
            )
        for index in range(10):
            telemetry.record(
                f"good{index}",
                competition_supported=True,
                identity_resolved=True,
                history_sufficient=True,
                model_produced=True,
                fully_modelled=True,
            )
        run = await telemetry.finish(now=NOW)
        assert run is not None
        assert run.funnel_is_monotonic()
        assert run.fixtures_seen == 38
        assert run.supported == 18
        assert run.identity_resolved == 13
        assert run.sufficient_history == 10
        assert run.fully_modelled == 10

    async def test_widening_funnel_is_detected(self, session: AsyncSession) -> None:
        """The check must actually fail on an impossible funnel."""
        run = ScanRun(
            started_at=NOW,
            provider="test",
            fixtures_seen=14,
            supported=14,
            identity_resolved=8,
            sufficient_history=6,
            model_produced=6,
            fully_modelled=6,
            fixtures_with_qualifying_selection=445,
        )
        assert not run.funnel_is_monotonic()

    async def test_stage_order_is_complete(self) -> None:
        assert len(STAGE_ORDER) == len(ScanStage)


@pytest.mark.asyncio
class TestRejectionsArePreserved:
    """What we rejected is the evidence, not a by-product."""

    async def test_unsupported_fixtures_are_stored(self, session: AsyncSession) -> None:
        telemetry = await _started(session)
        telemetry.record(
            "fx1",
            competition="Some Cup",
            rejection_code=RejectionCode.UNSUPPORTED_COMPETITION,
            rejection_detail="Competition not in our supported universe.",
        )
        await telemetry.finish(now=NOW)

        rows = await session.execute(select(ScanDecision))
        stored = list(rows.scalars().all())
        assert len(stored) == 1
        assert stored[0].rejection_code == RejectionCode.UNSUPPORTED_COMPETITION.value
        assert stored[0].terminal_stage == ScanStage.SEEN.value

    async def test_codes_are_machine_readable(self, session: AsyncSession) -> None:
        """Analytics must never depend on parsing English."""
        telemetry = await _started(session)
        for index, code in enumerate(RejectionCode):
            telemetry.record(f"fx{index}", rejection_code=code, rejection_detail="prose")
        await telemetry.finish(now=NOW)

        rows = await session.execute(select(ScanDecision.rejection_code))
        stored = {value for (value,) in rows.all()}
        assert stored == {code.value for code in RejectionCode}

    async def test_fully_modelled_but_unqualified_gets_a_code(
        self, session: AsyncSession
    ) -> None:
        """A fixture that modelled fine and passed no threshold is evidence too."""
        telemetry = await _started(session)
        telemetry.record(
            "fx1",
            competition_supported=True,
            identity_resolved=True,
            history_sufficient=True,
            model_produced=True,
            fully_modelled=True,
        )
        run = await telemetry.finish(now=NOW)
        assert run is not None
        await telemetry.attach_publication({}, run_id=run.id)

        rows = await session.execute(select(ScanDecision))
        decision = list(rows.scalars().all())[0]
        assert decision.rejection_code == RejectionCode.NO_SERVICE_QUALIFIED.value
        assert decision.qualified is False

    async def test_detail_is_truncated_not_rejected(self, session: AsyncSession) -> None:
        telemetry = await _started(session)
        telemetry.record("fx1", rejection_detail="x" * 900)
        await telemetry.finish(now=NOW)
        rows = await session.execute(select(ScanDecision.rejection_detail))
        assert len(str(list(rows.scalars().all())[0])) <= 512


@pytest.mark.asyncio
class TestRetention:
    """Telemetry outlives the fixtures it describes."""

    async def test_retention_is_by_scan_age_not_fixture_completion(
        self, session: AsyncSession
    ) -> None:
        """The property that makes this table answer the original question.

        ``stored_analyses`` is pruned when a fixture finishes, which deleted
        the evidence within days. Here a long-finished fixture's decision must
        survive.
        """
        telemetry = ScanTelemetry(session)
        old = NOW - timedelta(days=30)
        await telemetry.start(provider="test", hours_ahead=48, now=old)
        telemetry.record("finished_long_ago", kickoff=old, competition_supported=True)
        await telemetry.finish(now=old)

        removed = await telemetry.prune(now=NOW)
        assert removed == 0
        rows = await session.execute(select(ScanDecision))
        assert len(list(rows.scalars().all())) == 1

    async def test_prunes_beyond_the_window(self, session: AsyncSession) -> None:
        telemetry = ScanTelemetry(session)
        ancient = NOW - timedelta(days=RETENTION_DAYS + 10)
        await telemetry.start(provider="test", hours_ahead=48, now=ancient)
        telemetry.record("fx1")
        await telemetry.finish(now=ancient)

        assert await telemetry.prune(now=NOW) == 1


@pytest.mark.asyncio
class TestFailedScans:
    """A scan that fails must still leave a record of having tried."""

    async def test_quota_exhausted_is_recorded(self, session: AsyncSession) -> None:
        telemetry = await _started(session)
        run = await telemetry.finish(
            status=ScanRunStatus.QUOTA_EXHAUSTED, fixtures_seen=0, now=NOW
        )
        assert run is not None
        assert run.status == ScanRunStatus.QUOTA_EXHAUSTED.value
        assert run.fixtures_seen == 0

    async def test_provider_error_is_recorded(self, session: AsyncSession) -> None:
        telemetry = await _started(session)
        run = await telemetry.finish(
            status=ScanRunStatus.PROVIDER_ERROR, fixtures_seen=0, error_count=1, now=NOW
        )
        assert run is not None
        assert run.status == ScanRunStatus.PROVIDER_ERROR.value
        assert run.error_count == 1

    async def test_partial_failure_keeps_the_successful_fixtures(
        self, session: AsyncSession
    ) -> None:
        """One bad fixture must not discard the rest of the card."""
        telemetry = await _started(session)
        telemetry.record("good", competition_supported=True, identity_resolved=True)
        telemetry.record("bad", rejection_code=RejectionCode.ANALYSIS_ERROR)
        run = await telemetry.finish(fixtures_seen=2, error_count=1, now=NOW)
        assert run is not None
        assert run.fixtures_seen == 2
        assert run.supported == 1
        assert run.error_count == 1

    async def test_recording_without_a_run_is_a_no_op(self, session: AsyncSession) -> None:
        """Telemetry must never raise into the scan."""
        telemetry = ScanTelemetry(session)
        telemetry.record("fx1", competition_supported=True)
        telemetry.record_duplicate("fx1")
        assert await telemetry.finish(now=NOW) is None


@pytest.mark.asyncio
class TestAttribution:
    """Version and competition attribution, for later comparison."""

    async def test_model_version_is_recorded(self, session: AsyncSession) -> None:
        telemetry = ScanTelemetry(session)
        await telemetry.start(
            provider="api_football",
            hours_ahead=48,
            model_version="model-only-v2-dc",
            grid_version="grid-v2-poisson-dc",
            now=NOW,
        )
        run = await telemetry.finish(now=NOW)
        assert run is not None
        assert run.model_version == "model-only-v2-dc"
        assert run.grid_version == "grid-v2-poisson-dc"

    async def test_competition_code_is_stored_for_grouping(
        self, session: AsyncSession
    ) -> None:
        """Codes, not display names, so the stale-16 split is exact."""
        telemetry = await _started(session)
        telemetry.record("fx1", competition="Premier League", competition_code="E0")
        await telemetry.finish(now=NOW)
        rows = await session.execute(select(ScanDecision.competition_code))
        assert list(rows.scalars().all()) == ["E0"]


class TestCompetitionIdentifierTypes:
    """The string/int mismatch that made all 38 competitions look unused.

    ``ProviderCompetition.external_id`` is a string; ``Competition.
    api_football_id`` is an int. Comparing them directly is always false, which
    is how a discovery report listed our own Premier League as unconfigured.
    """

    def test_provider_ids_are_strings(self) -> None:
        from app.providers.models import ProviderCompetition

        competition = ProviderCompetition(
            provider_name="api_football", external_id="39", name="E0"
        )
        assert isinstance(competition.external_id, str)

    def test_configured_ids_are_integers(self) -> None:
        from app.core.competitions import COMPETITIONS

        for competition in COMPETITIONS:
            assert isinstance(competition.api_football_id, int)

    def test_naive_comparison_fails(self) -> None:
        """Pinned so nobody writes this comparison again."""
        from app.core.competitions import COMPETITIONS

        configured = {c.api_football_id for c in COMPETITIONS}
        assert "39" not in configured
        assert 39 in configured

    def test_normalised_comparison_resolves(self) -> None:
        """Compared as integers, API id 39 is the Premier League."""
        from app.core.competitions import COMPETITIONS

        by_id = {int(c.api_football_id): c.code for c in COMPETITIONS}
        assert by_id[int("39")] == "E0"


class TestMigrationStampsTimestamps:
    """The first deployment failed on this and took the scan down with it.

    ``TimestampMixin`` documents that "defaults are applied by the database
    rather than Python". A migration that creates ``created_at`` as NOT NULL
    without a server default therefore has nothing to fill it, and every insert
    raises NotNullViolationError.
    """

    def test_migration_gives_timestamps_a_server_default(self) -> None:
        from pathlib import Path

        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "versions"
            / "a7c2e1d94f30_scan_telemetry.py"
        )
        source = migration.read_text(encoding="utf-8")
        # Two tables, two timestamp columns each.
        assert source.count("server_default=sa.func.now()") == 4

    def test_model_timestamps_expect_a_database_default(self) -> None:
        for table in (ScanRun.__table__, ScanDecision.__table__):
            for name in ("created_at", "updated_at"):
                assert table.columns[name].server_default is not None, f"{table.name}.{name}"


@pytest.mark.asyncio
class TestTelemetryCannotBreakTheScan:
    """Observability must be enforced, not asserted.

    The first deployment caught its IntegrityError and logged a warning — and
    the scan still analysed nothing, because catching an exception does not
    repair a session SQLAlchemy has already marked unusable. Every subsequent
    fixture failed with "transaction has been rolled back".

    Each write now runs in its own SAVEPOINT, so a failure rolls back only
    that savepoint.
    """

    async def test_writes_use_savepoints(self) -> None:
        """Asserted on source: this is a property of how the writes are made."""
        import inspect

        import app.services.scan_telemetry as module

        source = inspect.getsource(module)
        assert source.count("begin_nested()") >= 3

    async def test_session_survives_a_failed_telemetry_write(
        self, session: AsyncSession
    ) -> None:
        """The property that matters: the scan continues after telemetry fails."""
        telemetry = await _started(session)

        # A NOT NULL violation, which SQLite does enforce, reproducing a write
        # that fails at flush the way the production IntegrityError did.
        # (A dangling foreign key would not: SQLite leaves FK checks off.)
        telemetry.record("fx1", competition_supported=True)
        for decision in telemetry._decisions.values():
            decision.provider_event_id = None  # type: ignore[assignment]

        assert await telemetry.finish(now=NOW) is None

        # The session must still be usable — this is what was broken.
        rows = await session.execute(select(ScanRun))
        assert list(rows.scalars().all())

    async def test_failed_start_leaves_recording_inert(
        self, session: AsyncSession
    ) -> None:
        """With no run, recording is a no-op rather than an error."""
        telemetry = ScanTelemetry(session)
        telemetry.record("fx1", competition_supported=True)
        assert await telemetry.finish(now=NOW) is None
        rows = await session.execute(select(ScanDecision))
        assert list(rows.scalars().all()) == []
