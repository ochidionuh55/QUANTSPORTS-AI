"""Historical results and the model version registry."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IntPrimaryKeyMixin, JSONType, TimestampMixin, enum_column
from app.database.enums import EnsembleMethod, MarginMethod, ModelStatus


class HistoricalMatch(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A settled result used to fit and validate models.

    Deliberately separate from ``matches``. That table is what a bookmaker is
    offering now; this one is the evidence base, and it must remain usable even
    when no bookmaker is reachable.
    """

    __tablename__ = "historical_matches"

    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_match_id: Mapped[str] = mapped_column(String(128), nullable=False)

    home_team_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    away_team_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    competition_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("competitions.id", ondelete="SET NULL")
    )

    season: Mapped[str | None] = mapped_column(String(16))
    match_date: Mapped[date] = mapped_column(Date, nullable=False)

    home_goals: Mapped[int] = mapped_column(Integer, nullable=False)
    away_goals: Mapped[int] = mapped_column(Integer, nullable=False)
    half_time_home_goals: Mapped[int | None] = mapped_column(Integer)
    half_time_away_goals: Mapped[int | None] = mapped_column(Integer)

    data_version: Mapped[str] = mapped_column(String(64), nullable=False)
    """Version of the ingested dataset.

    Free CSV datasets are revised in place and carry no vintage history, so
    perfect point-in-time reconstruction is not achievable in the MVP. Recording
    the version at least makes a revision detectable rather than invisible.
    """

    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "provider_name",
            "provider_match_id",
            name="uq_historical_matches_provider_name_provider_match_id",
        ),
        CheckConstraint("home_goals >= 0 AND away_goals >= 0", name="goals_non_negative"),
        CheckConstraint("home_team_id <> away_team_id", name="teams_differ"),
        Index("ix_historical_matches_match_date", "match_date"),
        Index("ix_historical_matches_home_team_id_match_date", "home_team_id", "match_date"),
        Index("ix_historical_matches_away_team_id_match_date", "away_team_id", "match_date"),
    )


class ModelVersion(IntPrimaryKeyMixin, TimestampMixin, Base):
    """A versioned, reproducible model configuration.

    Every prediction references one of these. Without it a prediction cannot be
    reproduced, and a backtest cannot attribute performance to anything.

    Chronological period boundaries are stored explicitly so that leakage is
    auditable after the fact: if the test period does not strictly follow the
    training period, the validation is void.
    """

    __tablename__ = "model_versions"

    name: Mapped[str] = mapped_column(String(96), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[ModelStatus] = mapped_column(
        enum_column(ModelStatus, "model_status"),
        default=ModelStatus.DRAFT,
        nullable=False,
    )

    margin_method: Mapped[MarginMethod] = mapped_column(
        enum_column(MarginMethod, "margin_method"), nullable=False
    )
    ensemble_method: Mapped[EnsembleMethod] = mapped_column(
        enum_column(EnsembleMethod, "ensemble_method"), nullable=False
    )

    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )
    feature_set: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )
    validation_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )
    """Brier score, log loss, ECE and the market baseline they are measured against."""

    training_start: Mapped[date | None] = mapped_column(Date)
    training_end: Mapped[date | None] = mapped_column(Date)
    validation_start: Mapped[date | None] = mapped_column(Date)
    validation_end: Mapped[date | None] = mapped_column(Date)
    test_start: Mapped[date | None] = mapped_column(Date)
    test_end: Mapped[date | None] = mapped_column(Date)

    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promotion_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_model_versions_name_version"),
        CheckConstraint(
            "training_end IS NULL OR test_start IS NULL OR training_end < test_start",
            name="training_precedes_test",
        ),
        CheckConstraint(
            "validation_end IS NULL OR test_start IS NULL OR validation_end < test_start",
            name="validation_precedes_test",
        ),
        Index("ix_model_versions_status", "status"),
    )
