"""Prediction engine tests: derived markets, bulk scanning and storage.

The market tests assert that every published market is a mathematical
consequence of the same distribution. Double chance must equal the sum of its
1X2 components exactly, and over/under must be complementary — if either drifts,
the product is publishing numbers that disagree with themselves.
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
from app.database.models import HistoricalMatch, StoredAnalysis, Team
from app.providers.errors import ProviderRateLimitError, ProviderUnavailableError
from app.providers.mock import MockOddsProvider
from app.services.daily_scan import (
    AnalysisRepository,
    DailyScanService,
    coverage_of,
)
from app.services.match_analysis import (
    Coverage,
    MatchAnalysis,
    MatchAnalysisService,
    TeamSnapshot,
)
from app.services.team_names import normalize_team_name

D = Decimal
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


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


def _analysis(**overrides: object) -> MatchAnalysis:
    """Build an analysis with a known distribution."""
    analysis = MatchAnalysis(
        home_name="Alpha",
        away_name="Bravo",
        kickoff=NOW + timedelta(hours=4),
        competition="Championship",
        coverage=Coverage.FULLY_MODELLED,
        home=TeamSnapshot("Alpha", team_id=1, matches=20),
        away=TeamSnapshot("Bravo", team_id=2, matches=20),
    )
    analysis.probabilities = {"home": D("0.50"), "draw": D("0.28"), "away": D("0.22")}
    analysis.over_under = {
        "over_0.5": D("0.94"),
        "under_0.5": D("0.06"),
        "over_1.5": D("0.76"),
        "under_1.5": D("0.24"),
        "over_2.5": D("0.52"),
        "under_2.5": D("0.48"),
        "over_3.5": D("0.29"),
        "under_3.5": D("0.71"),
    }
    analysis.both_teams_score = D("0.54")
    for key, value in overrides.items():
        setattr(analysis, key, value)
    analysis.build_markets()
    return analysis


class TestDerivedMarkets:
    """Every market must follow from the same distribution."""

    def test_all_supported_markets_present(self) -> None:
        markets = _analysis().markets
        assert set(markets) == {
            "1X2",
            "Double chance",
            "Result",
            "Goals",
            "Both teams to score",
        }

    def test_one_x_two_sums_to_one(self) -> None:
        outcomes = _analysis().markets["1X2"]
        assert abs(sum(outcomes.values()) - D(1)) < D("0.0001")

    def test_double_chance_is_exactly_the_sum_of_its_parts(self) -> None:
        """Not modelled separately: an exact consequence of 1X2."""
        markets = _analysis().markets
        one_x_two = markets["1X2"]
        double = markets["Double chance"]

        assert double["1X (home or draw)"] == one_x_two["Home"] + one_x_two["Draw"]
        assert double["12 (home or away)"] == one_x_two["Home"] + one_x_two["Away"]
        assert double["X2 (draw or away)"] == one_x_two["Draw"] + one_x_two["Away"]

    def test_double_chance_probabilities_sum_to_two(self) -> None:
        """Each outcome appears in exactly two of the three combinations."""
        double = _analysis().markets["Double chance"]
        assert abs(sum(double.values()) - D(2)) < D("0.0001")

    def test_goals_lines_are_complementary(self) -> None:
        goals = _analysis().markets["Goals"]
        for line in ("0.5", "1.5", "2.5", "3.5"):
            total = goals[f"Over {line}"] + goals[f"Under {line}"]
            assert abs(total - D(1)) < D("0.0001")

    def test_goals_are_monotonic(self) -> None:
        """A higher line cannot be more likely to be exceeded."""
        goals = _analysis().markets["Goals"]
        overs = [goals[f"Over {line}"] for line in ("0.5", "1.5", "2.5", "3.5")]
        assert overs == sorted(overs, reverse=True)

    def test_btts_is_complementary(self) -> None:
        btts = _analysis().markets["Both teams to score"]
        assert abs(btts["Yes"] + btts["No"] - D(1)) < D("0.0001")

    def test_no_markets_without_probabilities(self) -> None:
        """Nothing is published when the models could not run."""
        analysis = MatchAnalysis(
            home_name="A",
            away_name="B",
            kickoff=NOW,
            competition=None,
            coverage=Coverage.UNSUPPORTED,
            home=TeamSnapshot("A"),
            away=TeamSnapshot("B"),
        )
        analysis.build_markets()
        assert analysis.markets == {}

    def test_goals_omitted_without_a_goal_model(self) -> None:
        """Markets that cannot be derived are absent, not defaulted."""
        analysis = _analysis(over_under={}, both_teams_score=None)
        assert "Goals" not in analysis.markets
        assert "Both teams to score" not in analysis.markets
        assert "1X2" in analysis.markets


class TestCoverageHonesty:
    """A fixture we cannot model must say so."""

    async def test_unresolved_teams_without_odds_are_unsupported(
        self, session: AsyncSession
    ) -> None:
        """With neither history nor prices there is genuinely nothing to say."""
        provider = MockOddsProvider(name="mock")
        event = await provider.get_event("evt-1001")
        stripped = event.model_copy(update={"markets": ()})
        analysis = await MatchAnalysisService(session).analyse_event(stripped, now=NOW)

        assert analysis.coverage is Coverage.UNSUPPORTED
        assert analysis.unavailable_reason
        assert "could not match" in analysis.unavailable_reason.lower()
        assert analysis.markets == {}

    async def test_unresolved_teams_with_odds_fall_back_to_the_market(
        self, session: AsyncSession
    ) -> None:
        provider = MockOddsProvider(name="mock")
        event = await provider.get_event("evt-1001")
        analysis = await MatchAnalysisService(session).analyse_event(event, now=NOW)

        assert analysis.coverage is Coverage.DATA_ONLY
        assert analysis.components_used == ("market",)

    async def test_thin_history_is_data_only(self, session: AsyncSession) -> None:
        """Two matches must not produce a confident distribution."""
        for name in ("Arsenal", "Chelsea"):
            session.add(
                Team(
                    canonical_name=name,
                    normalized_name=normalize_team_name(name),
                    sport="football",
                )
            )
        await session.flush()
        teams = (await session.execute(select(Team))).scalars().all()

        for index in range(2):
            session.add(
                HistoricalMatch(
                    provider_name="csv",
                    provider_match_id=f"m{index}",
                    home_team_id=teams[0].id,
                    away_team_id=teams[1].id,
                    season="2025/2026",
                    match_date=NOW - timedelta(days=30 + index),
                    home_goals=1,
                    away_goals=0,
                    data_version="v",
                    ingested_at=NOW,
                )
            )
        await session.flush()

        provider = MockOddsProvider(name="mock")
        event = await provider.get_event("evt-1001")
        analysis = await MatchAnalysisService(session).analyse_event(event, now=NOW)

        assert analysis.coverage is Coverage.DATA_ONLY
        assert "below the" in (analysis.unavailable_reason or "")
        # Thin history means our models cannot run — but the fixture is priced,
        # so the market's own view is published rather than an empty screen.
        assert analysis.components_used == ("market",)
        assert "market" in (analysis.unavailable_reason or "").lower()

    def test_every_grade_has_a_badge(self) -> None:
        for grade in Coverage:
            assert grade.badge


class TestDailyScan:
    """Bulk scanning stores results for instant retrieval."""

    async def test_stores_every_fixture(self, session: AsyncSession) -> None:
        report = await DailyScanService(session).scan(
            MockOddsProvider(name="mock"), fetch_odds=False, now=NOW
        )
        stored = int(
            (await session.execute(select(func.count()).select_from(StoredAnalysis))).scalar_one()
        )
        assert report.fixtures_seen > 0
        assert report.stored == stored > 0
        assert report.duplicates_skipped == 1, "the mock feed repeats one fixture"

    async def test_unmodellable_fixtures_are_still_stored(self, session: AsyncSession) -> None:
        """A user asking what is on today should see the whole card."""
        await DailyScanService(session).scan(
            MockOddsProvider(name="mock"), fetch_odds=False, now=NOW
        )
        records = (await session.execute(select(StoredAnalysis))).scalars().all()
        assert records
        # None can be modelled, but the priced ones still carry the market's
        # view rather than an empty screen.
        assert all(coverage_of(r) in {Coverage.UNSUPPORTED, Coverage.DATA_ONLY} for r in records)
        assert all(r.unavailable_reason for r in records)
        assert all("poisson" not in (r.components_used or []) for r in records)

    async def test_rescanning_replaces_rather_than_duplicates(self, session: AsyncSession) -> None:
        service = DailyScanService(session)
        provider = MockOddsProvider(name="mock")
        await service.scan(provider, fetch_odds=False, now=NOW)
        first = int(
            (await session.execute(select(func.count()).select_from(StoredAnalysis))).scalar_one()
        )
        await service.scan(provider, fetch_odds=False, now=NOW + timedelta(hours=1))
        second = int(
            (await session.execute(select(func.count()).select_from(StoredAnalysis))).scalar_one()
        )
        assert first == second

    async def test_quota_exhaustion_is_reported_not_raised(self, session: AsyncSession) -> None:
        """A spent allowance must not crash the scheduled job."""

        class Exhausted(MockOddsProvider):
            async def get_events(self, **_kwargs: object) -> tuple:  # type: ignore[override]
                raise ProviderRateLimitError("spent", retry_after_seconds=60)

        report = await DailyScanService(session).scan(Exhausted(), now=NOW)
        assert report.quota_exhausted
        assert report.stored == 0

    async def test_provider_outage_is_reported_not_raised(self, session: AsyncSession) -> None:
        class Down(MockOddsProvider):
            async def get_events(self, **_kwargs: object) -> tuple:  # type: ignore[override]
                raise ProviderUnavailableError("down")

        report = await DailyScanService(session).scan(Down(), now=NOW)
        assert report.errors == 1

    async def test_finished_fixtures_are_pruned(self, session: AsyncSession) -> None:
        session.add(
            StoredAnalysis(
                provider_name="mock",
                provider_event_id="old",
                home_name="A",
                away_name="B",
                kickoff=NOW - timedelta(days=3),
                coverage="unsupported",
                markets={},
                home_stats={},
                away_stats={},
                components_used=[],
                components_dropped=[],
                computed_at=NOW - timedelta(days=3),
            )
        )
        await session.flush()

        await DailyScanService(session).scan(
            MockOddsProvider(name="mock"), fetch_odds=False, now=NOW
        )
        remaining = (
            (await session.execute(select(StoredAnalysis.provider_event_id))).scalars().all()
        )
        assert "old" not in remaining

    async def test_report_summary_is_readable(self, session: AsyncSession) -> None:
        report = await DailyScanService(session).scan(
            MockOddsProvider(name="mock"), fetch_odds=False, now=NOW
        )
        assert "fixtures" in report.summary()
        assert "coverage" in report.summary()


class TestAnalysisRepository:
    """The bot reads; it never computes."""

    async def test_upcoming_is_ordered_by_kickoff(self, session: AsyncSession) -> None:
        await DailyScanService(session).scan(
            MockOddsProvider(name="mock"), fetch_odds=False, now=NOW
        )
        records = await AnalysisRepository(session).upcoming(now=NOW)
        kickoffs = [r.kickoff for r in records]
        assert kickoffs == sorted(kickoffs)

    async def test_finished_fixtures_are_excluded(self, session: AsyncSession) -> None:
        await DailyScanService(session).scan(
            MockOddsProvider(name="mock"), fetch_odds=False, now=NOW
        )
        later = await AnalysisRepository(session).upcoming(now=NOW + timedelta(days=10))
        assert later == []

    async def test_lookup_by_fixture_id(self, session: AsyncSession) -> None:
        await DailyScanService(session).scan(
            MockOddsProvider(name="mock"), fetch_odds=False, now=NOW
        )
        repository = AnalysisRepository(session)
        records = await repository.upcoming(now=NOW)
        found = await repository.get(records[0].provider_event_id)
        assert found is not None
        assert found.home_name == records[0].home_name

    async def test_unknown_fixture_returns_none(self, session: AsyncSession) -> None:
        assert await AnalysisRepository(session).get("nope") is None

    async def test_last_computed_is_reported(self, session: AsyncSession) -> None:
        await DailyScanService(session).scan(
            MockOddsProvider(name="mock"), fetch_odds=False, now=NOW
        )
        computed = await AnalysisRepository(session).last_computed()
        assert computed is not None


class TestSharedProvider:
    """One provider per process, so quota tracking actually works."""

    def test_no_key_yields_no_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.providers import live

        monkeypatch.delenv(live.API_KEY_ENV, raising=False)
        live.reset()
        assert live.live_odds_provider() is None

    def test_provider_instance_is_reused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rebuilding per call reset the budget and defeated quota tracking."""
        from app.providers import live

        monkeypatch.setenv(live.API_KEY_ENV, "a-key")
        live.reset()
        first = live.live_odds_provider()
        second = live.live_odds_provider()
        assert first is second
        live.reset()

    def test_budget_persists_across_lookups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.providers import live

        monkeypatch.setenv(live.API_KEY_ENV, "a-key")
        live.reset()
        provider = live.live_odds_provider()
        assert provider is not None
        provider.budget.spend(NOW)  # type: ignore[attr-defined]
        again = live.live_odds_provider()
        assert again.budget.used == 1  # type: ignore[attr-defined]
        live.reset()


