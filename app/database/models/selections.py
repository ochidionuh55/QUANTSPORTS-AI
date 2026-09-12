"""Published Best of the Day selections.

**Published means fixed.** Once a selection is written it is a public claim
about a match that has not happened. Everything describing that claim — the
fixture, the market, the probability, the reasoning, the model version, the time
it was published — is locked at that moment. Only the result may be filled in
afterwards. A record that can be adjusted once the outcome is known produces
confidence rather than knowledge, which is worse than having no record at all.

**History is read, never recomputed.** When a user opens 10 September they must
see what was actually published that morning, not what today's model would say
about those fixtures now. The difference is the whole point: anyone can look
clever about last week's football.

**Corrections are recorded, not applied silently.** If a published selection
ever needs amending, the amendment is appended as an audit entry with a reason.
The original stays visible.
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

PENDING = "pending"
WON = "won"
LOST = "lost"
VOID = "void"
UNSETTLED = "unsettled"

SETTLED_STATUSES = frozenset({WON, LOST, VOID})


class ServiceSelection(IntPrimaryKeyMixin, TimestampMixin, Base):
    """One service's published selection for one day."""

    __tablename__ = "service_selections"
    __table_args__ = (
        # One published selection per service per day. The constraint is what
        # makes a re-run of the worker safe: a second attempt cannot quietly
        # replace a claim already made.
        UniqueConstraint("service_key", "selection_date", "rank", name="uq_service_selection_day"),
        Index("ix_service_selections_date", "selection_date"),
        Index("ix_service_selections_service", "service_key"),
        Index("ix_service_selections_status", "status"),
        Index("ix_service_selections_fixture", "provider_event_id"),
        Index("ix_service_selections_market", "market"),
        Index("ix_service_selections_published", "published_at"),
    )

    service_key: Mapped[str] = mapped_column(String(32), nullable=False)
    service_label: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_date: Mapped[date] = mapped_column(Date, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))

    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    home_name: Mapped[str] = mapped_column(String(128), nullable=False)
    away_name: Mapped[str] = mapped_column(String(128), nullable=False)
    competition: Mapped[str | None] = mapped_column(String(128))
    country: Mapped[str | None] = mapped_column(String(64))
    kickoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    market: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    probability: Mapped[float] = mapped_column(Float, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    coverage: Mapped[str] = mapped_column(String(24), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    components_used: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )
    factors: Mapped[dict[str, float]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    """The ranking terms, so "why this pick" quotes stored numbers."""

    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sample_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """When the claim was made. Must precede kickoff."""

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PENDING, server_default=text("'pending'")
    )
    home_goals: Mapped[int | None] = mapped_column(Integer)
    away_goals: Mapped[int | None] = mapped_column(Integer)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    corrected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Whether an audited correction exists for this record."""

    @property
    def is_settled(self) -> bool:
        """Whether the outcome is known."""
        return self.status in SETTLED_STATUSES

    @property
    def published_before_kickoff(self) -> bool:
        """Whether the claim genuinely predates the match.

        The property the entire track record rests on.
        """
        published = self.published_at
        kickoff = self.kickoff
        if published.tzinfo is None or kickoff.tzinfo is None:
            return published.replace(tzinfo=None) < kickoff.replace(tzinfo=None)
        return published < kickoff

    def locked_fields(self) -> dict[str, object]:
        """Return the fields that must never change after publication."""
        return {
            "provider_event_id": self.provider_event_id,
            "market": self.market,
            "outcome": self.outcome,
            "probability": self.probability,
            "score": self.score,
            "rationale": self.rationale,
            "model_version": self.model_version,
            "coverage": self.coverage,
            "published_at": self.published_at,
        }


class SelectionAudit(IntPrimaryKeyMixin, TimestampMixin, Base):
    """An append-only note against a published selection.

    Corrections are recorded here rather than applied to the selection itself,
    so the original claim stays visible and the change has a stated reason.
    """

    __tablename__ = "selection_audits"
    __table_args__ = (
        Index("ix_selection_audits_selection", "selection_id"),
        Index("ix_selection_audits_event", "event"),
    )

    selection_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    """``published``, ``settled``, ``corrected`` or ``void``."""

    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    previous: Mapped[dict[str, object]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DailySnapshot(IntPrimaryKeyMixin, TimestampMixin, Base):
    """What the product saw on one day.

    Records the shape of the card as well as the selections, so a historical
    page can honestly say "no service qualified out of 38 fixtures" rather than
    leaving a user wondering whether the system simply failed to run.
    """

    __tablename__ = "daily_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", name="uq_daily_snapshot_date"),
        Index("ix_daily_snapshots_date", "snapshot_date"),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    fixtures_available: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    fixtures_modelled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    services_run: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    services_qualified: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    coverage: Mapped[dict[str, int]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
