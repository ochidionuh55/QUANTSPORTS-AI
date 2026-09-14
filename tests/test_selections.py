"""Published selection tests.

A published selection is a public claim about a match that has not happened.
These tests exist to make that claim unfalsifiable after the fact: it cannot be
edited, cannot be regenerated, and cannot be quietly replaced by a better one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import (
    DailySnapshot,
    SelectionAudit,
    ServiceSelection,
    SettledPrediction,
    StoredAnalysis,
)
from app.services.selections import (
    UNDER_OBSERVATION,
    WITHHELD,
    SelectionService,
    published_services,
)

NOW = datetime(2026, 3, 15, 9, tzinfo=UTC)


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
    home: str = "0.66",
    draw: str = "0.20",
    away: str = "0.14",
    matches: int = 90,
    coverage: str = "fully_modelled",
    with_model: bool = True,
) -> StoredAnalysis:
    """Insert an analysis strong enough to qualify for several services."""
    record = StoredAnalysis(
        provider_name="api_football",
        provider_event_id=fixture_id,
        home_name=f"Alpha{fixture_id}",
        away_name=f"Bravo{fixture_id}",
        competition="Championship",
        kickoff=NOW + timedelta(hours=hours),
        coverage=coverage,
        markets={"1X2": {"Home": home, "Draw": draw, "Away": away}},
        market_probabilities={"home": home, "draw": draw, "away": away},
        model_probabilities=({"home": home, "draw": draw, "away": away} if with_model else {}),
        component_views=(
            [
                {"home": home, "draw": draw, "away": away},
                {"home": home, "draw": draw, "away": away},
            ]
            if with_model
            else []
        ),
        expected_home_goals=1.9,
        expected_away_goals=0.8,
        home_stats={"matches": matches, "resolved": True},
        away_stats={"matches": matches, "resolved": True},
        components_used=["poisson", "elo", "form"],
        components_dropped=[],
        computed_at=NOW,
    )
    session.add(record)
    await session.flush()
    return record


class TestPublication:
    """Writing today's selections."""

    async def test_publishes_qualifying_selections(self, session: AsyncSession) -> None:
        await _analysis(session)
        report = await SelectionService(session).publish(now=NOW)

        assert report.published > 0
        stored = (await session.execute(select(ServiceSelection))).scalars().all()
        assert stored

    async def test_published_before_kickoff(self, session: AsyncSession) -> None:
        """The property the whole track record rests on."""
        await _analysis(session)
        await SelectionService(session).publish(now=NOW)

        for selection in (await session.execute(select(ServiceSelection))).scalars():
            assert selection.published_before_kickoff

    async def test_started_fixtures_are_never_published(self, session: AsyncSession) -> None:
        await _analysis(session, hours=-3)
        report = await SelectionService(session).publish(now=NOW)

        assert report.analysed == 0
        assert report.published == 0

    async def test_withheld_services_never_publish(self, session: AsyncSession) -> None:
        """Validation found these overconfident; shipping them would sell a
        number already measured as wrong."""
        await _analysis(session, home="0.30", draw="0.30", away="0.40")
        await SelectionService(session).publish(now=NOW)

        stored = (await session.execute(select(ServiceSelection))).scalars().all()
        assert all(s.service_key not in WITHHELD for s in stored)

    async def test_withheld_services_are_excluded_from_the_catalogue(self) -> None:
        assert not WITHHELD & set(published_services())

    async def test_no_duplicates_on_rerun(self, session: AsyncSession) -> None:
        """A second worker run must not replace a claim already made."""
        await _analysis(session)
        service = SelectionService(session)
        first = await service.publish(now=NOW)
        second = await service.publish(now=NOW + timedelta(minutes=20))

        assert first.published > 0
        assert second.published == 0
        assert second.already_present > 0

        count = int(
            (await session.execute(select(func.count()).select_from(ServiceSelection))).scalar_one()
        )
        assert count == first.published

    async def test_a_service_with_no_qualifying_fixture_publishes_nothing(
        self, session: AsyncSession
    ) -> None:
        """An even fixture must not be forced into the home-win slot.

        Other services may still qualify from the same fixture — a balanced
        result with plenty of goals is a real thing — so the check is per
        service rather than across the whole card.
        """
        await _analysis(session, home="0.34", draw="0.33", away="0.33")
        await SelectionService(session).publish(now=NOW)

        home = (
            await session.execute(
                select(ServiceSelection).where(ServiceSelection.service_key == "home")
            )
        ).scalar_one_or_none()
        assert home is None

    async def test_an_empty_card_publishes_nothing(self, session: AsyncSession) -> None:
        """No fixtures means no selections, not a fallback."""
        report = await SelectionService(session).publish(now=NOW)

        assert report.published == 0
        assert report.services_qualified == 0

    async def test_thin_history_is_not_published(self, session: AsyncSession) -> None:
        await _analysis(session, matches=4)
        report = await SelectionService(session).publish(now=NOW)

        assert report.published == 0

    async def test_analyses_without_model_output_are_skipped(self, session: AsyncSession) -> None:
        """Nothing is invented for fixtures the models never ran on."""
        await _analysis(session, with_model=False)
        report = await SelectionService(session).publish(now=NOW)

        assert report.forecasts == 0
        assert report.published == 0

    async def test_publication_is_audited(self, session: AsyncSession) -> None:
        await _analysis(session)
        await SelectionService(session).publish(now=NOW)

        audits = (await session.execute(select(SelectionAudit))).scalars().all()
        assert audits
        assert all(a.event == "published" for a in audits)

    async def test_snapshot_records_the_day(self, session: AsyncSession) -> None:
        """A quiet day must be distinguishable from a day the worker missed."""
        await _analysis(session)
        await SelectionService(session).publish(now=NOW)

        snapshot = (await session.execute(select(DailySnapshot))).scalar_one_or_none()
        assert snapshot is not None
        assert snapshot.fixtures_available == 1
        assert snapshot.snapshot_date == NOW.date()

    async def test_snapshot_written_even_with_no_selections(self, session: AsyncSession) -> None:
        await SelectionService(session).publish(now=NOW)

        snapshot = (await session.execute(select(DailySnapshot))).scalar_one_or_none()
        assert snapshot is not None
        assert snapshot.fixtures_available == 0


