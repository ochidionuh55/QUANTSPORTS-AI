"""Phase 6 and 7 tests: market ingestion and the core quantitative models.

The margin-removal tests matter most. If margin removal is wrong, the market
prior is wrong, and every downstream probability inherits the error while
looking entirely plausible.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.enums import (
    ModellabilityStatus,
    OutcomeStatus,
    SnapshotType,
    StalenessStatus,
)
from app.database.models import Market, Match, OddsSnapshot, Outcome, Team
from app.providers.mock import MockOddsProvider
from app.providers.mock import fair_probabilities as mock_fair
from app.providers.models import ProviderRole
from app.quant.expected_value import (
    ValueAssessment,
    assess,
    edge,
    expected_value,
    kelly_fraction,
)
from app.quant.poisson import (
    LeagueAverages,
    TeamStrength,
    expected_goals,
    league_averages,
    match_probabilities,
    poisson_pmf,
    score_matrix,
    team_strength,
)
from app.quant.probability import (
    MarginMethod,
    ProbabilityError,
    fair_odds,
    fair_probabilities,
    get_strategy,
    implied_probability,
    overround,
)
from app.services.market_ingestion import (
    MarketIngestionService,
    refresh_staleness,
    staleness_of,
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


async def _count(session: AsyncSession, model: type) -> int:
    """Return a row count."""
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


class TestImpliedProbability:
    """Raw conversion and validation."""

    def test_even_money(self) -> None:
        assert implied_probability(D("2.0")) == D("0.5")

    @pytest.mark.parametrize("bad", [D("1.0"), D("0.5"), D("-2")])
    def test_rejects_impossible_odds(self, bad: Decimal) -> None:
        with pytest.raises(ProbabilityError):
            implied_probability(bad)

    def test_overround_detects_margin(self) -> None:
        book = [D("2.07"), D("3.45"), D("3.32")]
        assert D("1.05") < overround(book) < D("1.10")

    def test_fair_odds_round_trip(self) -> None:
        assert implied_probability(fair_odds(D("0.25"))) == D("0.25")

    def test_fair_odds_rejects_certainty(self) -> None:
        for bad in (D("0"), D("1"), D("1.5")):
            with pytest.raises(ProbabilityError):
                fair_odds(bad)


class TestMarginRemoval:
    """Every strategy must return a valid distribution."""

    BOOK: ClassVar[list[Decimal]] = [D("2.07"), D("3.45"), D("3.32")]

    @pytest.mark.parametrize("method", list(MarginMethod))
    def test_probabilities_sum_to_one(self, method: MarginMethod) -> None:
        result = fair_probabilities(self.BOOK, method)
        assert abs(sum(result) - D(1)) < D("0.0001")

    @pytest.mark.parametrize("method", list(MarginMethod))
    def test_probabilities_are_below_raw_implied(self, method: MarginMethod) -> None:
        """Removing margin must lower every probability."""
        result = fair_probabilities(self.BOOK, method)
        raw = [implied_probability(o) for o in self.BOOK]
        assert all(f < r for f, r in zip(result, raw, strict=True))

    @pytest.mark.parametrize("method", list(MarginMethod))
    def test_ordering_is_preserved(self, method: MarginMethod) -> None:
        """The favourite must stay the favourite."""
        result = fair_probabilities(self.BOOK, method)
        assert result[0] > result[1]
        assert result[0] > result[2]

    def test_methods_disagree(self) -> None:
        """If they agreed, comparing them in backtesting would be pointless."""
        proportional = fair_probabilities(self.BOOK, MarginMethod.PROPORTIONAL)
        shin = fair_probabilities(self.BOOK, MarginMethod.SHIN)
        assert proportional != shin

    def test_shin_takes_more_from_the_longshot(self) -> None:
        """Proportional scaling is biased; Shin corrects toward the favourite.

        A lopsided market makes the difference visible.
        """
        book = [D("1.20"), D("8.00"), D("15.00")]
        proportional = fair_probabilities(book, MarginMethod.PROPORTIONAL)
        shin = fair_probabilities(book, MarginMethod.SHIN)
        assert shin[-1] < proportional[-1]

    def test_single_outcome_rejected(self) -> None:
        with pytest.raises(ProbabilityError, match="complete market"):
            fair_probabilities([D("2.0")])

    def test_arbitrage_book_rejected(self) -> None:
        """A book summing below 1.0 is stale or wrong, not a gift."""
        with pytest.raises(ProbabilityError, match="at or below 1.0"):
            fair_probabilities([D("3.0"), D("3.0")])

    def test_unknown_method_rejected(self) -> None:
        with pytest.raises(ProbabilityError, match="Unknown margin method"):
            get_strategy("bayesian-vibes")

    def test_recovers_known_fair_probabilities(self) -> None:
        """Against the mock's untilted reference book, truth is known."""
        import asyncio

        provider = MockOddsProvider(role=ProviderRole.REFERENCE)

        async def run() -> dict[str, Decimal]:
            event = await provider.get_event("evt-1001")
            market = next(m for m in event.markets if m.external_id.endswith("1x2"))
            return {o.name: o.odds for o in market.outcomes}

        quoted = asyncio.run(run())
        truth = mock_fair("evt-1001", "1x2")
        recovered = fair_probabilities(list(quoted.values()), MarginMethod.PROPORTIONAL)
        for label, value in zip(quoted, recovered, strict=True):
            assert abs(value - truth[label]) < D("0.01")


