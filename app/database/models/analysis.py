"""Stored match analysis.

The bot must not compute anything when a user taps. A fixture analysis needs
several database queries plus an Elo replay, and doing that per tap makes the
interface slow and the cost scale with traffic rather than with fixtures.

So the worker computes every fixture once and stores the result here, and the
bot becomes a reader. One row per fixture per provider, replaced in place as
odds move.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IntPrimaryKeyMixin, JSONType, TimestampMixin


class StoredAnalysis(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A precomputed analysis of one fixture."""

    __tablename__ = "stored_analyses"
    __table_args__ = (
        UniqueConstraint(
            "provider_name",
            "provider_event_id",
            name="uq_stored_analyses_provider_event",
        ),
        Index("ix_stored_analyses_kickoff", "kickoff"),
        Index("ix_stored_analyses_coverage", "coverage"),
    )

    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    """Matches ``historical_matches.provider_match_id`` in width.

    Backfilled identifiers are built from competition, date and both club
    names, and Asian league names run long — "Sanfrecce Hiroshima" against
    "Hokkaido Consadole Sapporo" alone reaches 61 characters. A narrower column
    here than in the source table meant the backfill aborted on a competition
    the ingest had accepted.
    """

    home_name: Mapped[str] = mapped_column(String(128), nullable=False)
    away_name: Mapped[str] = mapped_column(String(128), nullable=False)
    competition: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(64))
    kickoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    coverage: Mapped[str] = mapped_column(String(24), nullable=False)
    """Grade rather than a boolean, so the interface can distinguish a data gap
    from a modelling limit and say which."""

    unavailable_reason: Mapped[str | None] = mapped_column(Text)

    markets: Mapped[dict[str, object]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    """Every supported market's probabilities, keyed by market name.

    Stored as JSON rather than columns because the market set grows — corners,
    cards, player markets — and each addition would otherwise be a migration.
    """

    expected_home_goals: Mapped[float | None] = mapped_column(Float)
    expected_away_goals: Mapped[float | None] = mapped_column(Float)

    market_odds: Mapped[dict[str, object] | None] = mapped_column(JSONType)
    market_probabilities: Mapped[dict[str, object] | None] = mapped_column(JSONType)

    home_stats: Mapped[dict[str, object]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    away_stats: Mapped[dict[str, object]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )

    components_used: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )
    components_dropped: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )

    provenance: Mapped[dict[str, object]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    """Where every part of this analysis came from.

    Which provider supplied the fixture and the odds, which historical source
    backed the models, how much history each side had, and which components
    ran or were dropped. Without it, a user asking "why is this only partially
    modelled" gets an assertion rather than an answer.
    """

    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When the models last ran. Shown to the user so a stale analysis is
    visible as stale rather than presented as current."""