class TestImmutability:
    """A published claim cannot be edited afterwards."""

    async def test_locked_fields_survive_republication(self, session: AsyncSession) -> None:
        """A stronger fixture appearing later must not overwrite the claim."""
        await _analysis(session, "1")
        service = SelectionService(session)
        await service.publish(now=NOW)

        original = (await session.execute(select(ServiceSelection).limit(1))).scalar_one()
        locked = dict(original.locked_fields())

        await _analysis(session, "2", home="0.85", draw="0.10", away="0.05")
        await service.publish(now=NOW + timedelta(hours=1))

        await session.refresh(original)
        assert original.locked_fields() == locked

    async def test_settlement_does_not_touch_locked_fields(self, session: AsyncSession) -> None:
        await _analysis(session)
        service = SelectionService(session)
        await service.publish(now=NOW)

        selection = (await session.execute(select(ServiceSelection).limit(1))).scalar_one()
        locked = dict(selection.locked_fields())

        session.add(
            SettledPrediction(
                provider_name="api_football",
                provider_event_id=selection.provider_event_id,
                source="live",
                home_name=selection.home_name,
                away_name=selection.away_name,
                competition="Championship",
                kickoff=selection.kickoff,
                coverage="fully_modelled",
                model_version="v1",
                components_used=[],
                predicted_home=Decimal("0.66"),
                predicted_draw=Decimal("0.20"),
                predicted_away=Decimal("0.14"),
                home_goals=2,
                away_goals=0,
                actual_result="home",
                predicted_favourite="home",
                favourite_won=True,
                settled_at=NOW,
            )
        )
        await session.flush()
        await service.settle(now=NOW + timedelta(hours=9))

        await session.refresh(selection)
        assert selection.locked_fields() == locked
        assert selection.status in {"won", "lost", "void"}


