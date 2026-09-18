"""One fixture per service per day.

Revision ID: e7b4d02f9a13
Revises: d5a93c17e820
Create Date: 2026-09-18

Adds ``uq_service_selection_fixture`` on
``(service_key, selection_date, provider_event_id)``.

``uq_service_selection_day`` constrains ``(service_key, selection_date, rank)``,
which permits the same fixture at two ranks. The scan reruns every three hours;
a fixture whose probability shifts lands at a different rank, finds that slot
free, and publishes again. Users saw the same match twice in one list.

**Existing duplicates must be removed before this runs.** The migration checks
and refuses rather than failing on the constraint, because the cleanup is not a
mechanical delete: it decides which publication is canonical and records what
was removed. ``scripts/audit_duplicate_selections.py --apply`` does that, keeps
the earliest publication, and writes an audit entry for each removal.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e7b4d02f9a13"
down_revision: str | None = "d5a93c17e820"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the fixture uniqueness constraint, refusing if duplicates remain."""
    connection = op.get_bind()
    duplicates = connection.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM (
                SELECT service_key, selection_date, provider_event_id
                FROM service_selections
                GROUP BY service_key, selection_date, provider_event_id
                HAVING COUNT(*) > 1
            ) AS duplicated
            """
        )
    ).scalar_one()

    if duplicates:
        raise RuntimeError(
            f"{duplicates} service/date/fixture groups still hold duplicate "
            "selections. Run scripts/audit_duplicate_selections.py --apply "
            "first: it keeps the earliest publication and records every "
            "removal. This migration will not choose for you."
        )

    op.create_unique_constraint(
        "uq_service_selection_fixture",
        "service_selections",
        ["service_key", "selection_date", "provider_event_id"],
    )


def downgrade() -> None:
    """Drop the fixture uniqueness constraint."""
    op.drop_constraint(
        "uq_service_selection_fixture", "service_selections", type_="unique"
    )
