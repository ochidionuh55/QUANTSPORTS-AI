"""Canonical identity for teams and competitions.

This is the least glamorous part of the system and the one most likely to
break everything quietly. If the odds provider says "Man Utd" and the history
provider says "Manchester United", an unresolved alias splits one club into two
entities, fragments its Elo rating, and corrupts every estimate downstream
without raising a single error.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, IntPrimaryKeyMixin, JSONType, TimestampMixin, enum_column
from app.database.enums import ReviewStatus


class Competition(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A canonical league or tournament."""

    __tablename__ = "competitions"

    canonical_name: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(160), nullable=False)
    country: Mapped[str | None] = mapped_column(String(80))
    sport: Mapped[str] = mapped_column(String(32), default="football", nullable=False)

    tier: Mapped[int | None] = mapped_column()
    """Division level, where known. 1 = top flight. Feeds competition strength."""

    has_historical_coverage: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    """Whether we hold enough history to model fixtures in this competition."""

    external_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )

    teams: Mapped[list[Team]] = relationship(back_populates="competition")

    __table_args__ = (
        UniqueConstraint(
            "sport", "country", "normalized_name", name="uq_competitions_sport_country_name"
        ),
        Index("ix_competitions_has_historical_coverage", "has_historical_coverage"),
    )


class Team(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A canonical team.

    All ratings, form and historical statistics attach here, never to a
    provider-supplied name string.
    """

    __tablename__ = "teams"

    canonical_name: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(160), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(80))
    sport: Mapped[str] = mapped_column(String(32), default="football", nullable=False)

    competition_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("competitions.id", ondelete="SET NULL")
    )
    """Primary competition, for disambiguation only. Teams move between tiers."""

    external_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )

    competition: Mapped[Competition | None] = relationship(back_populates="teams")
    aliases: Mapped[list[TeamAlias]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "sport", "country", "normalized_name", name="uq_teams_sport_country_normalized_name"
        ),
        Index("ix_teams_normalized_name", "normalized_name"),
    )


class TeamAlias(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A provider-specific name mapped to a canonical team.

    Resolution order is exact alias, then external ID, then fuzzy candidate
    search. A fuzzy match below the confidence threshold enters review rather
    than being accepted.
    """

    __tablename__ = "team_aliases"

    team_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )

    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    alias: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(160), nullable=False)
    external_team_id: Mapped[str | None] = mapped_column(String(128))

    match_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    """0.0000-1.0000. Null for aliases created by hand."""

    review_status: Mapped[ReviewStatus] = mapped_column(
        enum_column(ReviewStatus, "review_status"),
        default=ReviewStatus.PENDING_REVIEW,
        nullable=False,
    )
    is_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    review_note: Mapped[str | None] = mapped_column(Text)

    team: Mapped[Team] = relationship(back_populates="aliases")

    __table_args__ = (
        UniqueConstraint(
            "provider_name", "normalized_alias", name="uq_team_aliases_provider_normalized_alias"
        ),
        CheckConstraint(
            "match_confidence IS NULL OR (match_confidence >= 0 AND match_confidence <= 1)",
            name="match_confidence_is_a_probability",
        ),
        Index("ix_team_aliases_review_status", "review_status"),
        Index("ix_team_aliases_external_team_id", "provider_name", "external_team_id"),
    )