class TestPoisson:
    """Goal model behaviour and its stated limits."""

    def test_pmf_sums_to_one(self) -> None:
        total = sum(poisson_pmf(k, 1.5) for k in range(30))
        assert abs(total - 1.0) < 1e-9

    def test_zero_rate_forces_zero_goals(self) -> None:
        assert poisson_pmf(0, 0.0) == 1.0
        assert poisson_pmf(1, 0.0) == 0.0

    def test_score_matrix_is_a_distribution(self) -> None:
        grid = score_matrix(1.5, 1.2)
        assert abs(sum(grid.values()) - D(1)) < D("0.0001")
        assert all(p >= 0 for p in grid.values())

    def test_outcome_probabilities_sum_to_one(self) -> None:
        result = match_probabilities(1.6, 1.1)
        total = result.home_win + result.draw + result.away_win
        assert abs(total - D(1)) < D("0.0001")

    def test_stronger_home_side_wins_more_often(self) -> None:
        strong = match_probabilities(2.2, 0.8)
        even = match_probabilities(1.3, 1.3)
        assert strong.home_win > even.home_win
        assert strong.home_win > strong.away_win

    def test_over_under_are_complementary(self) -> None:
        result = match_probabilities(1.5, 1.4)
        for line in (0.5, 1.5, 2.5, 3.5):
            over = result.over_under[f"over_{line}"]
            under = result.over_under[f"under_{line}"]
            assert abs(over + under - D(1)) < D("0.0001")

    def test_higher_rates_raise_over_probability(self) -> None:
        low = match_probabilities(0.8, 0.7)
        high = match_probabilities(2.1, 1.9)
        assert high.over_under["over_2.5"] > low.over_under["over_2.5"]

    def test_btts_rises_with_both_rates(self) -> None:
        assert (
            match_probabilities(1.8, 1.7).both_teams_score
            > match_probabilities(1.8, 0.3).both_teams_score
        )

    def test_league_averages(self) -> None:
        averages = league_averages([(2, 1), (1, 1), (3, 0), (0, 2)])
        assert averages.home_goals == 1.5
        assert averages.away_goals == 1.0
        assert averages.matches == 4

    def test_league_averages_require_data(self) -> None:
        with pytest.raises(ValueError, match="no matches"):
            league_averages([])

    def test_strength_flags_small_samples(self) -> None:
        """A team with two matches must not be trusted, nor silently shrunk."""
        averages = LeagueAverages(home_goals=1.5, away_goals=1.1, matches=100)
        sparse = team_strength(1, [(3, 0)], [(2, 1)], averages)
        assert sparse.matches_played == 2
        assert not sparse.is_reliable

    def test_strength_reflects_performance(self) -> None:
        averages = LeagueAverages(home_goals=1.5, away_goals=1.1, matches=100)
        prolific = team_strength(1, [(4, 0)] * 5, [(3, 1)] * 5, averages)
        toothless = team_strength(2, [(0, 2)] * 5, [(0, 3)] * 5, averages)
        assert prolific.attack > toothless.attack
        assert prolific.defence < toothless.defence

    def test_expected_goals_never_zero(self) -> None:
        """A rate of zero would make a clean sheet certain."""
        averages = LeagueAverages(home_goals=1.5, away_goals=1.1, matches=100)
        nobody = TeamStrength(1, attack=0.0, defence=0.0, matches_played=20)
        home, away = expected_goals(nobody, nobody, averages)
        assert home > 0
        assert away > 0

    def test_draws_are_underpredicted(self) -> None:
        """A documented Poisson weakness, asserted so it is not forgotten.

        Independence understates draws; Dixon-Coles corrects it in Phase 8.
        Observed top-flight draw rates sit near 25%.
        """
        assert match_probabilities(1.5, 1.2).draw < D("0.28")