class TestSettlement:
    """Results are filled in from the shared settlement record."""

    async def _settle(self, session: AsyncSession, home: int, away: int) -> ServiceSelection:
        await _analysis(session)
        service = SelectionService(session)
        await service.publish(now=NOW)
        selection = (await session.execute(select(ServiceSelection).limit(1))).scalar_one()

        session.add(
            SettledPrediction(
                provider_name="api_football",
                provider_event_id=selection.provider_event_id,
                source="live",
                home_name=selection.home_name,
                away_name=selection.away_name,
                competition="Championship",
                kickoff=selection.kickoff,
                coverage="fully_modelled",
                model_version="v1",
                components_used=[],
                predicted_home=Decimal("0.66"),
                predicted_draw=Decimal("0.20"),
                predicted_away=Decimal("0.14"),
                home_goals=home,
                away_goals=away,
                actual_result="home" if home > away else "away",
                predicted_favourite="home",
                favourite_won=home > away,
                settled_at=NOW,
            )
        )
        await session.flush()
        await service.settle(now=NOW + timedelta(hours=9))
        await session.refresh(selection)
        return selection

    async def test_pending_before_kickoff(self, session: AsyncSession) -> None:
        await _analysis(session)
        service = SelectionService(session)
        await service.publish(now=NOW)

        assert await service.settle(now=NOW + timedelta(hours=1)) == 0
        selection = (await session.execute(select(ServiceSelection).limit(1))).scalar_one()
        assert selection.status == "pending"

    async def test_home_win_settles(self, session: AsyncSession) -> None:
        selection = await self._settle(session, 2, 0)
        assert selection.is_settled
        assert selection.home_goals == 2

    async def test_a_loss_is_recorded(self, session: AsyncSession) -> None:
        """Losses must be as visible as wins."""
        await _analysis(session)
        service = SelectionService(session)
        await service.publish(now=NOW)

        home_selection = (
            await session.execute(
                select(ServiceSelection).where(ServiceSelection.service_key == "home")
            )
        ).scalar_one_or_none()
        if home_selection is None:
            pytest.skip("home service did not qualify")

        session.add(
            SettledPrediction(
                provider_name="api_football",
                provider_event_id=home_selection.provider_event_id,
                source="live",
                home_name=home_selection.home_name,
                away_name=home_selection.away_name,
                competition="Championship",
                kickoff=home_selection.kickoff,
                coverage="fully_modelled",
                model_version="v1",
                components_used=[],
                predicted_home=Decimal("0.66"),
                predicted_draw=Decimal("0.20"),
                predicted_away=Decimal("0.14"),
                home_goals=0,
                away_goals=2,
                actual_result="away",
                predicted_favourite="home",
                favourite_won=False,
                settled_at=NOW,
            )
        )
        await session.flush()
        await service.settle(now=NOW + timedelta(hours=9))
        await session.refresh(home_selection)

        assert home_selection.status == "lost"

    async def test_settlement_is_audited(self, session: AsyncSession) -> None:
        await self._settle(session, 2, 0)
        events = {a.event for a in (await session.execute(select(SelectionAudit))).scalars().all()}
        assert "settled" in events


