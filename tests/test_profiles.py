"""Statistical profile tests.

Profiles are the part of QUANTSPORT that does not depend on a model being any
good. A user who distrusts every forecast can still use them, so they must be
counts of what happened — never estimates, never extrapolations.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import Competition, HistoricalMatch, Team
from app.services.profiles import MIN_SAMPLE, ProfileService, SplitRecord
from app.services.team_names import normalize_team_name

NOW = datetime(2026, 6, 1, tzinfo=UTC)


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


async def _team(session: AsyncSession, name: str) -> Team:
    """Insert a club."""
    team = Team(
        canonical_name=name,
        normalized_name=normalize_team_name(name),
        country="England",
        sport="football",
    )
    session.add(team)
    await session.flush()
    return team


async def _match(
    session: AsyncSession,
    home: Team,
    away: Team,
    home_goals: int,
    away_goals: int,
    days_ago: int,
    competition_id: int | None = None,
    index: int = 0,
) -> None:
    """Insert one completed match."""
    session.add(
        HistoricalMatch(
            provider_name="csv",
            provider_match_id=f"m-{home.id}-{away.id}-{days_ago}-{index}",
            home_team_id=home.id,
            away_team_id=away.id,
            competition_id=competition_id,
            season="2025/2026",
            match_date=(NOW - timedelta(days=days_ago)).date(),
            home_goals=home_goals,
            away_goals=away_goals,
            data_version="v",
            ingested_at=NOW,
        )
    )
    await session.flush()


class TestSplitRecord:
    """Counting is the whole contract."""

    def test_counts_results(self) -> None:
        record = SplitRecord()
        record.add(2, 1)
        record.add(1, 1)
        record.add(0, 3)

        assert (record.played, record.won, record.drawn, record.lost) == (3, 1, 1, 1)
        assert record.points_per_game == pytest.approx(4 / 3)

    def test_counts_goals_markets(self) -> None:
        record = SplitRecord()
        record.add(2, 1)  # 3 goals, both scored
        record.add(0, 0)  # 0 goals, clean sheet, failed to score

        assert record.over_2_5 == 1
        assert record.both_scored == 1
        assert record.clean_sheets == 1
        assert record.failed_to_score == 1

    def test_small_samples_report_no_rate(self) -> None:
        """A percentage of four matches is noise dressed as a statistic."""
        record = SplitRecord()
        for _ in range(MIN_SAMPLE - 1):
            record.add(1, 0)

        assert not record.is_meaningful
        assert record.rate(record.over_2_5) is None

    def test_rates_appear_once_the_sample_is_adequate(self) -> None:
        record = SplitRecord()
        for _ in range(MIN_SAMPLE):
            record.add(2, 1)

        assert record.is_meaningful
        assert record.rate(record.over_2_5) == 1.0


class TestTeamProfile:
    """A club's full record."""

    async def test_reports_results_and_goals(self, session: AsyncSession) -> None:
        home = await _team(session, "Alpha")
        away = await _team(session, "Bravo")
        for index in range(8):
            await _match(session, home, away, 2, 1, days_ago=10 + index, index=index)

        profile = await ProfileService(session).team_profile(home.id, now=NOW)
        assert profile is not None
        assert profile.overall.played == 8
        assert profile.overall.won == 8
        assert profile.overall.scored == 16

    async def test_separates_home_from_away(self, session: AsyncSession) -> None:
        """The figure a user wants when asking whether a side travels badly."""
        alpha = await _team(session, "Alpha")
        bravo = await _team(session, "Bravo")
        for index in range(8):
            await _match(session, alpha, bravo, 3, 0, days_ago=10 + index, index=index)
        for index in range(8):
            await _match(session, bravo, alpha, 3, 0, days_ago=30 + index, index=index)

        profile = await ProfileService(session).team_profile(alpha.id, now=NOW)
        assert profile is not None
        assert profile.home.won == 8
        assert profile.away.lost == 8
        assert profile.home_advantage == pytest.approx(3.0)

    async def test_recent_form_is_newest_last(self, session: AsyncSession) -> None:
        alpha = await _team(session, "Alpha")
        bravo = await _team(session, "Bravo")
        await _match(session, alpha, bravo, 3, 0, days_ago=30, index=1)
        await _match(session, alpha, bravo, 0, 3, days_ago=1, index=2)

        profile = await ProfileService(session).team_profile(alpha.id, now=NOW)
        assert profile is not None
        assert profile.form_string.endswith("L")
        assert profile.form_string.startswith("W")

    async def test_unknown_team_returns_none(self, session: AsyncSession) -> None:
        assert await ProfileService(session).team_profile(999_999) is None

    async def test_team_with_no_recent_matches(self, session: AsyncSession) -> None:
        """Said plainly rather than shown as zeroes."""
        alpha = await _team(session, "Alpha")
        profile = await ProfileService(session).team_profile(alpha.id, now=NOW)

        assert profile is not None
        assert not profile.has_data

    async def test_search_finds_by_fragment(self, session: AsyncSession) -> None:
        await _team(session, "Manchester United")
        await _team(session, "Manchester City")

        found = await ProfileService(session).find_teams("manchester")
        assert len(found) == 2

    async def test_search_ignores_very_short_queries(self, session: AsyncSession) -> None:
        """A single letter would match half the database."""
        await _team(session, "Arsenal")
        assert await ProfileService(session).find_teams("a") == []