class TestExpectedValue:
    """Value arithmetic and its guard rails."""

    def test_positive_when_price_exceeds_fair(self) -> None:
        assert expected_value(D("0.5"), D("2.5")) == D("0.25")

    def test_zero_at_fair_odds(self) -> None:
        assert expected_value(D("0.5"), D("2.0")) == D("0")

    def test_negative_when_price_is_short(self) -> None:
        assert expected_value(D("0.5"), D("1.8")) < 0

    def test_rejects_invalid_inputs(self) -> None:
        with pytest.raises(ProbabilityError):
            expected_value(D("0"), D("2.0"))
        with pytest.raises(ProbabilityError):
            expected_value(D("0.5"), D("1.0"))

    def test_edge_is_probability_space(self) -> None:
        assert edge(D("0.55"), D("0.50")) == D("0.05")

    def test_assess_uses_supplied_market_probability(self) -> None:
        with_margin = assess(D("0.55"), D("2.0"))
        without = assess(D("0.55"), D("2.0"), market_probability=D("0.48"))
        assert without.edge > with_margin.edge

    def test_filters_reject_thin_edges(self) -> None:
        thin = ValueAssessment(
            model_probability=D("0.51"),
            market_probability=D("0.50"),
            odds=D("2.0"),
            edge=D("0.01"),
            expected_value=D("0.02"),
        )
        assert thin.has_value
        assert not thin.passes()

    def test_filters_reject_long_prices(self) -> None:
        """Long shots are where model error is largest in relative terms."""
        longshot = assess(D("0.06"), D("25.0"))
        assert longshot.has_value
        assert not longshot.passes()

    def test_kelly_is_zero_without_edge(self) -> None:
        assert kelly_fraction(D("0.4"), D("2.0")) == D("0")

    def test_kelly_is_capped(self) -> None:
        """Full Kelly is optimal only if the probability is exactly right."""
        assert kelly_fraction(D("0.9"), D("5.0")) == D("0.25")


class TestMarketIngestion:
    """Provider events become canonical rows."""

    async def _seed_teams(self, session: AsyncSession) -> None:
        """Create canonical teams so fixtures can resolve."""
        for name in (
            "Arsenal",
            "Chelsea",
            "Manchester United",
            "Liverpool",
            "Real Madrid",
            "Sevilla",
            "Barcelona",
            "Valencia",
            "Everton",
            "Brentford",
            "Fulham",
            "Brighton and Hove Albion",
            "Girona",
            "Osasuna",
            "Manchester City",
            "Luton Town",
        ):
            session.add(
                Team(
                    canonical_name=name,
                    normalized_name=normalize_team_name(name),
                    sport="football",
                )
            )
        await session.flush()

    async def test_creates_matches_markets_outcomes_snapshots(self, session: AsyncSession) -> None:
        await self._seed_teams(session)
        report = await MarketIngestionService(session).ingest_events(
            MockOddsProvider(name="soft"), now=NOW
        )

        assert report.matches_created > 0
        assert report.outcomes_upserted > 0
        assert report.snapshots_written == report.outcomes_upserted
        assert await _count(session, Match) == report.matches_created
        assert await _count(session, OddsSnapshot) == report.snapshots_written

    async def test_duplicate_events_are_deduplicated(self, session: AsyncSession) -> None:
        """The mock feed repeats one fixture, as real feeds do."""
        await self._seed_teams(session)
        report = await MarketIngestionService(session).ingest_events(
            MockOddsProvider(name="soft"), now=NOW
        )
        assert report.duplicates_skipped == 1

    async def test_reingestion_updates_rather_than_duplicates(self, session: AsyncSession) -> None:
        await self._seed_teams(session)
        service = MarketIngestionService(session)
        provider = MockOddsProvider(name="soft")

        first = await service.ingest_events(provider, now=NOW)
        matches_after_first = await _count(session, Match)
        second = await service.ingest_events(provider, now=NOW + timedelta(minutes=5))

        assert second.matches_created == 0
        assert second.matches_updated == first.matches_created
        assert await _count(session, Match) == matches_after_first

    async def test_price_history_accumulates(self, session: AsyncSession) -> None:
        """Current price is state; the snapshot ledger is history."""
        await self._seed_teams(session)
        service = MarketIngestionService(session)
        provider = MockOddsProvider(name="soft")

        await service.ingest_events(provider, now=NOW)
        outcomes_after_first = await _count(session, Outcome)
        snapshots_after_first = await _count(session, OddsSnapshot)

        await service.ingest_events(provider, now=NOW + timedelta(minutes=10))

        assert await _count(session, Outcome) == outcomes_after_first
        assert await _count(session, OddsSnapshot) == snapshots_after_first * 2

    async def test_reference_and_tradeable_are_distinguished(self, session: AsyncSession) -> None:
        """The whole market-as-prior design depends on this separation."""
        await self._seed_teams(session)
        service = MarketIngestionService(session)
        await service.ingest_events(
            MockOddsProvider(name="soft", role=ProviderRole.TRADEABLE), now=NOW
        )
        await service.ingest_events(
            MockOddsProvider(name="sharp", role=ProviderRole.REFERENCE), now=NOW
        )

        roles = (await session.execute(select(OddsSnapshot.provider_role))).scalars().all()
        assert {str(r) for r in roles} == {"reference", "tradeable"}

    async def test_unresolvable_teams_are_flagged_not_dropped(self, session: AsyncSession) -> None:
        """A coverage gap must be visible, not look like an empty result."""
        report = await MarketIngestionService(session).ingest_events(
            MockOddsProvider(name="soft"), now=NOW
        )

        assert report.matches_created > 0
        assert report.unmodellable
        matches = (await session.execute(select(Match))).scalars().all()
        assert all(m.modellability is ModellabilityStatus.UNRESOLVED_TEAMS for m in matches)
        assert all(m.modellability_note for m in matches)

    async def test_resolved_fixtures_are_modellable(self, session: AsyncSession) -> None:
        await self._seed_teams(session)
        await MarketIngestionService(session).ingest_events(MockOddsProvider(name="soft"), now=NOW)
        matches = (await session.execute(select(Match))).scalars().all()
        modellable = [m for m in matches if m.modellability is ModellabilityStatus.MODELLABLE]
        assert modellable
        assert all(m.home_team_id and m.away_team_id for m in modellable)

    async def test_odds_feed_never_creates_teams(self, session: AsyncSession) -> None:
        """Teams minted from a bookmaker would have no history attached."""
        await MarketIngestionService(session).ingest_events(MockOddsProvider(name="soft"), now=NOW)
        assert await _count(session, Team) == 0

    async def test_suspended_outcomes_are_recorded(self, session: AsyncSession) -> None:
        await self._seed_teams(session)
        await MarketIngestionService(session).ingest_events(MockOddsProvider(name="soft"), now=NOW)
        statuses = (await session.execute(select(Outcome.status))).scalars().all()
        assert OutcomeStatus.SUSPENDED in set(statuses)

    async def test_snapshot_type_is_recorded(self, session: AsyncSession) -> None:
        await self._seed_teams(session)
        await MarketIngestionService(session).ingest_events(
            MockOddsProvider(name="soft"),
            snapshot_type=SnapshotType.CLOSING,
            now=NOW,
        )
        types = (await session.execute(select(OddsSnapshot.snapshot_type))).scalars().all()
        assert all(t is SnapshotType.CLOSING for t in types)

    async def test_markets_belong_to_their_match(self, session: AsyncSession) -> None:
        await self._seed_teams(session)
        await MarketIngestionService(session).ingest_events(MockOddsProvider(name="soft"), now=NOW)
        markets = (await session.execute(select(Market))).scalars().all()
        assert markets
        assert all(m.match_id is not None for m in markets)


