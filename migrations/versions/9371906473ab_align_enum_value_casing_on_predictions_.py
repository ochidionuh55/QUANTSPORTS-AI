"""align enum value casing on predictions and outcomes

Revision ID: 9371906473ab
Revises: 7c3af50a828e
Create Date: 2026-09-08 01:08:54.245762+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "9371906473ab"
down_revision: str | None = "7c3af50a828e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the migration."""
    # Alembic does not compare CHECK constraints, so this migration is hand
    # written.
    #
    # predictions.margin_method and outcomes.status were declared without
    # values_callable, so SQLAlchemy persisted the enum MEMBER NAMES
    # ("PROPORTIONAL") while every other enum column persisted the enum VALUES
    # ("proportional"). The same Python enum therefore round-tripped as two
    # different strings depending on which table it was written to, and a query
    # filtering on a literal value would match one table and silently return
    # nothing from the other.
    #
    # Rows are normalised before the constraints are replaced. Both tables are
    # empty at this revision, but the UPDATE keeps the migration safe to apply
    # in an environment where they are not.
    op.execute("UPDATE predictions SET margin_method = lower(margin_method)")
    op.execute("UPDATE outcomes SET status = lower(status)")

    with op.batch_alter_table("predictions", schema=None) as batch_op:
        batch_op.drop_constraint("margin_method", type_="check")
        batch_op.create_check_constraint(
            "margin_method",
            "margin_method IN ('proportional', 'power', 'shin')",
        )

    with op.batch_alter_table("outcomes", schema=None) as batch_op:
        batch_op.drop_constraint("outcome_status", type_="check")
        batch_op.create_check_constraint(
            "outcome_status",
            "status IN ('active', 'suspended', 'settled', 'removed')",
        )


def downgrade() -> None:
    """Revert the migration."""
    op.execute("UPDATE predictions SET margin_method = upper(margin_method)")
    op.execute("UPDATE outcomes SET status = upper(status)")

    with op.batch_alter_table("predictions", schema=None) as batch_op:
        batch_op.drop_constraint("margin_method", type_="check")
        batch_op.create_check_constraint(
            "margin_method",
            "margin_method IN ('PROPORTIONAL', 'POWER', 'SHIN')",
        )

    with op.batch_alter_table("outcomes", schema=None) as batch_op:
        batch_op.drop_constraint("outcome_status", type_="check")
        batch_op.create_check_constraint(
            "outcome_status",
            "status IN ('ACTIVE', 'SUSPENDED', 'SETTLED', 'REMOVED')",
        )
