"""Live market data: fixtures, markets, outcomes and price snapshots."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, IntPrimaryKeyMixin, TimestampMixin, enum_column
from app.database.enums import (
    MatchStatus,
    ModellabilityStatus,
    OutcomeStatus,
    ProviderRole,
    SnapshotType,
    StalenessStatus,
)


class Match(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A pre-match fixture as offered by a market data provider.

    Kept logically separate from ``historical_matches``: this table holds what
    a bookmaker is currently offering, that one holds settled results.

    Team foreign keys are nullable because a fixture arrives before its names
    have been resolved to canonical teams. An unresolved fixture is stored with
    its raw names and marked unmodellable rather than dropped.
    """

    __tablename__ = "matches"

    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)

    sport: Mapped[str] = mapped_column(String(32), default="football", nullable=False)

    home_team_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="SET NULL")
    )
    away_team_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="SET NULL")
    )
    home_team_raw: Mapped[str] = mapped_column(String(160), nullable=False)
    away_team_raw: Mapped[str] = mapped_column(String(160), nullable=False)

    competition_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("competitions.id", ondelete="SET NULL")
    )
    competition_raw: Mapped[str | None] = mapped_column(String(160))
    country: Mapped[str | None] = mapped_column(String(80))

    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[MatchStatus] = mapped_column(
        enum_column(MatchStatus, "match_status"),
        default=MatchStatus.SCHEDULED,
        nullable=False,
    )

    modellability: Mapped[ModellabilityStatus] = mapped_column(
        enum_column(ModellabilityStatus, "modellability_status"),
        default=ModellabilityStatus.NO_HISTORICAL_COVERAGE,
        nullable=False,
    )
    """Why a fixture can or cannot be modelled.

    Reported to the user explicitly. A coverage gap must never look the same
    as a filter returning nothing.
    """

    modellability_note: Mapped[str | None] = mapped_column(Text)

    markets: Mapped[list[Market]] = relationship(
        back_populates="match", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "provider_name", "provider_event_id", name="uq_matches_provider_name_event_id"
        ),
        Index("ix_matches_start_time", "start_time"),
        Index("ix_matches_status_start_time", "status", "start_time"),
        Index("ix_matches_modellability", "modellability"),
    )


class Market(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A betting market attached to a fixture, e.g. 1X2 or Over/Under 2.5."""

    __tablename__ = "markets"

    match_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("matches.id", ondelete="CASCADE"), nullable=False
    )

    provider_market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_name: Mapped[str] = mapped_column(String(128), nullable=False)
    specifier: Mapped[str | None] = mapped_column(String(64))
    """Market parameter such as ``total=2.5``. Null for parameterless markets."""

    status: Mapped[OutcomeStatus] = mapped_column(
        enum_column(OutcomeStatus, "outcome_status"),
        default=OutcomeStatus.ACTIVE,
        nullable=False,
    )

    match: Mapped[Match] = relationship(back_populates="markets")
    outcomes: Mapped[list[Outcome]] = relationship(
        back_populates="market", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "match_id",
            "provider_market_id",
            "specifier",
            name="uq_markets_match_id_provider_market_id_specifier",
        ),
    )


class Outcome(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A single selectable outcome and its current price.

    Odds are ``Numeric``, never float: binary floating point cannot represent
    decimal prices exactly, and the error compounds through probability and
    expected-value arithmetic.
    """

    __tablename__ = "outcomes"

    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )

    provider_outcome_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome_name: Mapped[str] = mapped_column(String(128), nullable=False)

    odds: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)

    status: Mapped[OutcomeStatus] = mapped_column(
        enum_column(OutcomeStatus, "outcome_status"),
        default=OutcomeStatus.ACTIVE,
        nullable=False,
    )

    last_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staleness: Mapped[StalenessStatus] = mapped_column(
        enum_column(StalenessStatus, "staleness_status"),
        default=StalenessStatus.FRESH,
        nullable=False,
    )

    market: Mapped[Market] = relationship(back_populates="outcomes")
    snapshots: Mapped[list[OddsSnapshot]] = relationship(
        back_populates="outcome", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "market_id", "provider_outcome_id", name="uq_outcomes_market_id_provider_outcome_id"
        ),
        CheckConstraint("odds > 1", name="odds_exceed_stake"),
        Index("ix_outcomes_staleness", "staleness"),
    )


class OddsSnapshot(IntPrimaryKeyMixin, Base):
    """An immutable price observation.

    ``provider_role`` is the column that keeps the quant architecture honest.
    A REFERENCE price from a sharp book forms the prior; a TRADEABLE price is
    the one bet into. Both must be captured, from different sources, or
    expected value is measured against the same book that supplied the prior
    and collapses to zero by construction.
    """

    __tablename__ = "odds_snapshots"

    outcome_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("outcomes.id", ondelete="CASCADE"), nullable=False
    )

    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_role: Mapped[ProviderRole] = mapped_column(
        enum_column(ProviderRole, "provider_role"), nullable=False
    )

    odds: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    snapshot_type: Mapped[SnapshotType] = mapped_column(
        enum_column(SnapshotType, "snapshot_type"),
        default=SnapshotType.INTERIM,
        nullable=False,
    )

    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Provider's own timestamp, where supplied. Clocks drift; keep both."""

    outcome: Mapped[Outcome] = relationship(back_populates="snapshots")

    __table_args__ = (
        CheckConstraint("odds > 1", name="odds_exceed_stake"),
        Index("ix_odds_snapshots_outcome_id_captured_at", "outcome_id", "captured_at"),
        Index("ix_odds_snapshots_provider_role_snapshot_type", "provider_role", "snapshot_type"),
    )
