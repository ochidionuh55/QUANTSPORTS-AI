"""Operator analytics tests.

Retention is the figure that decides whether the product is working, so it has
to be honest: no dividing by tiny denominators, no counting the same person
twice, and no reporting a ratio from three opinions as satisfaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import FixtureView, User, UserFeedback
from app.services.analytics import AnalyticsService

NOW = datetime(2026, 3, 15, 12, tzinfo=UTC)


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


async def _user(session: AsyncSession, telegram_id: int) -> User:
    """Insert a user."""
    user = User(telegram_id=telegram_id, credits=0)
    session.add(user)
    await session.flush()
    return user


async def _view(
    session: AsyncSession, user: User, days_ago: float, competition: str = "E0"
) -> None:
    """Insert a fixture view at a point in the past."""
    view = FixtureView(
        user_id=user.id,
        provider_event_id=f"f{user.id}-{days_ago}",
        home_name="Alpha",
        away_name="Bravo",
        competition=competition,
        kickoff=NOW,
    )
    session.add(view)
    await session.flush()
    view.created_at = NOW - timedelta(days=days_ago)
    await session.flush()


class TestUserCounts:
    """Registrations and activity."""

    async def test_counts_users(self, session: AsyncSession) -> None:
        for index in range(3):
            await _user(session, index)

        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert snapshot.total_users == 3

    async def test_active_users_are_distinct(self, session: AsyncSession) -> None:
        """Opening five fixtures is one active user, not five."""
        user = await _user(session, 1)
        for index in range(5):
            await _view(session, user, days_ago=0.1 * index)

        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert snapshot.active_today == 1
        assert snapshot.views_today == 5

    async def test_activity_windows_are_separate(self, session: AsyncSession) -> None:
        recent = await _user(session, 1)
        older = await _user(session, 2)
        await _view(session, recent, days_ago=0.5)
        await _view(session, older, days_ago=20)

        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert snapshot.active_today == 1
        assert snapshot.active_week == 1
        assert snapshot.active_month == 2


class TestRetention:
    """The number that decides whether the product works."""

    async def test_measures_returning_users(self, session: AsyncSession) -> None:
        returning = [await _user(session, index) for index in range(6)]
        for user in returning:
            await _view(session, user, days_ago=10)
        for user in returning[:3]:
            await _view(session, user, days_ago=2)

        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert snapshot.returning_rate == pytest.approx(0.5)

    async def test_too_few_users_reports_nothing(self, session: AsyncSession) -> None:
        """Retention from three people is noise, not a metric."""
        for index in range(3):
            user = await _user(session, index)
            await _view(session, user, days_ago=10)

        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert snapshot.returning_rate is None

    async def test_no_history_reports_nothing(self, session: AsyncSession) -> None:
        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert snapshot.returning_rate is None


class TestFeedback:
    """Satisfaction needs a real sample."""

    async def test_small_samples_report_no_ratio(self, session: AsyncSession) -> None:
        user = await _user(session, 1)
        for _ in range(3):
            session.add(UserFeedback(user_id=user.id, verdict="up"))
        await session.flush()

        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert snapshot.feedback_up == 3
        assert snapshot.satisfaction is None

    async def test_ratio_appears_with_enough_responses(self, session: AsyncSession) -> None:
        user = await _user(session, 1)
        for index in range(12):
            session.add(UserFeedback(user_id=user.id, verdict="up" if index < 9 else "down"))
        await session.flush()

        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert snapshot.satisfaction == pytest.approx(0.75)


class TestEngagement:
    """Depth of use."""

    async def test_views_per_active_user(self, session: AsyncSession) -> None:
        for index in range(2):
            user = await _user(session, index)
            for view in range(3):
                await _view(session, user, days_ago=1 + view * 0.1)

        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert snapshot.views_per_active_user == pytest.approx(3.0)

    async def test_no_activity_reports_nothing(self, session: AsyncSession) -> None:
        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert snapshot.views_per_active_user is None

    async def test_top_competitions(self, session: AsyncSession) -> None:
        user = await _user(session, 1)
        for index in range(3):
            await _view(session, user, days_ago=1 + index * 0.1, competition="E0")
        await _view(session, user, days_ago=2, competition="SP1")

        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert snapshot.top_competitions[0] == ("E0", 3)

    async def test_daily_active_series(self, session: AsyncSession) -> None:
        user = await _user(session, 1)
        await _view(session, user, days_ago=0.5)

        snapshot = await AnalyticsService(session).snapshot(now=NOW)
        assert len(snapshot.daily_active) == 14
        assert snapshot.daily_active[-1][1] == 1


class TestEmptyProduct:
    """A brand-new deployment must not divide by zero."""

    async def test_snapshot_on_empty_database(self, session: AsyncSession) -> None:
        snapshot = await AnalyticsService(session).snapshot(now=NOW)

        assert snapshot.total_users == 0
        assert snapshot.acceptance_rate is None
        assert snapshot.satisfaction is None
        assert snapshot.views_per_active_user is None
        assert snapshot.returning_rate is None
