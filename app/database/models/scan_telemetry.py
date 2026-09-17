"""Durable record of what each scan examined, and why it rejected the rest.

**Why this exists.** The pipeline could say what it published and not what it
considered. ``StoredAnalysis`` is pruned once a fixture finishes, so the
evidence for "we looked at 186 fixtures and 51 were in competitions we do not
support" was deleted within days of being created. ``ScanReport`` counted
exactly the right things and was logged and discarded. A diagnostic built on
those sources reported a funnel where later stages exceeded earlier ones,
which is not a measurement error but a sign that the population was gone.

These tables keep that evidence. A scan writes one :class:`ScanRun` and one
:class:`ScanDecision` per fixture it saw — including every fixture it threw
away, because those decisions are the substance of the question.

**Not tied to fixture lifecycle.** Pruning follows the fixture in
``StoredAnalysis`` because a finished match needs no live analysis. The
opposite is true here: the value of a rejection record is entirely historical.
Retention is by age of the scan, never by whether its fixtures have finished.

**Observability only.** Nothing here influences what is analysed, published or
settled. A failure writing telemetry must never cost a selection, so callers
record decisions on a best-effort basis.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from itertools import pairwise
from typing import Final

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, IntPrimaryKeyMixin, TimestampMixin


class RejectionCode(str, Enum):
    """Why a fixture did not reach a published selection.

    Machine-readable, so analytics never depends on parsing English. The
    human-readable detail the analyser wrote is stored separately in
    ``rejection_detail`` and is free to change wording without breaking a
    report.
    """

    UNSUPPORTED_COMPETITION = "UNSUPPORTED_COMPETITION"
    HOME_TEAM_UNRESOLVED = "HOME_TEAM_UNRESOLVED"
    AWAY_TEAM_UNRESOLVED = "AWAY_TEAM_UNRESOLVED"
    BOTH_TEAMS_UNRESOLVED = "BOTH_TEAMS_UNRESOLVED"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    MODEL_FAILURE = "MODEL_FAILURE"
    INCOMPLETE_MODEL = "INCOMPLETE_MODEL"
    NO_SERVICE_QUALIFIED = "NO_SERVICE_QUALIFIED"
    PROVIDER_DATA_INVALID = "PROVIDER_DATA_INVALID"
    DUPLICATE_FIXTURE = "DUPLICATE_FIXTURE"
    """The feed repeated a fixture. Recorded rather than silently skipped so
    the difference between a thin card and a noisy feed stays visible."""

    ANALYSIS_ERROR = "ANALYSIS_ERROR"
    """The analyser raised. Distinct from MODEL_FAILURE, which is the model
    declining to produce an estimate rather than the code breaking."""


class ScanStage(str, Enum):
    """The furthest stage a fixture reached.

    Ordered. ``PUBLISHED`` is the only terminal stage that is not a rejection,
    and every other value names where the fixture stopped.
    """

    SEEN = "SEEN"
    SUPPORTED = "SUPPORTED"
    IDENTITY_RESOLVED = "IDENTITY_RESOLVED"
    HISTORY_SUFFICIENT = "HISTORY_SUFFICIENT"
    MODEL_PRODUCED = "MODEL_PRODUCED"
    FULLY_MODELLED = "FULLY_MODELLED"
    PUBLISHED = "PUBLISHED"


STAGE_ORDER: Final[tuple[ScanStage, ...]] = (
    ScanStage.SEEN,
    ScanStage.SUPPORTED,
    ScanStage.IDENTITY_RESOLVED,
    ScanStage.HISTORY_SUFFICIENT,
    ScanStage.MODEL_PRODUCED,
    ScanStage.FULLY_MODELLED,
    ScanStage.PUBLISHED,
)
"""Stage sequence, used to check that a funnel never widens."""


class ScanRunStatus(str, Enum):
    """How a scan ended."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class ScanRun(IntPrimaryKeyMixin, TimestampMixin, Base):
    """One execution of the scheduled scan.

    Counts are of **unique fixtures**, never of selections. One fixture can
    produce a selection in several services, and counting rows would make the
    funnel widen at the last stage — the exact failure that made the first
    diagnostic unusable. ``selections_published`` is kept separately for that
    reason and is deliberately not part of the funnel.
    """

    __tablename__ = "scan_runs"
    __table_args__ = (
        Index("ix_scan_runs_started", "started_at"),
        Index("ix_scan_runs_status", "status"),
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(64))
    grid_version: Mapped[str | None] = mapped_column(String(64))
    hours_ahead: Mapped[int | None] = mapped_column(Integer)

    # The funnel. Each is a count of unique fixtures and must not exceed the
    # one above it.
    fixtures_seen: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    supported: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    identity_resolved: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    sufficient_history: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    model_produced: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    fully_modelled: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    fixtures_with_qualifying_selection: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    # Deliberately outside the funnel: one fixture, many services.
    selections_published: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    duplicates_skipped: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    odds_fetched: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    error_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(32), default=ScanRunStatus.RUNNING.value, server_default="RUNNING", nullable=False
    )

    decisions: Mapped[list[ScanDecision]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    def funnel(self) -> tuple[tuple[str, int], ...]:
        """Return the funnel stages in order, for reporting and checking."""
        return (
            ("fixtures_seen", self.fixtures_seen),
            ("supported", self.supported),
            ("identity_resolved", self.identity_resolved),
            ("sufficient_history", self.sufficient_history),
            ("model_produced", self.model_produced),
            ("fully_modelled", self.fully_modelled),
            ("fixtures_with_qualifying_selection", self.fixtures_with_qualifying_selection),
        )

    def funnel_is_monotonic(self) -> bool:
        """Whether each stage is no larger than the one before it.

        A funnel that widens means the stages were counted over different
        populations. Checked rather than assumed, because that is precisely
        what went wrong before this table existed.
        """
        counts = [count for _, count in self.funnel()]
        return all(later <= earlier for earlier, later in pairwise(counts))


class ScanDecision(IntPrimaryKeyMixin, TimestampMixin, Base):
    """What the scan decided about one fixture.

    Written for every fixture seen, including ones rejected immediately. A
    fixture discarded because its competition is unsupported is the evidence
    this table exists to keep; recording only survivors would reproduce the
    blindness it was built to remove.
    """

    __tablename__ = "scan_decisions"
    __table_args__ = (
        # One decision per fixture per run. Protects the counts from a retried
        # scan or a feed that repeats a fixture.
        UniqueConstraint("scan_run_id", "provider_event_id", name="uq_scan_decision_fixture"),
        Index("ix_scan_decisions_run", "scan_run_id"),
        Index("ix_scan_decisions_stage", "terminal_stage"),
        Index("ix_scan_decisions_reason", "rejection_code"),
        Index("ix_scan_decisions_competition", "competition"),
        Index("ix_scan_decisions_kickoff", "kickoff"),
    )

    scan_run_id: Mapped[int] = mapped_column(
        ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False
    )
    run: Mapped[ScanRun] = relationship(back_populates="decisions")

    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    competition: Mapped[str | None] = mapped_column(String(160))
    competition_code: Mapped[str | None] = mapped_column(String(16))
    kickoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    home_team: Mapped[str | None] = mapped_column(String(160))
    away_team: Mapped[str | None] = mapped_column(String(160))

    competition_supported: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    identity_resolved: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    history_sufficient: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    model_produced: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    fully_modelled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    qualified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    selections_published: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    """How many services published this fixture. Zero is normal and expected."""

    terminal_stage: Mapped[str] = mapped_column(
        String(32), default=ScanStage.SEEN.value, server_default="SEEN", nullable=False
    )
    rejection_code: Mapped[str | None] = mapped_column(String(48))
    rejection_detail: Mapped[str | None] = mapped_column(String(512))
    """The analyser's own wording. Free to change without breaking reports,
    because nothing aggregates on it."""

    coverage_tier: Mapped[str | None] = mapped_column(String(32))
    model_version: Mapped[str | None] = mapped_column(String(64))

    @property
    def reached(self) -> ScanStage:
        """Return the furthest stage this fixture reached."""
        if self.qualified:
            return ScanStage.PUBLISHED
        if self.fully_modelled:
            return ScanStage.FULLY_MODELLED
        if self.model_produced:
            return ScanStage.MODEL_PRODUCED
        if self.history_sufficient:
            return ScanStage.HISTORY_SUFFICIENT
        if self.identity_resolved:
            return ScanStage.IDENTITY_RESOLVED
        if self.competition_supported:
            return ScanStage.SUPPORTED
        return ScanStage.SEEN

    @property
    def is_rejection(self) -> bool:
        """Whether this fixture stopped short of publishing."""
        return self.rejection_code is not None


__all__ = [
    "STAGE_ORDER",
    "RejectionCode",
    "ScanDecision",
    "ScanRun",
    "ScanRunStatus",
    "ScanStage",
]
