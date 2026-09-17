"""Scan telemetry: durable record of what each scan saw and rejected.

Revision ID: a7c2e1d94f30
Revises: 65cacc8c767b
Create Date: 2026-09-17

Adds ``scan_runs`` and ``scan_decisions``.

These are operational evidence, not model state. ``stored_analyses`` is pruned
once a fixture finishes, which is correct for a live analysis and fatal for a
record of what we rejected: the evidence disappeared days after it was created.
These tables are retained by scan age instead, so the funnel remains
answerable long after the matches have been played.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a7c2e1d94f30"
down_revision: str | None = "65cacc8c767b"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the scan telemetry tables."""
    op.create_table(
        "scan_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.Column("grid_version", sa.String(length=64), nullable=True),
        sa.Column("hours_ahead", sa.Integer(), nullable=True),
        # Funnel counts. Unique fixtures at every stage.
        sa.Column("fixtures_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("supported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("identity_resolved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sufficient_history", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_produced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fully_modelled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "fixtures_with_qualifying_selection",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        # Outside the funnel: one fixture can publish in several services.
        sa.Column("selections_published", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicates_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("odds_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="RUNNING"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_scan_runs_started", "scan_runs", ["started_at"])
    op.create_index("ix_scan_runs_status", "scan_runs", ["status"])

    op.create_table(
        "scan_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "scan_run_id",
            sa.Integer(),
            sa.ForeignKey("scan_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_event_id", sa.String(length=128), nullable=False),
        sa.Column("competition", sa.String(length=160), nullable=True),
        sa.Column("competition_code", sa.String(length=16), nullable=True),
        sa.Column("kickoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("home_team", sa.String(length=160), nullable=True),
        sa.Column("away_team", sa.String(length=160), nullable=True),
        sa.Column("competition_supported", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("identity_resolved", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("history_sufficient", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("model_produced", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("fully_modelled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("qualified", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("selections_published", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("terminal_stage", sa.String(length=32), nullable=False, server_default="SEEN"),
        sa.Column("rejection_code", sa.String(length=48), nullable=True),
        sa.Column("rejection_detail", sa.String(length=512), nullable=True),
        sa.Column("coverage_tier", sa.String(length=32), nullable=True),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # One decision per fixture per run: a retried scan or a feed repeating
        # a fixture must not inflate the funnel.
        sa.UniqueConstraint(
            "scan_run_id", "provider_event_id", name="uq_scan_decision_fixture"
        ),
    )
    op.create_index("ix_scan_decisions_run", "scan_decisions", ["scan_run_id"])
    op.create_index("ix_scan_decisions_stage", "scan_decisions", ["terminal_stage"])
    op.create_index("ix_scan_decisions_reason", "scan_decisions", ["rejection_code"])
    op.create_index("ix_scan_decisions_competition", "scan_decisions", ["competition"])
    op.create_index("ix_scan_decisions_kickoff", "scan_decisions", ["kickoff"])


def downgrade() -> None:
    """Drop the scan telemetry tables."""
    op.drop_table("scan_decisions")
    op.drop_table("scan_runs")
