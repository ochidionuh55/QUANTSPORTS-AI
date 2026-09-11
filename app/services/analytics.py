"""Operator analytics.

What is actually happening in the product: how many people, how often, what
they use, and whether they come back.

**Counted from what the product already stores.** Registrations, fixture views,
saved fixtures, follows and feedback are all recorded for their own reasons;
this module reads them rather than adding tracking. No new collection, no
personal data beyond the Telegram id already needed to reply to someone.

**Retention is the number that matters.** Total users only ever rises and so
flatters continuously. The share of last week's users who returned this week is
the figure that tells you whether the product is worth opening twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.core.logging import get_logger
from app.database.models import (
    FixtureView,
    HighlightFollow,
    HighlightSelection,
    SavedFixture,
    StoredAnalysis,
    User,
    UserFeedback,
)

logger = get_logger(__name__)


@dataclass
class UsageSnapshot:
    """The state of the product right now."""

    total_users: int = 0
    accepted_terms: int = 0
    active_today: int = 0
    active_week: int = 0
    active_month: int = 0
    new_today: int = 0
    new_week: int = 0

    views_today: int = 0
    views_week: int = 0
    views_all: int = 0

    saved_fixtures: int = 0
    follows: int = 0
    feedback_up: int = 0
    feedback_down: int = 0

    fixtures_upcoming: int = 0
    coverage: dict[str, int] = field(default_factory=dict)
    highlights_today: int = 0
    last_scan: datetime | None = None

    returning_rate: float | None = None
    """Share of last week's active users who were active again this week.

    ``None`` when last week had too few users to divide by — a retention figure
    from three people is noise.
    """

    daily_active: list[tuple[date, int]] = field(default_factory=list)
    top_competitions: list[tuple[str, int]] = field(default_factory=list)

    @property
    def acceptance_rate(self) -> float | None:
        """Share of registered users who passed the age gate."""
        if not self.total_users:
            return None
        return self.accepted_terms / self.total_users

    @property
    def views_per_active_user(self) -> float | None:
        """Depth of use this week."""
        if not self.active_week:
            return None
        return self.views_week / self.active_week

    @property
    def feedback_total(self) -> int:
        """All feedback received."""
        return self.feedback_up + self.feedback_down

    @property
    def satisfaction(self) -> float | None:
        """Share of feedback that was positive.

        ``None`` below ten responses: a ratio from three opinions says nothing.
        """
        if self.feedback_total < 10:
            return None
        return self.feedback_up / self.feedback_total


class AnalyticsService:
    """Reads product usage from existing tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def snapshot(self, now: datetime | None = None) -> UsageSnapshot:
        """Build the operator dashboard."""
        moment = now or datetime.now(UTC)
        today = moment - timedelta(days=1)
        week = moment - timedelta(days=7)
        month = moment - timedelta(days=30)
        snapshot = UsageSnapshot()

        snapshot.total_users = await self._count(select(func.count()).select_from(User))
        snapshot.accepted_terms = await self._count(
            select(func.count()).select_from(User).where(User.terms_accepted_at.isnot(None))
        )
        snapshot.new_today = await self._count(
            select(func.count()).select_from(User).where(User.created_at >= today)
        )
        snapshot.new_week = await self._count(
            select(func.count()).select_from(User).where(User.created_at >= week)
        )

        snapshot.active_today = await self._active_since(today)
        snapshot.active_week = await self._active_since(week)
        snapshot.active_month = await self._active_since(month)

        snapshot.views_today = await self._count(
            select(func.count()).select_from(FixtureView).where(FixtureView.created_at >= today)
        )
        snapshot.views_week = await self._count(
            select(func.count()).select_from(FixtureView).where(FixtureView.created_at >= week)
        )
        snapshot.views_all = await self._count(select(func.count()).select_from(FixtureView))

        snapshot.saved_fixtures = await self._count(select(func.count()).select_from(SavedFixture))
        snapshot.follows = await self._count(
            select(func.count())
            .select_from(HighlightFollow)
            .where(HighlightFollow.active.is_(True))
        )
        snapshot.feedback_up = await self._count(
            select(func.count()).select_from(UserFeedback).where(UserFeedback.verdict == "up")
        )
        snapshot.feedback_down = await self._count(
            select(func.count()).select_from(UserFeedback).where(UserFeedback.verdict == "down")
        )

        snapshot.fixtures_upcoming = await self._count(
            select(func.count()).select_from(StoredAnalysis).where(StoredAnalysis.kickoff >= moment)
        )
        snapshot.highlights_today = await self._count(
            select(func.count())
            .select_from(HighlightSelection)
            .where(
                HighlightSelection.selection_date == moment.date(),
                HighlightSelection.source == "live",
            )
        )

        coverage_rows = await self._session.execute(
            select(StoredAnalysis.coverage, func.count())
            .where(StoredAnalysis.kickoff >= moment)
            .group_by(StoredAnalysis.coverage)
        )
        snapshot.coverage = {str(grade): int(count) for grade, count in coverage_rows}

        snapshot.last_scan = (
            await self._session.execute(
                select(StoredAnalysis.computed_at)
                .order_by(StoredAnalysis.computed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        snapshot.returning_rate = await self._returning_rate(moment)
        snapshot.daily_active = await self._daily_active(moment)
        snapshot.top_competitions = await self._top_competitions(week)
        return snapshot

    async def _count(self, statement: Select[tuple[int]]) -> int:
        """Run a count query."""
        result = await self._session.execute(statement)
        return int(result.scalar_one())

    async def _active_since(self, moment: datetime) -> int:
        """Distinct users who opened a fixture since a point in time."""
        result = await self._session.execute(
            select(func.count(distinct(FixtureView.user_id))).where(
                FixtureView.created_at >= moment
            )
        )
        return int(result.scalar_one())

    async def _returning_rate(self, moment: datetime) -> float | None:
        """Share of last week's users who came back this week.

        The honest engagement measure. Total users only rises, so it always
        looks like growth; retention is what says whether anyone finds the
        product worth a second visit.
        """
        this_week = moment - timedelta(days=7)
        last_week = moment - timedelta(days=14)

        previous = await self._session.execute(
            select(distinct(FixtureView.user_id)).where(
                FixtureView.created_at >= last_week,
                FixtureView.created_at < this_week,
            )
        )
        previous_ids = {row[0] for row in previous.all()}
        if len(previous_ids) < 5:
            return None

        current = await self._session.execute(
            select(distinct(FixtureView.user_id)).where(FixtureView.created_at >= this_week)
        )
        current_ids = {row[0] for row in current.all()}
        return len(previous_ids & current_ids) / len(previous_ids)

    async def _daily_active(self, moment: datetime, days: int = 14) -> list[tuple[date, int]]:
        """Distinct users per day, oldest first."""
        rows: list[tuple[date, int]] = []
        for offset in range(days - 1, -1, -1):
            day_end = moment - timedelta(days=offset)
            day_start = day_end - timedelta(days=1)
            result = await self._session.execute(
                select(func.count(distinct(FixtureView.user_id))).where(
                    FixtureView.created_at >= day_start,
                    FixtureView.created_at < day_end,
                )
            )
            rows.append((day_end.date(), int(result.scalar_one())))
        return rows

    async def _top_competitions(self, since: datetime, limit: int = 8) -> list[tuple[str, int]]:
        """Which competitions users actually open."""
        result = await self._session.execute(
            select(FixtureView.competition, func.count())
            .where(
                FixtureView.created_at >= since,
                FixtureView.competition.isnot(None),
            )
            .group_by(FixtureView.competition)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [(str(name), int(count)) for name, count in result.all()]
