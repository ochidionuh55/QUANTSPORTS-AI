"""Which competition and service pairs have earned permission to publish.

**Why permission is per pair.** Wave 1 validation found calibration varies by
market within a competition: Liga Alef holds the largest dataset and earned
two of twenty-one services, while Bosnia holds the smallest and earned eleven.
A competition-level switch would either publish a market the record
contradicts or discard one it supports.

**The full matrix is kept, not only the winners.** 168 capabilities were
tested; 43 passed, 79 were withheld and 46 had too little evidence to judge.
The failures are the reason the passes mean anything, and a later model change
is compared against them rather than replacing them silently.

**Insufficient evidence is not failure.** A service that qualifies too rarely
to produce a usable confidence interval is unproven, not disproven. Collapsing
the two would lose the distinction between "we tested this and it was wrong"
and "we have not been able to test it yet".

**Version-bound.** A capability is validated against one model version. When
the model changes the evidence no longer describes what production computes,
so the capability stops matching and publication blocks until it is revalidated.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, IntPrimaryKeyMixin, TimestampMixin


class CapabilityState(str, Enum):
    """Whether a competition and service pair may publish."""

    ACTIVE = "ACTIVE"
    """Validated: the observed hit rate's confidence interval contains the
    predicted probability, on a sufficient and stable sample."""

    WITHHELD = "WITHHELD"
    """Tested and rejected. The interval excludes the prediction, the service
    is unstable across time, or the competition itself was withheld."""

    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    """Not judgeable. Too few qualifying selections, or an interval too wide
    to separate a good service from a bad one."""


PUBLISHABLE_STATES: frozenset[str] = frozenset({CapabilityState.ACTIVE.value})
"""The only state that permits publication.

A single-member set on purpose: adding to it is a deliberate act, not a
consequence of some other change.
"""


class ServiceCapability(IntPrimaryKeyMixin, TimestampMixin, Base):
    """One competition x service x model version, with its evidence."""

    __tablename__ = "service_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "competition_code",
            "service_key",
            "model_version",
            name="uq_service_capabilities_competition_service_version",
        ),
        Index("ix_service_capabilities_competition", "competition_code"),
        Index("ix_service_capabilities_state", "state"),
        Index("ix_service_capabilities_version", "model_version"),
    )

    competition_code: Mapped[str] = mapped_column(String(16), nullable=False)
    """Our competition code, not the provider's league id.

    Publication resolves a fixture to a code, so the capability must be keyed
    the same way or the lookup silently misses.
    """

    provider_league_id: Mapped[int | None] = mapped_column(Integer)
    competition_name: Mapped[str] = mapped_column(String(160), nullable=False)

    service_key: Mapped[str] = mapped_column(String(64), nullable=False)
    service_label: Mapped[str] = mapped_column(String(160), nullable=False)

    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_version: Mapped[str] = mapped_column(String(64), nullable=False)

    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(48), nullable=False)

    # The evidence behind the decision, kept so a verdict can be re-examined
    # without re-running the validation.
    threshold: Mapped[float | None] = mapped_column(Float)
    qualifying_sample: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    total_sample: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    predicted_rate: Mapped[float | None] = mapped_column(Float)
    observed_rate: Mapped[float | None] = mapped_column(Float)
    calibration_gap: Mapped[float | None] = mapped_column(Float)
    interval_low: Mapped[float | None] = mapped_column(Float)
    interval_high: Mapped[float | None] = mapped_column(Float)
    brier: Mapped[float | None] = mapped_column(Float)
    window_spread: Mapped[float | None] = mapped_column(Float)

    validated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    @property
    def may_publish(self) -> bool:
        """Whether this pair permits publication."""
        return self.state in PUBLISHABLE_STATES


__all__ = ["PUBLISHABLE_STATES", "CapabilityState", "ServiceCapability"]
