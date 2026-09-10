"""User activity: history, saved fixtures and preferences.

Recording a view is deliberately cheap and never blocks the response. If it
fails, the user still gets their analysis — losing a history row is a far
smaller cost than failing the thing they asked for.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import (
    FixtureView,
    SavedFixture,
    StoredAnalysis,
    UserPreferences,
)

logger = get_logger(__name__)

HISTORY_LIMIT = 10
SAVED_LIMIT = 25


class ActivityService:
    """History, saved fixtures and preferences for one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_view(self, user_id: int, record: StoredAnalysis) -> FixtureView:
        """Note that a user opened a fixture.

        Fixture details are copied rather than referenced: stored analyses are
        pruned once a match finishes, so a foreign key would make history
        disappear exactly when someone wants to look back.
        """
        view = FixtureView(
            user_id=user_id,
            provider_event_id=record.provider_event_id,
            home_name=record.home_name,
            away_name=record.away_name,
            competition=record.competition,
            kickoff=record.kickoff,
        )
        self._session.add(view)
        await self._session.flush()
        return view

    async def recent(self, user_id: int, limit: int = HISTORY_LIMIT) -> list[FixtureView]:
        """Return distinct fixtures a user viewed, most recent first.

        Deduplicated in Python: opening one fixture five times should occupy
        one slot in a ten-item list, not five.
        """
        result = await self._session.execute(
            select(FixtureView)
            .where(FixtureView.user_id == user_id)
            .order_by(FixtureView.created_at.desc())
            .limit(limit * 5)
        )
        seen: set[str] = set()
        distinct: list[FixtureView] = []
        for view in result.scalars().all():
            if view.provider_event_id in seen:
                continue
            seen.add(view.provider_event_id)
            distinct.append(view)
            if len(distinct) >= limit:
                break
        return distinct

    async def save(
        self, user_id: int, record: StoredAnalysis, note: str | None = None
    ) -> tuple[SavedFixture, bool]:
        """Save a fixture, or return the existing entry.

        Returns:
            ``(saved, created)``.
        """
        existing = await self._session.execute(
            select(SavedFixture).where(
                SavedFixture.user_id == user_id,
                SavedFixture.provider_event_id == record.provider_event_id,
            )
        )
        found = existing.scalar_one_or_none()
        if found is not None:
            return found, False

        saved = SavedFixture(
            user_id=user_id,
            provider_event_id=record.provider_event_id,
            home_name=record.home_name,
            away_name=record.away_name,
            competition=record.competition,
            kickoff=record.kickoff,
            note=note,
        )
        self._session.add(saved)
        await self._session.flush()
        return saved, True

    async def unsave(self, user_id: int, provider_event_id: str) -> bool:
        """Remove a saved fixture. Returns whether anything was removed."""
        result = await self._session.execute(
            delete(SavedFixture).where(
                SavedFixture.user_id == user_id,
                SavedFixture.provider_event_id == provider_event_id,
            )
        )
        return bool(result.rowcount)

    async def is_saved(self, user_id: int, provider_event_id: str) -> bool:
        """Whether a user has saved a fixture."""
        result = await self._session.execute(
            select(SavedFixture.id).where(
                SavedFixture.user_id == user_id,
                SavedFixture.provider_event_id == provider_event_id,
            )
        )
        return result.first() is not None

    async def saved(
        self, user_id: int, limit: int = SAVED_LIMIT, now: datetime | None = None
    ) -> list[SavedFixture]:
        """Return saved fixtures, soonest kickoff first."""
        result = await self._session.execute(
            select(SavedFixture)
            .where(SavedFixture.user_id == user_id)
            .order_by(SavedFixture.kickoff)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def preferences(self, user_id: int) -> dict[str, object]:
        """Return a user's settings, defaulting to empty."""
        result = await self._session.execute(
            select(UserPreferences).where(UserPreferences.user_id == user_id)
        )
        found = result.scalar_one_or_none()
        return dict(found.settings) if found and found.settings else {}

    async def set_preference(self, user_id: int, key: str, value: object) -> dict[str, object]:
        """Set one setting, leaving the rest untouched."""
        result = await self._session.execute(
            select(UserPreferences).where(UserPreferences.user_id == user_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            record = UserPreferences(user_id=user_id, settings={})
            self._session.add(record)

        settings = dict(record.settings or {})
        settings[key] = value
        record.settings = settings
        await self._session.flush()
        return settings
