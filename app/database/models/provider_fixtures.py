"""Every fixture the provider returned, whether or not it can train a model.

**Why this is a separate table.** ``historical_matches`` is the evidence base:
its own docstring calls it "a settled result used to fit and validate models",
its goals are ``NOT NULL`` and a check constraint requires them non-negative.
It cannot hold an abandoned match, and it should not — every model, backtest
and validation path reads it and assumes a score is present.

But a history that silently omits abandoned fixtures implies matches that never
happened. Across Wave 1 that is roughly 900 fixtures: NPFL 274, Liga Alef 430,
Primera B 123. Dropping them would leave no record that the provider ever
carried them, and no way to tell a data gap from a quiet ingestion failure.

So this table records what the provider actually had. Trainable rows also go
to ``historical_matches``; the rest stay here alone, with the status that
explains why. Nothing disappears between provider and database, and the
existing table keeps the contract 113,029 rows already depend on.

**Eligibility is stored, not judged here.** ``training_eligible`` is set from
:func:`app.core.fixture_status.is_training_eligible`, the one policy every
caller uses. This table records that decision; it does not make it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IntPrimaryKeyMixin, TimestampMixin


class ProviderFixture(IntPrimaryKeyMixin, TimestampMixin, Base):
    """One fixture as the provider reported it.

    Keyed on provider and provider fixture id, so re-running ingestion updates
    a row rather than adding one. A fixture whose status legitimately changes —
    postponed then played — is the same fixture, and must not become two.
    """

    __tablename__ = "provider_fixtures"
    __table_args__ = (
        UniqueConstraint(
            "provider_name",
            "provider_fixture_id",
            name="uq_provider_fixtures_provider_fixture",
        ),
        Index("ix_provider_fixtures_league", "provider_league_id"),
        Index("ix_provider_fixtures_season", "season"),
        Index("ix_provider_fixtures_kickoff", "kickoff"),
        Index("ix_provider_fixtures_eligible", "training_eligible"),
        Index("ix_provider_fixtures_status", "status_code"),
        # Covering indexes for the team foreign keys. Without them a delete on
        # teams scans every row here.
        Index("ix_provider_fixtures_home_team", "home_team_id"),
        Index("ix_provider_fixtures_away_team", "away_team_id"),
    )

    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_fixture_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_league_id: Mapped[int] = mapped_column(Integer, nullable=False)
    """The provider's league id, kept as given.

    Not our competition code. Codes are ours and can change; this is the key
    the provider will answer to next time.
    """

    competition_code: Mapped[str | None] = mapped_column(String(16))
    season: Mapped[str] = mapped_column(String(16), nullable=False)
    kickoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    provider_home_team_id: Mapped[str | None] = mapped_column(String(64))
    provider_away_team_id: Mapped[str | None] = mapped_column(String(64))
    home_team_name: Mapped[str] = mapped_column(String(160), nullable=False)
    away_team_name: Mapped[str] = mapped_column(String(160), nullable=False)

    home_team_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="SET NULL")
    )
    away_team_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="SET NULL")
    )
    """Canonical ids where identity resolved.

    Nullable on purpose: a fixture whose teams could not be resolved is still
    a fixture the provider had, and recording it is the point. It simply
    cannot train anything.
    """

    status_code: Mapped[str | None] = mapped_column(String(16))
    """The provider's raw status, as given. Never normalised away."""

    status_class: Mapped[str] = mapped_column(String(24), nullable=False)
    """Our classification of it: FINISHED, ABANDONED, NOT_PLAYED, and so on."""

    home_goals: Mapped[int | None] = mapped_column(Integer)
    away_goals: Mapped[int | None] = mapped_column(Integer)
    """Nullable, unlike ``historical_matches``. An abandoned match has no score
    and the absence is the information."""

    training_eligible: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    exclusion_reason: Mapped[str | None] = mapped_column(String(160))
    """Why this fixture cannot train, when it cannot.

    Always set when ``training_eligible`` is false, so a reconciliation can
    account for every stored row. A fixture missing from training without a
    stated reason is indistinguishable from one lost by accident.
    """

    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_payload_hash: Mapped[str | None] = mapped_column(String(64))
    """Hash of the fields we stored, so a provider revision is detectable."""

    @property
    def scoreline(self) -> str | None:
        """Return the final score, or ``None`` if there is not one."""
        if self.home_goals is None or self.away_goals is None:
            return None
        return f"{self.home_goals}-{self.away_goals}"

    @property
    def identity_resolved(self) -> bool:
        """Whether both teams matched canonical records."""
        return self.home_team_id is not None and self.away_team_id is not None


__all__ = ["ProviderFixture"]