class TestFreshness:
    """A stale price must never reach a booking code."""

    def _outcome(self, fetched: datetime, ttl: timedelta) -> Outcome:
        """Build an unsaved outcome with explicit freshness fields."""
        return Outcome(
            market_id=1,
            provider_outcome_id="x",
            outcome_name="Home",
            odds=D("2.0"),
            status=OutcomeStatus.ACTIVE,
            last_fetched_at=fetched,
            expires_at=fetched + ttl,
            staleness=StalenessStatus.FRESH,
        )

    def test_fresh_within_ttl(self) -> None:
        outcome = self._outcome(NOW, timedelta(minutes=15))
        assert staleness_of(outcome, NOW + timedelta(minutes=5)) is StalenessStatus.FRESH

    def test_stale_just_past_ttl(self) -> None:
        outcome = self._outcome(NOW, timedelta(minutes=15))
        assert staleness_of(outcome, NOW + timedelta(minutes=20)) is StalenessStatus.STALE

    def test_expired_well_past_ttl(self) -> None:
        outcome = self._outcome(NOW, timedelta(minutes=15))
        assert staleness_of(outcome, NOW + timedelta(hours=6)) is StalenessStatus.EXPIRED

    async def test_refresh_updates_stored_state(self, session: AsyncSession) -> None:
        team_names = ("Arsenal", "Chelsea")
        for name in team_names:
            session.add(
                Team(
                    canonical_name=name,
                    normalized_name=normalize_team_name(name),
                    sport="football",
                )
            )
        await session.flush()

        await MarketIngestionService(session).ingest_events(MockOddsProvider(name="soft"), now=NOW)
        counts = await refresh_staleness(session, now=NOW + timedelta(hours=8))
        assert counts.get("expired", 0) > 0
        assert counts.get("fresh", 0) == 0
