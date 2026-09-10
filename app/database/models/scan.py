"""Scans, predictions and booking codes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import (
    Base,
    CreatedAtMixin,
    IntPrimaryKeyMixin,
    JSONType,
    TimestampMixin,
    enum_column,
)
from app.database.enums import (
    BookingCodeStatus,
    MarginMethod,
    ScanMode,
    ScanStatus,
    ScanType,
)

if TYPE_CHECKING:
    from app.database.models.user import User


class Scan(IntPrimaryKeyMixin, TimestampMixin, Base):
    """One execution of the scanning pipeline."""

    __tablename__ = "scans"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    model_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("model_versions.id", ondelete="RESTRICT")
    )

    scan_type: Mapped[ScanType] = mapped_column(enum_column(ScanType, "scan_type"), nullable=False)
    mode: Mapped[ScanMode] = mapped_column(
        enum_column(ScanMode, "scan_mode"), default=ScanMode.RESEARCH, nullable=False
    )
    status: Mapped[ScanStatus] = mapped_column(
        enum_column(ScanStatus, "scan_status"), default=ScanStatus.PENDING, nullable=False
    )

    credits_reserved: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    credits_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    matches_scanned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    matches_unmodellable: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """Counted and reported, never silently discarded."""

    outcomes_scanned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    idempotency_key: Mapped[str | None] = mapped_column(String(128))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="scans")
    predictions: Mapped[list[Prediction]] = relationship(
        back_populates="scan", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_scans_user_id_idempotency_key"),
        CheckConstraint("credits_reserved >= 0 AND credits_used >= 0", name="credits_non_negative"),
        CheckConstraint("credits_used <= credits_reserved", name="usage_within_reservation"),
        Index("ix_scans_user_id_created_at", "user_id", "created_at"),
        Index("ix_scans_status", "status"),
    )


class Prediction(IntPrimaryKeyMixin, CreatedAtMixin, Base):
    """A single probability estimate for one outcome.

    Every field needed to reconstruct the estimate is stored: the model
    version, the exact price observation used, the margin method, the market
    prior, the model's own estimate and the shrunk posterior. Without all of
    them a prediction is an opinion rather than a measurement.

    ``confidence_score`` is nullable by design. No score is emitted until
    calibration data exists to derive one; a hand-assigned number would be
    marketing, not measurement.
    """

    __tablename__ = "predictions"

    scan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("scans.id", ondelete="CASCADE"), nullable=False
    )
    match_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("matches.id", ondelete="CASCADE"), nullable=False
    )
    outcome_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("outcomes.id", ondelete="SET NULL")
    )
    model_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False
    )

    tradeable_snapshot_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("odds_snapshots.id", ondelete="SET NULL")
    )
    reference_snapshot_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("odds_snapshots.id", ondelete="SET NULL")
    )
    """The sharp price the prior was derived from, distinct from the traded price."""

    market_name: Mapped[str] = mapped_column(String(128), nullable=False)
    selection: Mapped[str] = mapped_column(String(128), nullable=False)
    specifier: Mapped[str | None] = mapped_column(String(64))

    odds: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    margin_method: Mapped[MarginMethod] = mapped_column(
        enum_column(MarginMethod, "margin_method"), nullable=False
    )

    raw_market_probability: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    """1/odds, before margin removal."""

    market_prior_probability: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    """Margin-free probability from the reference source. The prior."""

    model_probability: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    """The model's independent estimate, before shrinkage."""

    final_probability: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    """Posterior after shrinking the model toward the prior."""

    edge: Mapped[Decimal] = mapped_column(Numeric(9, 8), nullable=False)
    # EV = (probability * odds) - 1. NUMERIC(10, 8) caps at 99.99999999, which
    # a long-odds outcome combined with a miscalibrated model can exceed. An
    # overflow would abort the whole scan transaction; a wide column lets the
    # implausible value be stored and then rejected by the EV filter, which is
    # both safer and far easier to diagnose.
    expected_value: Mapped[Decimal] = mapped_column(Numeric(14, 8), nullable=False)

    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    rank: Mapped[int | None] = mapped_column(Integer)

    feature_values: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )

    scan: Mapped[Scan] = relationship(back_populates="predictions")

    __table_args__ = (
        CheckConstraint("odds > 1", name="odds_exceed_stake"),
        CheckConstraint(
            "final_probability > 0 AND final_probability < 1", name="final_probability_in_range"
        ),
        CheckConstraint(
            "market_prior_probability > 0 AND market_prior_probability < 1",
            name="prior_in_range",
        ),
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 100)",
            name="confidence_score_in_range",
        ),
        Index("ix_predictions_scan_id_rank", "scan_id", "rank"),
        Index("ix_predictions_match_id", "match_id"),
        Index("ix_predictions_model_version_id", "model_version_id"),
    )


class BookingCode(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A bookmaker booking code generated from a set of selections."""

    __tablename__ = "booking_codes"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    scan_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("scans.id", ondelete="SET NULL")
    )

    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    booking_code: Mapped[str | None] = mapped_column(String(64))
    share_url: Mapped[str | None] = mapped_column(Text)

    status: Mapped[BookingCodeStatus] = mapped_column(
        enum_column(BookingCodeStatus, "booking_code_status"),
        default=BookingCodeStatus.PENDING,
        nullable=False,
    )

    selections: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )
    """Exact selections submitted, so a failure can be diagnosed after the fact."""

    unavailable_selections: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )

    expiry_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_booking_codes_user_id_idempotency_key"
        ),
        Index("ix_booking_codes_user_id_created_at", "user_id", "created_at"),
        Index("ix_booking_codes_status", "status"),
    )