class TestHistory:
    """Past days are read, never regenerated."""

    async def test_day_view_returns_stored_selections(self, session: AsyncSession) -> None:
        await _analysis(session)
        service = SelectionService(session)
        await service.publish(now=NOW)

        view = await service.day_view(NOW.date())
        assert view.selections
        assert view.snapshot is not None

    async def test_history_is_not_regenerated_from_current_state(
        self, session: AsyncSession
    ) -> None:
        """Anyone can look clever about last week's football."""
        await _analysis(session, "1")
        service = SelectionService(session)
        await service.publish(now=NOW)
        before = [(s.provider_event_id, s.probability) for s in await service.for_day(NOW.date())]

        # The world changes: the analyses are removed entirely.
        for analysis in (await session.execute(select(StoredAnalysis))).scalars():
            await session.delete(analysis)
        await session.flush()

        after = [(s.provider_event_id, s.probability) for s in await service.for_day(NOW.date())]
        assert after == before

    async def test_empty_day_returns_an_empty_view(self, session: AsyncSession) -> None:
        view = await SelectionService(session).day_view(NOW.date())
        assert view.selections == []

    async def test_available_days_newest_first(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        service = SelectionService(session)
        await service.publish(now=NOW)
        await service.publish(now=NOW + timedelta(days=1))

        days = await service.available_days()
        assert days == sorted(days, reverse=True)

    async def test_history_is_not_limited_to_a_week(self, session: AsyncSession) -> None:
        """Records stay available indefinitely."""
        long_ago = NOW - timedelta(days=200)

        # The fixture must sit on the day being published, now that only
        # today's card is eligible.
        record = await _analysis(session, "1")
        record.kickoff = long_ago + timedelta(hours=6)
        record.computed_at = long_ago
        await session.flush()

        service = SelectionService(session)
        await service.publish(now=long_ago)

        days = await service.available_days()
        assert long_ago.date() in days


class TestTrackRecord:
    """Live performance, per service."""

    async def test_every_published_service_appears(self, session: AsyncSession) -> None:
        records = await SelectionService(session).track_record()
        assert {r.key for r in records} == set(published_services())

    async def test_rates_withheld_until_the_sample_is_meaningful(
        self, session: AsyncSession
    ) -> None:
        """A rate from three results says nothing."""
        await _analysis(session)
        service = SelectionService(session)
        await service.publish(now=NOW)

        records = await service.track_record()
        assert all(not r.meaningful for r in records)

    async def test_observed_services_are_flagged(self, session: AsyncSession) -> None:
        """Services without strong evidence must not look proven."""
        records = await SelectionService(session).track_record()
        observed = {r.key for r in records if r.status == "observed"}
        assert observed == UNDER_OBSERVATION & set(published_services())

    async def test_gap_is_none_without_settled_selections(self, session: AsyncSession) -> None:
        records = await SelectionService(session).track_record()
        assert all(r.gap is None for r in records)


class TestServiceDepth:
    """Each service publishes a ranked list, not a single pick."""

    async def test_publishes_multiple_ranked_selections(self, session: AsyncSession) -> None:
        """A single pick per service gives a user nothing to work with on a
        busy card, and hides where the strength drops off."""
        for index in range(6):
            await _analysis(
                session,
                str(index),
                home=str(0.70 - index * 0.02),
                draw="0.18",
                away=str(0.12 + index * 0.02),
            )

        await SelectionService(session).publish(now=NOW)
        home = await SelectionService(session).for_service("home", day=NOW.date())

        assert len(home) > 1
        assert [s.rank for s in home] == sorted(s.rank for s in home)

    async def test_ranked_strongest_first(self, session: AsyncSession) -> None:
        """Rank one must score at least as well as rank two."""
        for index in range(5):
            await _analysis(session, str(index), home=str(0.72 - index * 0.03))

        service = SelectionService(session)
        await service.publish(now=NOW)
        home = await service.for_service("home", day=NOW.date())

        assert len(home) > 1, "ranking must produce a list, not a single pick"
        scores = [s.score for s in home]
        assert scores == sorted(scores, reverse=True)

    async def test_withheld_service_returns_nothing(self, session: AsyncSession) -> None:
        await _analysis(session)
        service = SelectionService(session)
        await service.publish(now=NOW)

        for key in WITHHELD:
            assert await service.for_service(key, day=NOW.date()) == []

    async def test_counts_exclude_withheld(self, session: AsyncSession) -> None:
        await _analysis(session)
        service = SelectionService(session)
        await service.publish(now=NOW)

        counts = await service.service_counts(day=NOW.date())
        assert not set(counts) & WITHHELD

    async def test_picks_for_fixture_lists_every_service(self, session: AsyncSession) -> None:
        """A fixture card should say which services chose it."""
        await _analysis(session, "1")
        service = SelectionService(session)
        await service.publish(now=NOW)

        picks = await service.picks_for_fixture("1", day=NOW.date())
        assert picks
        assert all(len(pick) == 3 for pick in picks)

    async def test_picks_for_unknown_fixture_is_empty(self, session: AsyncSession) -> None:
        picks = await SelectionService(session).picks_for_fixture("nope", day=NOW.date())
        assert picks == []


class TestStrongestAngle:
    """Every analysed fixture says something, without faking a selection."""

    @staticmethod
    def _record(markets: dict[str, dict[str, str]], components: list[str]) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            provider_event_id="1",
            home_name="Alpha",
            away_name="Bravo",
            kickoff=NOW,
            competition="Championship",
            coverage="fully_modelled" if components != ["market"] else "data_only",
            components_used=components,
            expected_home_goals=1.8,
            expected_away_goals=1.0,
            markets=markets,
        )

    def test_unremarkable_fixture_gets_no_angle(self) -> None:
        """A market true in almost every match is a fact, not a finding."""
        from app.bot.formatting import strongest_angle

        record = self._record(
            {
                "1X2": {"Home": "0.38", "Draw": "0.30", "Away": "0.32"},
                "Goals": {"Over 2.5": "0.51", "Under 2.5": "0.49"},
                "Both teams to score": {"Yes": "0.52", "No": "0.48"},
            },
            ["poisson", "elo", "form"],
        )
        assert strongest_angle(record) is None

    def test_strong_fixture_gets_an_angle(self) -> None:
        from app.bot.formatting import strongest_angle

        record = self._record(
            {
                "1X2": {"Home": "0.72", "Draw": "0.16", "Away": "0.12"},
                "Goals": {"Over 2.5": "0.55", "Under 2.5": "0.45"},
                "Both teams to score": {"Yes": "0.50", "No": "0.50"},
            },
            ["poisson", "elo", "form"],
        )
        angle = strongest_angle(record)
        assert angle is not None
        assert "Home" in angle[0]

    def test_angle_is_not_shown_when_a_service_selected_the_fixture(self) -> None:
        """A ranked selection outranks an observation, and only one appears."""
        from app.bot.formatting import format_summary_line

        record = self._record(
            {
                "1X2": {"Home": "0.72", "Draw": "0.16", "Away": "0.12"},
                "Goals": {"Over 2.5": "0.60", "Under 2.5": "0.40"},
                "Both teams to score": {"Yes": "0.55", "No": "0.45"},
            },
            ["poisson", "elo", "form"],
        )
        rendered = format_summary_line(record, {"1": [("🏆 Best Home Win", "Home", 0.72)]})
        assert "⭐" in rendered
        assert "📌" not in rendered

    def test_market_only_fixture_is_labelled_as_the_market(self) -> None:
        """Relaying a bookmaker price back as our analysis would be dishonest."""
        from app.bot.formatting import format_summary_line

        record = self._record(
            {
                "1X2": {"Home": "0.45", "Draw": "0.27", "Away": "0.28"},
                "Goals": {"Over 1.5": "0.85", "Over 2.5": "0.55"},
                "Both teams to score": {"Yes": "0.52", "No": "0.48"},
            },
            ["market"],
        )
        rendered = format_summary_line(record, {})
        assert "Market's strongest" in rendered
        assert "📌 Strongest" not in rendered


