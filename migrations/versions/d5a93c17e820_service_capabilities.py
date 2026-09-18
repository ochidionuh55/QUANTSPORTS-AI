"""Service capabilities: per competition and service publication permission.

Revision ID: d5a93c17e820
Revises: c3f81b20a5de
Create Date: 2026-09-18

Adds ``service_capabilities``.

Holds the whole Wave 1 validation matrix, not only the capabilities that
passed: 168 rows across 8 competitions and 21 services. The withheld and
insufficient rows are the reason the passing ones mean anything, and a future
model change is compared against them rather than overwriting them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d5a93c17e820"
down_revision: str | None = "c3f81b20a5de"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the capability table."""
    op.create_table(
        "service_capabilities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competition_code", sa.String(length=16), nullable=False),
        sa.Column("provider_league_id", sa.Integer(), nullable=True),
        sa.Column("competition_name", sa.String(length=160), nullable=False),
        sa.Column("service_key", sa.String(length=64), nullable=False),
        sa.Column("service_label", sa.String(length=160), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("validation_version", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=48), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column(
            "qualifying_sample", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("total_sample", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("predicted_rate", sa.Float(), nullable=True),
        sa.Column("observed_rate", sa.Float(), nullable=True),
        sa.Column("calibration_gap", sa.Float(), nullable=True),
        sa.Column("interval_low", sa.Float(), nullable=True),
        sa.Column("interval_high", sa.Float(), nullable=True),
        sa.Column("brier", sa.Float(), nullable=True),
        sa.Column("window_spread", sa.Float(), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False),
        # TimestampMixin expects the database to stamp these. A NOT NULL column
        # with no server default rejects every insert, which took the scan down
        # once already.
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
        sa.UniqueConstraint(
            "competition_code",
            "service_key",
            "model_version",
            name="uq_service_capabilities_competition_service_version",
        ),
    )
    op.create_index(
        "ix_service_capabilities_competition", "service_capabilities", ["competition_code"]
    )
    op.create_index("ix_service_capabilities_state", "service_capabilities", ["state"])
    op.create_index(
        "ix_service_capabilities_version", "service_capabilities", ["model_version"]
    )


def downgrade() -> None:
    """Drop the capability table."""
    op.drop_table("service_capabilities")
