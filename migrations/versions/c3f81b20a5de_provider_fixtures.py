"""Provider fixtures: every fixture returned, trainable or not.

Revision ID: c3f81b20a5de
Revises: a7c2e1d94f30
Create Date: 2026-09-17

Adds ``provider_fixtures``.

``historical_matches`` is the training set: its goals are NOT NULL and a check
constraint requires them non-negative. It cannot hold an abandoned fixture, and
altering it would mean dropping those guarantees on a table of 113,029 rows
that every model, backtest and validation path reads.

This table sits beside it and records what the provider actually had, so a
fixture that cannot train is still visibly a fixture rather than an absence.
Goals are nullable here because an abandoned match has no score and that
absence is the information.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c3f81b20a5de"
down_revision: str | None = "a7c2e1d94f30"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the provider fixtures table."""
    op.create_table(
        "provider_fixtures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("provider_fixture_id", sa.String(length=128), nullable=False),
        sa.Column("provider_league_id", sa.Integer(), nullable=False),
        sa.Column("competition_code", sa.String(length=16), nullable=True),
        sa.Column("season", sa.String(length=16), nullable=False),
        sa.Column("kickoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_home_team_id", sa.String(length=64), nullable=True),
        sa.Column("provider_away_team_id", sa.String(length=64), nullable=True),
        sa.Column("home_team_name", sa.String(length=160), nullable=False),
        sa.Column("away_team_name", sa.String(length=160), nullable=False),
        sa.Column(
            "home_team_id",
            sa.BigInteger(),
            sa.ForeignKey("teams.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "away_team_id",
            sa.BigInteger(),
            sa.ForeignKey("teams.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status_code", sa.String(length=16), nullable=True),
        sa.Column("status_class", sa.String(length=24), nullable=False),
        # Nullable, unlike historical_matches. An abandoned match has no score.
        sa.Column("home_goals", sa.Integer(), nullable=True),
        sa.Column("away_goals", sa.Integer(), nullable=True),
        sa.Column(
            "training_eligible",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("exclusion_reason", sa.String(length=160), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_payload_hash", sa.String(length=64), nullable=True),
        # TimestampMixin expects the database to stamp these. A NOT NULL column
        # with no server default rejects every insert — that mistake took the
        # production scan down once already.
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
            "provider_name",
            "provider_fixture_id",
            name="uq_provider_fixtures_provider_fixture",
        ),
    )
    op.create_index("ix_provider_fixtures_league", "provider_fixtures", ["provider_league_id"])
    op.create_index("ix_provider_fixtures_season", "provider_fixtures", ["season"])
    op.create_index("ix_provider_fixtures_kickoff", "provider_fixtures", ["kickoff"])
    op.create_index(
        "ix_provider_fixtures_eligible", "provider_fixtures", ["training_eligible"]
    )
    op.create_index("ix_provider_fixtures_status", "provider_fixtures", ["status_code"])
    op.create_index("ix_provider_fixtures_home_team", "provider_fixtures", ["home_team_id"])
    op.create_index("ix_provider_fixtures_away_team", "provider_fixtures", ["away_team_id"])


def downgrade() -> None:
    """Drop the provider fixtures table."""
    op.drop_table("provider_fixtures")