class TestMarketOnlyFallback:
    """No history does not have to mean nothing to show.

    A fixture with odds carries real information even when our models cannot
    run. Withholding it leaves a user staring at "outside our coverage" for a
    match the whole world is betting on — while presenting a bookmaker's price
    as our own estimate would be a lie of attribution.
    """

    async def test_unresolved_teams_still_get_the_market_view(self, session: AsyncSession) -> None:
        provider = MockOddsProvider(name="mock")
        event = await provider.get_event("evt-1002")
        analysis = await MatchAnalysisService(session).analyse_event(event, now=NOW)

        assert analysis.coverage is Coverage.DATA_ONLY
        assert analysis.components_used == ("market",)
        assert analysis.markets["1X2"]

    async def test_market_probabilities_sum_to_one(self, session: AsyncSession) -> None:
        """The margin is removed the same way as for a modelled fixture."""
        provider = MockOddsProvider(name="mock")
        event = await provider.get_event("evt-1002")
        analysis = await MatchAnalysisService(session).analyse_event(event, now=NOW)

        total = sum(analysis.markets["1X2"].values())
        assert abs(total - D(1)) < D("0.001")

    async def test_attributed_to_the_market_not_to_us(self, session: AsyncSession) -> None:
        provider = MockOddsProvider(name="mock")
        event = await provider.get_event("evt-1002")
        analysis = await MatchAnalysisService(session).analyse_event(event, now=NOW)

        reason = (analysis.unavailable_reason or "").lower()
        assert "market" in reason
        assert "not a quantsport estimate" in reason

    async def test_never_graded_fully_modelled(self, session: AsyncSession) -> None:
        """A market view must never be mistaken for a modelled fixture."""
        provider = MockOddsProvider(name="mock")
        event = await provider.get_event("evt-1002")
        analysis = await MatchAnalysisService(session).analyse_event(event, now=NOW)

        assert analysis.coverage is not Coverage.FULLY_MODELLED
        assert "poisson" not in analysis.components_used

    async def test_without_odds_nothing_is_invented(self, session: AsyncSession) -> None:
        """The fallback needs real prices; it must not manufacture any."""
        provider = MockOddsProvider(name="mock")
        event = await provider.get_event("evt-1002")
        stripped = event.model_copy(update={"markets": ()})
        analysis = await MatchAnalysisService(session).analyse_event(stripped, now=NOW)

        assert analysis.coverage is Coverage.UNSUPPORTED
        assert analysis.markets == {}
        assert "nothing reliable to show" in (analysis.unavailable_reason or "")
