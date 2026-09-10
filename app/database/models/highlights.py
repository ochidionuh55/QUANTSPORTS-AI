"""Daily highlight selections and their outcomes.

A permanent, tamper-evident record of every highlight QUANTSPORT has published.

**Recorded before kickoff, settled afterwards, never edited.** The selection,
its probability, the model version and the reasoning are written when the
highlight is published; only the result is filled in later. A track record that
can be adjusted once the outcome is known is worse than no track record, since
it produces confidence rather than knowledge.

**Reconstructed history is kept separate.** Highlights derived after the fact
from historical predictions are honest — the same scoring, applied to forecasts
that were themselves leakage-free — but they were never actually published, and
a run of good luck in reconstruction is not evidence about the live product.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IntPrimaryKeyMixin, JSONType, TimestampMixin


class HighlightSelection(IntPrimaryKeyMixin, TimestampMixin, Base):
    """One published highlight."""

    __tablename__ = "highlight_selections"
    __table_args__ = (
        UniqueConstraint(
            "provider_event_id",
            "market",
            "outcome",
            "source",
            "track",
            name="uq_highlight_fixture_market",
        ),
        Index("ix_highlight_selections_day", "selection_date"),
        Index("ix_highlight_selections_source", "source"),
        Index("ix_highlight_selections_status", "status"),
        Index("ix_highlight_selections_track", "track"),
    )

    track: Mapped[str] = mapped_column(
        String(24), nullable=False, default="sharp", server_default=text("'sharp'")
    )
    """Which board this selection belongs to.

    Three boards, three different questions — most likely, most informative,
    most historically supported. A selection is stored with the board that
    chose it, so each can be scored separately and a strong board cannot hide
    behind a weak one.
    """

    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="live", server_default=text("'live'")
    )
    """``live`` or ``reconstructed``. Never averaged together."""

    selection_date: Mapped[date] = mapped_column(Date, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    home_name: Mapped[str] = mapped_column(String(128), nullable=False)
    away_name: Mapped[str] = mapped_column(String(128), nullable=False)
    competition: Mapped[str | None] = mapped_column(String(128))
    kickoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    market: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    probability: Mapped[float] = mapped_column(Float, nullable=False)
    base_rate: Mapped[float | None] = mapped_column(Float)
    """How often this outcome happens generally, so a reader can judge whether
    the selection was unusual or ordinary."""

    score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    coverage: Mapped[str] = mapped_column(String(24), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    components_used: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )

    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    factors: Mapped[dict[str, object]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    """The component scores that caused this to qualify, so "why this?" has a
    real answer rather than a restatement of the probability."""

    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When the selection was written. Must precede kickoff for a live
    highlight; the constraint is what makes the record trustworthy."""

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    """``pending``, ``won``, ``lost`` or ``void``."""

    home_goals: Mapped[int | None] = mapped_column(Integer)
    away_goals: Mapped[int | None] = mapped_column(Integer)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_settled(self) -> bool:
        """Whether the outcome is known."""
        return self.status in {"won", "lost", "void"}

    @property
    def was_recorded_before_kickoff(self) -> bool:
        """Whether this selection predates the match it describes."""
        recorded = self.recorded_at
        kickoff = self.kickoff
        if recorded.tzinfo is None or kickoff.tzinfo is None:
            return recorded.replace(tzinfo=None) < kickoff.replace(tzinfo=None)
        return recorded < kickoff


class HighlightFollow(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A user choosing to follow a highlight.

    Separate from the global record: what one person tracked is not what
    QUANTSPORT published, and merging them would let a user's selective
    following flatter the platform's numbers.
    """

    __tablename__ = "highlight_follows"
    __table_args__ = (
        UniqueConstraint("user_id", "highlight_id", name="uq_highlight_follow_user"),
        Index("ix_highlight_follows_user", "user_id"),
    )

    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    highlight_id: Mapped[int] = mapped_column(Integer, nullable=False)
    followed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
