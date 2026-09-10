"""User activity: viewing history and saved fixtures.

Two separate ideas, deliberately kept apart. History is a passive record of
what someone looked at; saving is an explicit act. Merging them would mean
either losing the history when a user unsaves, or cluttering the saved list
with everything they ever tapped.

History is append-only, so "how often is this fixture opened" stays answerable
later. The most-recent view is derived at query time rather than by mutating a
row.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import (
    Base,
    CreatedAtMixin,
    IntPrimaryKeyMixin,
    JSONType,
    TimestampMixin,
)


class FixtureView(IntPrimaryKeyMixin, CreatedAtMixin, Base):
    """One occasion on which a user opened a fixture analysis."""

    __tablename__ = "fixture_views"
    __table_args__ = (
        Index("ix_fixture_views_user_created", "user_id", "created_at"),
        Index("ix_fixture_views_fixture", "provider_event_id"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)

    home_name: Mapped[str] = mapped_column(String(128), nullable=False)
    away_name: Mapped[str] = mapped_column(String(128), nullable=False)
    competition: Mapped[str | None] = mapped_column(String(128))
    kickoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Copied rather than joined.

    Stored analyses are pruned once a fixture finishes, so a join would make
    history vanish exactly when someone wants to look back at it.
    """


class SavedFixture(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A fixture a user explicitly kept."""

    __tablename__ = "saved_fixtures"
    __table_args__ = (
        UniqueConstraint("user_id", "provider_event_id", name="uq_saved_fixtures_user_fixture"),
        Index("ix_saved_fixtures_user", "user_id"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)

    home_name: Mapped[str] = mapped_column(String(128), nullable=False)
    away_name: Mapped[str] = mapped_column(String(128), nullable=False)
    competition: Mapped[str | None] = mapped_column(String(128))
    kickoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class UserPreferences(IntPrimaryKeyMixin, TimestampMixin, Base):
    """Per-user settings.

    One row per user, values in JSON. Preferences accumulate — favourite
    leagues, default filters, notification choices — and a column per setting
    would mean a migration for each.
    """

    __tablename__ = "user_preferences"
    __table_args__ = (UniqueConstraint("user_id", name="uq_user_preferences_user"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    settings: Mapped[dict[str, object]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
