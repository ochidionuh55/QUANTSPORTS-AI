"""User feedback and prediction settlement.

``SettledPrediction`` is the point of this module. Every analysis we publish is
compared against what actually happened and stored permanently, so the system
judges itself continuously rather than on whichever days someone happened to
remember.

The row is a snapshot, not a join. It copies the probabilities as they were
published, because the stored analysis is overwritten as odds move and the
fixture is pruned once it finishes. Without the copy, a week-old result could
not be tied to the forecast that produced it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
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


class UserFeedback(IntPrimaryKeyMixin, CreatedAtMixin, Base):
    """One thumbs-up or thumbs-down on an analysis.

    Append-only: a user changing their mind writes a second row rather than
    editing the first, so the history of what people thought over time survives.
    """

    __tablename__ = "user_feedback"
    __table_args__ = (
        Index("ix_user_feedback_fixture", "provider_event_id"),
        Index("ix_user_feedback_user", "user_id"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String(64))
    feature: Mapped[str] = mapped_column(String(32), nullable=False, default="analysis")
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)


class SettledPrediction(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A published analysis compared against the actual result."""

    __tablename__ = "settled_predictions"
    __table_args__ = (
        UniqueConstraint(
            "provider_name",
            "provider_event_id",
            name="uq_settled_predictions_provider_event",
        ),
        Index("ix_settled_predictions_kickoff", "kickoff"),
        Index("ix_settled_predictions_competition", "competition"),
        Index("ix_settled_predictions_coverage", "coverage"),
        Index("ix_settled_predictions_source", "source"),
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
    kickoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    coverage: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="live", server_default="live"
    )
    """``live`` or ``backfill``.

    Kept separate at query time, never blended. Backfilled predictions are
    honest — same engine, same leakage discipline — but they were produced with
    hindsight about which fixtures exist and which leagues had coverage.
    Averaging them with live results would flatter or distort both.
    """

    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    """Which model produced the forecast. Without it, a performance history
    spanning a model change is uninterpretable."""

    components_used: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )

    # --- what we said ------------------------------------------------------
    predicted_home: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_draw: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_away: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_over_2_5: Mapped[float | None] = mapped_column(Float)
    predicted_btts: Mapped[float | None] = mapped_column(Float)
    expected_home_goals: Mapped[float | None] = mapped_column(Float)
    expected_away_goals: Mapped[float | None] = mapped_column(Float)

    market_home: Mapped[float | None] = mapped_column(Float)
    market_draw: Mapped[float | None] = mapped_column(Float)
    market_away: Mapped[float | None] = mapped_column(Float)
    """The market's view at publication, so model and market are scored on
    identical fixtures. A score without this baseline is uninterpretable."""

    # --- what happened -----------------------------------------------------
    home_goals: Mapped[int] = mapped_column(Integer, nullable=False)
    away_goals: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_result: Mapped[str] = mapped_column(String(8), nullable=False)
    predicted_favourite: Mapped[str] = mapped_column(String(8), nullable=False)
    favourite_won: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    over_2_5_hit: Mapped[bool | None] = mapped_column(Boolean)
    btts_hit: Mapped[bool | None] = mapped_column(Boolean)

    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
