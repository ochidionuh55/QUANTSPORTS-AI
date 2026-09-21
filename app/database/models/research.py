"""Research-scoped persistence for QUANTSPORT NEXT.

**Why these tables exist and why they are separate.** The existing empty
tables were inspected before this was written, and neither was safe to reuse:

* ``model_versions`` is a production concept, not a blank slate. Its own
  ``ModelStatus`` documents that *"only a PROMOTED version may drive
  user-facing value detection"*, so a challenger written there could acquire
  customer-facing power the moment its status changed. It stays reserved for
  the production champion lineage; a challenger earns a row there only when a
  human explicitly promotes it — never as a side effect of research.
* ``predictions`` and ``settled_predictions`` are live production tables
  (``settled_predictions`` holds the settled record read by settlement,
  highlights and market-history; ``predictions`` belongs to the value engine).

So research gets its own namespace. **Nothing in this module is imported by any
customer-facing surface** — not the bot, the public API, the selection or
settlement services, or the capability gate. A challenger prediction can reach
a user only through a deliberate, separate promotion step that does not exist
yet. Isolation is by construction: these tables carry no foreign key from any
production table, and no production query references them.

Research never *modifies or promotes* the control ``model-only-v2-dc`` and never
writes it to a production table. It may, however, record the frozen champion's
own forecast as an append-only paired baseline in ``research_predictions`` — the
champion's logic is unchanged, and pairing a challenger against the frozen grid
on identical inputs is the only way "one thing changed" can be measured.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedAtMixin, IntPrimaryKeyMixin, JSONType, TimestampMixin

# Experiment lifecycle — a superset of the governance states. Stored as plain
# text (not a DB enum) so a new state never needs an ALTER TYPE mid-programme.
EXPERIMENT_STATES = (
    "proposed",
    "running",
    "completed",
    "rejected",
    "promising",
    "oos_validated",
    "shadow",
    "candidate",
)


class ResearchExperiment(IntPrimaryKeyMixin, TimestampMixin, Base):
    """One experiment in the research programme — the challenger registry.

    This is the research equivalent of ``model_versions``, kept deliberately
    separate so a research row can never be mistaken for a promotable
    production model. Promotion out of research is a human act recorded
    elsewhere; nothing here drives production.
    """

    __tablename__ = "research_experiments"

    experiment_id: Mapped[str] = mapped_column(String(32), nullable=False)
    """Stable handle, e.g. ``EXP-004``."""

    title: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="proposed")
    lane: Mapped[str | None] = mapped_column(String(8))

    control_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="model-only-v2-dc"
    )
    challenger_version: Mapped[str | None] = mapped_column(String(64))

    data_cutoff: Mapped[date | None] = mapped_column(Date)
    training_start: Mapped[date | None] = mapped_column(Date)
    training_end: Mapped[date | None] = mapped_column(Date)
    validation_start: Mapped[date | None] = mapped_column(Date)
    validation_end: Mapped[date | None] = mapped_column(Date)

    features: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, server_default="{}", nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("experiment_id", name="uq_research_experiments_experiment_id"),
        CheckConstraint(
            "training_end IS NULL OR validation_start IS NULL "
            "OR training_end <= validation_start",
            name="train_precedes_validation",
        ),
        Index("ix_research_experiments_status", "status"),
    )


class ResearchPrediction(IntPrimaryKeyMixin, CreatedAtMixin, Base):
    """A challenger's forecast for one fixture-market. Append-only.

    The forecast itself is immutable once written: ``probability`` and
    ``generated_at`` are never updated. Only the settlement fields are filled,
    exactly once, after the result is known. A row here is research evidence
    and is never read by any production surface.

    ``mode``:
      * ``historical`` — produced retrospectively over the corpus for
        walk-forward evaluation (``generated_at`` is when the harness ran, not
        a pre-kickoff moment).
      * ``shadow`` — produced live for a future fixture; the writer guarantees
        ``generated_at < kickoff`` so it is genuine prospective evidence.
    """

    __tablename__ = "research_predictions"

    experiment_id: Mapped[str] = mapped_column(String(32), nullable=False)
    challenger_version: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(12), nullable=False, default="historical")

    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    competition: Mapped[str | None] = mapped_column(String(128))
    home_name: Mapped[str] = mapped_column(String(128), nullable=False)
    away_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kickoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    market: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    probability: Mapped[float] = mapped_column(Float, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Settlement — filled once, never edits the forecast above.
    home_goals: Mapped[int | None] = mapped_column(Integer)
    away_goals: Mapped[int | None] = mapped_column(Integer)
    settled_outcome: Mapped[str | None] = mapped_column(String(16))
    hit: Mapped[bool | None] = mapped_column(Boolean)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "challenger_version",
            "provider_event_id",
            "market",
            "outcome",
            name="uq_research_predictions_shadow",
        ),
        CheckConstraint(
            "probability >= 0 AND probability <= 1",
            name="probability_unit",
        ),
        Index("ix_research_predictions_experiment_id", "experiment_id"),
        Index("ix_research_predictions_kickoff", "kickoff"),
        Index("ix_research_predictions_generated_at", "generated_at"),
    )


class ResearchOddsSnapshot(IntPrimaryKeyMixin, CreatedAtMixin, Base):
    """A single observed bookmaker price at a single moment. Append-only.

    Never overwritten: a later price for the same selection is a new row, so
    opening, current and closing observations stay distinguishable. This is the
    isolated EXP-006 store; it does not feed the Outsider board, the website or
    any published surface. A model-vs-market gap here is comparison data, never
    called edge or profitability until genuine market evidence is sufficient.
    """

    __tablename__ = "research_odds_snapshots"

    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    competition: Mapped[str | None] = mapped_column(String(128))
    kickoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    bookmaker: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_role: Mapped[str] = mapped_column(
        String(24), nullable=False, default="reference"
    )
    market: Mapped[str] = mapped_column(String(64), nullable=False)
    selection: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_odds: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)
    snapshot_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="current"
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "provider_event_id",
            "bookmaker",
            "market",
            "selection",
            "snapshot_type",
            "captured_at",
            name="uq_research_odds_snapshot_point",
        ),
        CheckConstraint("raw_odds > 1", name="odds_gt_one"),
        Index("ix_research_odds_snapshots_provider_event_id", "provider_event_id"),
        Index("ix_research_odds_snapshots_captured_at", "captured_at"),
        Index("ix_research_odds_snapshots_kickoff", "kickoff"),
    )