class TestSettlementIsGlobal:
    """Every published store settles from one source of truth."""

    async def test_worker_settles_selections_and_highlights_together(self) -> None:
        """Settling one store and not another lets the product tell two
        different stories about the same match.

        The worker previously settled highlights while leaving service
        selections pending forever, which is exactly that failure.
        """
        import pathlib

        source = pathlib.Path("app/worker/main.py").read_text()
        block = source[source.index("async def settle_finished") :]
        block = block[: block.index("scheduler.add_job")]

        assert "SettlementService(session).settle(provider)" in block
        assert "HighlightService(session).settle(" in block
        assert "SelectionService(session).settle(" in block

    async def test_settlement_never_alters_the_published_claim(self, session: AsyncSession) -> None:
        """The result is added; the prediction is not revised."""
        await _analysis(session)
        service = SelectionService(session)
        await service.publish(now=NOW)

        selection = (await session.execute(select(ServiceSelection).limit(1))).scalar_one()
        locked = dict(selection.locked_fields())

        session.add(
            SettledPrediction(
                provider_name="api_football",
                provider_event_id=selection.provider_event_id,
                source="live",
                home_name=selection.home_name,
                away_name=selection.away_name,
                competition="Championship",
                kickoff=selection.kickoff,
                coverage="fully_modelled",
                model_version="v1",
                components_used=[],
                predicted_home=Decimal("0.66"),
                predicted_draw=Decimal("0.20"),
                predicted_away=Decimal("0.14"),
                home_goals=2,
                away_goals=0,
                actual_result="home",
                predicted_favourite="home",
                favourite_won=True,
                settled_at=NOW,
            )
        )
        await session.flush()
        await service.settle(now=NOW + timedelta(hours=9))
        await session.refresh(selection)

        assert selection.locked_fields() == locked
        assert selection.status != "pending"
        assert selection.home_goals == 2

    async def test_a_days_record_is_read_not_recomputed(self, session: AsyncSession) -> None:
        """A daily record regenerated with today's model would be looking
        clever about football already played."""
        await _analysis(session, "1")
        service = SelectionService(session)
        await service.publish(now=NOW)

        before = [
            (s.provider_event_id, s.outcome, s.probability)
            for s in await service.for_day(NOW.date())
        ]

        for analysis in (await session.execute(select(StoredAnalysis))).scalars():
            await session.delete(analysis)
        await session.flush()

        after = [
            (s.provider_event_id, s.outcome, s.probability)
            for s in await service.for_day(NOW.date())
        ]
        assert after == before


