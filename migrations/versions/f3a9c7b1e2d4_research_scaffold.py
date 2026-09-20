"""Research-scoped persistence for QUANTSPORT NEXT.

Revision ID: f3a9c7b1e2d4
Revises: e7b4d02f9a13
Create Date: 2026-09-20

Adds three research-only tables — ``research_experiments`` (challenger
registry), ``research_predictions`` (append-only shadow/historical forecasts)
and ``research_odds_snapshots`` (append-only prospective market observations).

These are deliberately separate from ``model_versions``, ``predictions`` and
``settled_predictions``, which are production concepts. Nothing here is read by
any customer-facing surface; a challenger reaches a user only through a later,
explicit promotion step. See ``app/database/models/research.py`` for the full
isolation rationale. This migration creates tables only — it touches no
existing table, no production data, and no capability or model state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3a9c7b1e2d4"
down_revision: str | None = "e7b4d02f9a13"
branch_labels: str | None = None
depends_on: str | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def upgrade() -> None:
    """Create the three research-scoped tables."""
    # ── research_experiments — the challenger registry ───────────────────────
    op.create_table(
        "research_experiments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("experiment_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("hypothesis", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "status", sa.String(length=24), nullable=False, server_default="proposed"
        ),
        sa.Column("lane", sa.String(length=8), nullable=True),
        sa.Column(
            "control_version",
            sa.String(length=64),
            nullable=False,
            server_default="model-only-v2-dc",
        ),
        sa.Column("challenger_version", sa.String(length=64), nullable=True),
        sa.Column("data_cutoff", sa.Date(), nullable=True),
        sa.Column("training_start", sa.Date(), nullable=True),
        sa.Column("training_end", sa.Date(), nullable=True),
        sa.Column("validation_start", sa.Date(), nullable=True),
        sa.Column("validation_end", sa.Date(), nullable=True),
        sa.Column("features", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("configuration", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("metrics", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("notes", sa.Text(), nullable=True),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "experiment_id", name="uq_research_experiments_experiment_id"
        ),
        sa.CheckConstraint(
            "training_end IS NULL OR validation_start IS NULL "
            "OR training_end <= validation_start",
            name="ck_research_experiments_train_precedes_validation",
        ),
    )
    op.create_index(
        "ix_research_experiments_status", "research_experiments", ["status"]
    )

    # ── research_predictions — append-only challenger forecasts ──────────────
    op.create_table(
        "research_predictions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("experiment_id", sa.String(length=32), nullable=False),
        sa.Column("challenger_version", sa.String(length=64), nullable=False),
        sa.Column(
            "mode", sa.String(length=12), nullable=False, server_default="historical"
        ),
        sa.Column("provider_event_id", sa.String(length=128), nullable=False),
        sa.Column("competition", sa.String(length=128), nullable=True),
        sa.Column("home_name", sa.String(length=128), nullable=False),
        sa.Column("away_name", sa.String(length=128), nullable=False),
        sa.Column("kickoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("probability", sa.Float(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("home_goals", sa.Integer(), nullable=True),
        sa.Column("away_goals", sa.Integer(), nullable=True),
        sa.Column("settled_outcome", sa.String(length=16), nullable=True),
        sa.Column("hit", sa.Boolean(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.UniqueConstraint(
            "experiment_id",
            "challenger_version",
            "provider_event_id",
            "market",
            "outcome",
            name="uq_research_predictions_shadow",
        ),
        sa.CheckConstraint(
            "probability >= 0 AND probability <= 1",
            name="ck_research_predictions_probability_unit",
        ),
    )
    op.create_index(
        "ix_research_predictions_experiment_id",
        "research_predictions",
        ["experiment_id"],
    )
    op.create_index(
        "ix_research_predictions_kickoff", "research_predictions", ["kickoff"]
    )
    op.create_index(
        "ix_research_predictions_generated_at",
        "research_predictions",
        ["generated_at"],
    )
    op.create_index(
        "ix_research_predictions_created_at", "research_predictions", ["created_at"]
    )

    # ── research_odds_snapshots — append-only prospective market data ────────
    op.create_table(
        "research_odds_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("provider_event_id", sa.String(length=128), nullable=False),
        sa.Column("competition", sa.String(length=128), nullable=True),
        sa.Column("kickoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("bookmaker", sa.String(length=64), nullable=False),
        sa.Column(
            "provider_role",
            sa.String(length=24),
            nullable=False,
            server_default="reference",
        ),
        sa.Column("market", sa.String(length=64), nullable=False),
        sa.Column("selection", sa.String(length=64), nullable=False),
        sa.Column("raw_odds", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column(
            "snapshot_type",
            sa.String(length=16),
            nullable=False,
            server_default="current",
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "provider_event_id",
            "bookmaker",
            "market",
            "selection",
            "snapshot_type",
            "captured_at",
            name="uq_research_odds_snapshot_point",
        ),
        sa.CheckConstraint(
            "raw_odds > 1", name="ck_research_odds_snapshots_odds_gt_one"
        ),
    )
    op.create_index(
        "ix_research_odds_snapshots_provider_event_id",
        "research_odds_snapshots",
        ["provider_event_id"],
    )
    op.create_index(
        "ix_research_odds_snapshots_captured_at",
        "research_odds_snapshots",
        ["captured_at"],
    )
    op.create_index(
        "ix_research_odds_snapshots_kickoff",
        "research_odds_snapshots",
        ["kickoff"],
    )
    op.create_index(
        "ix_research_odds_snapshots_created_at",
        "research_odds_snapshots",
        ["created_at"],
    )


def downgrade() -> None:
    """Drop the research-scoped tables. Production schema is untouched by these."""
    op.drop_table("research_odds_snapshots")
    op.drop_table("research_predictions")
    op.drop_table("research_experiments")