class TestHeadToHead:
    """The record between two clubs."""

    async def test_counts_from_the_nominated_side(self, session: AsyncSession) -> None:
        """Wins mean that club winning, wherever it was played."""
        alpha = await _team(session, "Alpha")
        bravo = await _team(session, "Bravo")
        await _match(session, alpha, bravo, 2, 0, days_ago=100, index=1)
        await _match(session, bravo, alpha, 0, 1, days_ago=50, index=2)

        record = await ProfileService(session).head_to_head(alpha.id, bravo.id)
        assert record.played == 2
        assert record.home_wins == 2
        assert record.away_wins == 0

    async def test_no_meetings_is_stated(self, session: AsyncSession) -> None:
        alpha = await _team(session, "Alpha")
        bravo = await _team(session, "Bravo")

        record = await ProfileService(session).head_to_head(alpha.id, bravo.id)
        assert not record.has_data

    async def test_rates_need_enough_meetings(self, session: AsyncSession) -> None:
        alpha = await _team(session, "Alpha")
        bravo = await _team(session, "Bravo")
        await _match(session, alpha, bravo, 3, 3, days_ago=10, index=1)

        record = await ProfileService(session).head_to_head(alpha.id, bravo.id)
        assert record.rate(record.over_2_5) is None


class TestLeagueProfile:
    """How a competition actually behaves."""

    async def test_aggregates_a_competition(self, session: AsyncSession) -> None:
        competition = Competition(
            canonical_name="Premier League",
            normalized_name=normalize_team_name("Premier League"),
            country="England",
            sport="football",
        )
        session.add(competition)
        await session.flush()

        alpha = await _team(session, "Alpha")
        bravo = await _team(session, "Bravo")
        for index in range(60):
            await _match(
                session,
                alpha,
                bravo,
                2,
                1,
                days_ago=10 + index,
                competition_id=competition.id,
                index=index,
            )

        profile = await ProfileService(session).league_profile("Premier League", now=NOW)
        assert profile.matches == 60
        assert profile.goals_per_game == pytest.approx(3.0)
        assert profile.rate(profile.over_2_5) == 1.0

    async def test_small_league_reports_no_rates(self, session: AsyncSession) -> None:
        competition = Competition(
            canonical_name="Tiny League",
            normalized_name=normalize_team_name("Tiny League"),
            country="Nowhere",
            sport="football",
        )
        session.add(competition)
        await session.flush()

        alpha = await _team(session, "Alpha")
        bravo = await _team(session, "Bravo")
        await _match(session, alpha, bravo, 1, 0, days_ago=5, competition_id=competition.id)

        profile = await ProfileService(session).league_profile("Tiny League", now=NOW)
        assert profile.has_data
        assert profile.rate(profile.draws) is None

    async def test_unknown_league_has_no_data(self, session: AsyncSession) -> None:
        profile = await ProfileService(session).league_profile("Nowhere League")
        assert not profile.has_data


class TestNoFabrication:
    """Profiles must never estimate."""

    async def test_nothing_is_reported_without_matches(self, session: AsyncSession) -> None:
        alpha = await _team(session, "Alpha")
        profile = await ProfileService(session).team_profile(alpha.id, now=NOW)

        assert profile is not None
        assert profile.overall.played == 0
        assert profile.overall.points_per_game is None
        assert profile.overall.goals_per_game is None
        assert profile.home_advantage is None

    async def test_old_matches_fall_outside_the_window(self, session: AsyncSession) -> None:
        """Squads and managers change; a decade-old record is not this club."""
        alpha = await _team(session, "Alpha")
        bravo = await _team(session, "Bravo")
        await _match(session, alpha, bravo, 5, 0, days_ago=4000, index=1)

        profile = await ProfileService(session).team_profile(alpha.id, now=NOW)
        assert profile is not None
        assert profile.overall.played == 0