class TestSettlementSurvivesPruning:
    """A published selection outlives the analysis that produced it.

    Stored analyses are transient — the scan removes them once a fixture is
    settled. A selection is permanent. If settlement depended entirely on the
    analysis, every fixture that finished overnight would become permanently
    unsettleable, and the record would show claims that could never be scored.
    """

    async def test_settles_from_the_provider_when_no_settlement_row_exists(
        self, session: AsyncSession
    ) -> None:
        await _analysis(session, "1")
        service = SelectionService(session)
        await service.publish(now=NOW)

        # The analysis is gone, exactly as pruning would leave it.
        for analysis in (await session.execute(select(StoredAnalysis))).scalars():
            await session.delete(analysis)
        await session.flush()

        class _Results:
            async def get_results(self, ids: list[str]) -> list[object]:
                from types import SimpleNamespace

                return [
                    SimpleNamespace(provider_event_id=identifier, home_goals=2, away_goals=0)
                    for identifier in ids
                ]

        settled = await service.settle(now=NOW + timedelta(hours=9), source=_Results())

        assert settled > 0
        selection = (await session.execute(select(ServiceSelection).limit(1))).scalar_one()
        assert selection.is_settled
        assert selection.home_goals == 2

    async def test_provider_failure_does_not_break_settlement(self, session: AsyncSession) -> None:
        """A provider outage must leave selections pending, not raise."""
        await _analysis(session, "1")
        service = SelectionService(session)
        await service.publish(now=NOW)

        class _Broken:
            async def get_results_for_dates(self, days: list[object]) -> list[object]:
                raise RuntimeError("provider unavailable")

        settled = await service.settle(now=NOW + timedelta(hours=9), source=_Broken())
        assert settled == 0

    async def test_settles_without_a_source(self, session: AsyncSession) -> None:
        """The settlement table remains the primary path."""
        await _analysis(session)
        service = SelectionService(session)
        await service.publish(now=NOW)

        selection = (await session.execute(select(ServiceSelection).limit(1))).scalar_one()
        session.add(
            SettledPrediction(
                provider_name="api_football",
                provider_event_id=selection.provider_event_id,
                source="live",
                home_name=selection.home_name,
                away_name=selection.away_name,
                competition="Championship",
                kickoff=selection.kickoff,
                coverage="fully_modelled",
                model_version="v1",
                components_used=[],
                predicted_home=Decimal("0.66"),
                predicted_draw=Decimal("0.20"),
                predicted_away=Decimal("0.14"),
                home_goals=3,
                away_goals=1,
                actual_result="home",
                predicted_favourite="home",
                favourite_won=True,
                settled_at=NOW,
            )
        )
        await session.flush()

        assert await service.settle(now=NOW + timedelta(hours=9)) > 0


