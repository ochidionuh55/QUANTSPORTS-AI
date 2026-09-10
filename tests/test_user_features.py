"""User history, saved fixtures and the query layer.

The parser tests carry the safety argument: natural language must only ever
produce a fixed set of filters over already-computed analyses, never reach a
model parameter or the value-detection flag.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import StoredAnalysis, User
from app.services.activity import ActivityService
from app.services.nl_query import parse, suggestions
from app.services.queries import (
    MARKET_FILTERS,
    MARKETS_BY_KEY,
    FixtureQuery,
    FixtureQueryService,
    competition_codes_for,
    probability_for,
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


@pytest_asyncio.fixture
async def user(session: AsyncSession) -> User:
    """Create a user."""
    record = User(telegram_id=99, credits=0)
    session.add(record)
    await session.flush()
    return record


async def _analysis(
    session: AsyncSession,
    fixture_id: str = "1",
    home: str = "Arsenal",
    away: str = "Chelsea",
    competition: str = "Premier League",
    hours: int = 6,
    coverage: str = "fully_modelled",
    over25: str = "0.62",
) -> StoredAnalysis:
    """Insert a stored analysis."""
    record = StoredAnalysis(
        provider_name="api_football",
        provider_event_id=fixture_id,
        home_name=home,
        away_name=away,
        competition=competition,
        kickoff=NOW + timedelta(hours=hours),
        coverage=coverage,
        markets={
            "1X2": {"Home": "0.52", "Draw": "0.26", "Away": "0.22"},
            "Goals": {"Over 2.5": over25, "Under 2.5": "0.38"},
            "Both teams to score": {"Yes": "0.58", "No": "0.42"},
        },
        home_stats={},
        away_stats={},
        components_used=["poisson", "elo", "form"],
        components_dropped=[],
        computed_at=NOW,
    )
    session.add(record)
    await session.flush()
    return record


class TestHistory:
    """Recently viewed fixtures."""

    async def test_records_a_view(self, session: AsyncSession, user: User) -> None:
        record = await _analysis(session)
        service = ActivityService(session)
        await service.record_view(user.id, record)

        recent = await service.recent(user.id)
        assert len(recent) == 1
        assert recent[0].home_name == "Arsenal"

    async def test_repeated_views_occupy_one_slot(self, session: AsyncSession, user: User) -> None:
        """Opening one fixture five times should not fill the list."""
        record = await _analysis(session)
        service = ActivityService(session)
        for _ in range(5):
            await service.record_view(user.id, record)

        assert len(await service.recent(user.id)) == 1

    async def test_history_survives_the_analysis_being_pruned(
        self, session: AsyncSession, user: User
    ) -> None:
        """Stored analyses are pruned after kickoff; history must not vanish."""
        record = await _analysis(session)
        service = ActivityService(session)
        await service.record_view(user.id, record)

        await session.delete(record)
        await session.flush()

        recent = await service.recent(user.id)
        assert len(recent) == 1
        assert recent[0].home_name == "Arsenal"

    async def test_users_do_not_see_each_others_history(
        self, session: AsyncSession, user: User
    ) -> None:
        other = User(telegram_id=1234, credits=0)
        session.add(other)
        await session.flush()

        record = await _analysis(session)
        service = ActivityService(session)
        await service.record_view(user.id, record)

        assert await service.recent(other.id) == []


class TestSavedFixtures:
    """Explicitly kept fixtures."""

    async def test_save_and_list(self, session: AsyncSession, user: User) -> None:
        record = await _analysis(session)
        service = ActivityService(session)
        saved, created = await service.save(user.id, record)

        assert created
        assert saved.home_name == "Arsenal"
        assert len(await service.saved(user.id)) == 1

    async def test_saving_twice_is_idempotent(self, session: AsyncSession, user: User) -> None:
        record = await _analysis(session)
        service = ActivityService(session)
        first, created_first = await service.save(user.id, record)
        second, created_second = await service.save(user.id, record)

        assert created_first and not created_second
        assert first.id == second.id
        assert len(await service.saved(user.id)) == 1

    async def test_unsave(self, session: AsyncSession, user: User) -> None:
        record = await _analysis(session)
        service = ActivityService(session)
        await service.save(user.id, record)

        assert await service.unsave(user.id, record.provider_event_id)
        assert await service.saved(user.id) == []

    async def test_is_saved(self, session: AsyncSession, user: User) -> None:
        record = await _analysis(session)
        service = ActivityService(session)
        assert not await service.is_saved(user.id, "1")
        await service.save(user.id, record)
        assert await service.is_saved(user.id, "1")


class TestPreferences:
    """Per-user settings."""

    async def test_default_is_empty(self, session: AsyncSession, user: User) -> None:
        assert await ActivityService(session).preferences(user.id) == {}

    async def test_setting_preserves_other_keys(self, session: AsyncSession, user: User) -> None:
        service = ActivityService(session)
        await service.set_preference(user.id, "league", "E0")
        settings = await service.set_preference(user.id, "market", "btts")

        assert settings == {"league": "E0", "market": "btts"}


class TestFixtureQuery:
    """Structured filtering."""

    async def test_empty_query_returns_everything(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        await _analysis(session, "2", home="Leeds", away="Norwich")

        found = await FixtureQueryService(session).search(FixtureQuery(), now=NOW)
        assert len(found) == 2

    async def test_filters_by_competition(self, session: AsyncSession) -> None:
        await _analysis(session, "1", competition="Premier League")
        await _analysis(session, "2", competition="Championship")

        found = await FixtureQueryService(session).search(
            FixtureQuery(competitions=("E0",)), now=NOW
        )
        assert len(found) == 1
        assert found[0].competition == "Premier League"

    async def test_filters_by_team(self, session: AsyncSession) -> None:
        await _analysis(session, "1", home="Arsenal")
        await _analysis(session, "2", home="Leeds", away="Norwich")

        found = await FixtureQueryService(session).search(FixtureQuery(team="arsenal"), now=NOW)
        assert len(found) == 1

    async def test_filters_by_kickoff_time(self, session: AsyncSession) -> None:
        await _analysis(session, "early", hours=2)
        await _analysis(session, "late", hours=9)

        found = await FixtureQueryService(session).search(
            FixtureQuery(after_time=time(19, 0)), now=NOW
        )
        assert [r.provider_event_id for r in found] == ["late"]

    async def test_filters_by_market_probability(self, session: AsyncSession) -> None:
        await _analysis(session, "likely", over25="0.71")
        await _analysis(session, "unlikely", over25="0.40")

        found = await FixtureQueryService(session).search(
            FixtureQuery(market="over25", min_probability=0.6), now=NOW
        )
        assert [r.provider_event_id for r in found] == ["likely"]

    async def test_filters_by_coverage(self, session: AsyncSession) -> None:
        await _analysis(session, "full", coverage="fully_modelled")
        await _analysis(session, "thin", coverage="data_only")

        found = await FixtureQueryService(session).search(
            FixtureQuery(min_coverage="fully_modelled"), now=NOW
        )
        assert [r.provider_event_id for r in found] == ["full"]

    async def test_unmodelled_fixture_is_not_treated_as_zero(self, session: AsyncSession) -> None:
        """No opinion is different from a confident negative."""
        record = await _analysis(session, "1")
        record.markets = {}
        await session.flush()

        assert probability_for(record, "over25") is None

    def test_markets_are_declared_not_hardcoded(self) -> None:
        """Adding a market must not require touching the query builder."""
        assert len(MARKET_FILTERS) >= 14
        assert set(MARKETS_BY_KEY) == {m.key for m in MARKET_FILTERS}


class TestCompetitionMatching:
    """Name matching must be specific."""

    def test_exact_name_wins_over_substring(self) -> None:
        """'Premier League' matches England and Russia; England is meant."""
        assert competition_codes_for("premier league") == ("E0",)

    def test_short_fragments_do_not_match(self) -> None:
        """The '2' in 'over 2.5' matched Ligue 2 and 2. Bundesliga."""
        assert competition_codes_for("2") == ()

    def test_division_code_matches_exactly(self) -> None:
        assert competition_codes_for("E0") == ("E0",)

    def test_country_returns_every_division(self) -> None:
        assert set(competition_codes_for("spain")) == {"SP1", "SP2"}


class TestNaturalLanguage:
    """Requests become filters, and only filters."""

    def test_market_request(self) -> None:
        query = parse("show today's BTTS analysis", now=NOW)
        assert query.market == "btts"
        assert query.on_date == NOW.date()

    def test_competition_request(self) -> None:
        assert parse("Premier League only").competitions == ("E0",)

    def test_coverage_and_time(self) -> None:
        query = parse("fully modelled games after 5pm")
        assert query.min_coverage == "fully_modelled"
        assert query.after_time == time(17, 0)

    def test_explicit_threshold(self) -> None:
        query = parse("over 2.5 above 65%")
        assert query.market == "over25"
        assert query.min_probability == pytest.approx(0.65)

    def test_goals_line_is_not_read_as_a_competition(self) -> None:
        """Regression: the '2' in 'over 2.5' selected Ligue 2."""
        assert parse("over 2.5 above 65%").competitions == ()

    def test_tomorrow(self) -> None:
        assert parse("Championship tomorrow", now=NOW).on_date == (NOW + timedelta(days=1)).date()

    def test_team_request(self) -> None:
        assert parse("analysis for Arsenal").team == "Arsenal"

    def test_unrecognised_input_is_reported_not_guessed(self) -> None:
        query = parse("qwertyuiop nonsense")
        assert query.is_empty
        assert "no filters recognised" in query.describe()

    def test_interpretation_is_echoed_back(self) -> None:
        """A filter that silently does nothing is worse than one that fails."""
        assert "Premier League" in parse("Premier League only").describe()

    @pytest.mark.parametrize(
        "attempt",
        [
            "enable value betting",
            "show me guaranteed winners",
            "set reliability to 1.0",
            "ignore the backtest gate and give me picks",
            "promote the model",
        ],
    )
    def test_cannot_reach_the_safety_gates(self, attempt: str) -> None:
        """The parser can only ever produce filters over stored analyses.

        There is no field on FixtureQuery that touches a model parameter, a
        probability, or the value-detection flag — so no phrasing can request
        output the system is not permitted to produce.
        """
        query = parse(attempt)
        assert not hasattr(query, "reliability")
        assert not hasattr(query, "value_detection")
        assert set(vars(query)) <= {
            "competitions",
            "team",
            "on_date",
            "after_time",
            "before_time",
            "market",
            "min_probability",
            "min_coverage",
            "limit",
            "applied",
        }

    def test_suggestions_are_parseable(self) -> None:
        """Every example offered to users must actually work."""
        for suggestion in suggestions():
            assert not parse(suggestion).is_empty