class TestPruningProtectsSettlement:
    """The scan must not delete what settlement still needs."""

    def test_prune_requires_a_settlement_row(self) -> None:
        """Deleting on age alone is what made overnight fixtures
        unsettleable."""
        import pathlib

        source = pathlib.Path("app/services/daily_scan.py").read_text()
        block = source[source.index("async def _prune") :]
        block = block[: block.index("class AnalysisRepository")]

        assert "settled.exists()" in block
        assert "DELETE_AFTER_DAYS" in block


class TestResultsAreFetchedByDate:
    """The free plan forbids fetching fixtures by id.

    That restriction stranded every published selection permanently: the call
    failed, nothing settled, and the record showed predictions that could never
    be scored. Fetching by date is permitted and cheaper — one request covers a
    whole card rather than one per twenty fixtures.
    """

    async def test_dates_are_requested_not_ids(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        service = SelectionService(session)
        await service.publish(now=NOW)

        for analysis in (await session.execute(select(StoredAnalysis))).scalars():
            await session.delete(analysis)
        await session.flush()

        requested: list[object] = []

        class _ByDate:
            async def get_results_for_dates(self, days: list[object]) -> list[object]:
                from types import SimpleNamespace

                requested.extend(days)
                return [SimpleNamespace(provider_event_id="1", home_goals=2, away_goals=0)]

            async def get_results(self, ids: list[str]) -> list[object]:
                raise AssertionError("id lookup is forbidden on the free plan")

        settled = await service.settle(now=NOW + timedelta(hours=9), source=_ByDate())

        assert settled > 0
        assert requested == [NOW.date()]

    async def test_one_request_per_day_not_per_fixture(self, session: AsyncSession) -> None:
        """Twenty selections on one date must cost one request."""
        for index in range(20):
            await _analysis(session, str(index))
        service = SelectionService(session)
        await service.publish(now=NOW)

        for analysis in (await session.execute(select(StoredAnalysis))).scalars():
            await session.delete(analysis)
        await session.flush()

        calls: list[list[object]] = []

        class _Counting:
            async def get_results_for_dates(self, days: list[object]) -> list[object]:
                calls.append(list(days))
                return []

        await service.settle(now=NOW + timedelta(hours=9), source=_Counting())

        assert len(calls) == 1
        assert len(calls[0]) == 1

    async def test_falls_back_to_ids_when_that_is_all_a_source_offers(
        self, session: AsyncSession
    ) -> None:
        """A provider on a paid plan, or a test double, may only offer ids."""
        await _analysis(session, "1")
        service = SelectionService(session)
        await service.publish(now=NOW)

        for analysis in (await session.execute(select(StoredAnalysis))).scalars():
            await session.delete(analysis)
        await session.flush()

        class _ById:
            async def get_results(self, ids: list[str]) -> list[object]:
                from types import SimpleNamespace

                return [
                    SimpleNamespace(provider_event_id=identifier, home_goals=1, away_goals=0)
                    for identifier in ids
                ]

        settled = await service.settle(now=NOW + timedelta(hours=9), source=_ById())
        assert settled > 0
