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

**Existing duplicates are removed here, not refused.** A first version raised
instead, on the reasoning that choosing which publication survives is not a
mechanical decision. But migrations run at worker startup, so the refusal took
the service down rather than blocking a change — a guard that turns a data
problem into an outage is the wrong guard.

The choice it was protecting is made explicitly: the **earliest** publication
is canonical, because it is what users first saw and what the audit trail
already records. Every removal writes a ``selection_audits`` row against the
surviving selection naming the removed id, its rank and its publication time,
so nothing disappears silently.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e7b4d02f9a13"
down_revision: str | None = "d5a93c17e820"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Remove duplicate fixtures, recording each, then add the constraint."""
    connection = op.get_bind()

    # Every row that is not the earliest publication of its fixture for its
    # service and day. Ordered by published_at, with rank and id breaking ties
    # for rows written in the same transaction, so the survivor is
    # deterministic rather than whichever the planner returned first.
    doomed = connection.execute(
        sa.text(
            """
            SELECT id, service_key, selection_date, provider_event_id,
                   rank, published_at, home_name, away_name, keeper
            FROM (
                SELECT id, service_key, selection_date, provider_event_id,
                       rank, published_at, home_name, away_name,
                       FIRST_VALUE(id) OVER (
                           PARTITION BY service_key, selection_date,
                                        provider_event_id
                           ORDER BY published_at, rank, id
                       ) AS keeper
                FROM service_selections
            ) AS ranked
            WHERE id <> keeper
            """
        )
    ).fetchall()

    for row in doomed:
        connection.execute(
            sa.text(
                """
                INSERT INTO selection_audits
                    (selection_id, event, detail, occurred_at,
                     created_at, updated_at)
                VALUES (:selection_id, :event, :detail, NOW(), NOW(), NOW())
                """
            ),
            {
                "selection_id": row.keeper,
                "event": "DUPLICATE_FIXTURE_IN_SERVICE",
                "detail": (
                    f"removed duplicate #{row.id} (rank {row.rank}, published "
                    f"{row.published_at}); kept #{row.keeper} as the earliest "
                    f"publication of {row.home_name} v {row.away_name} for "
                    f"{row.service_key} on {row.selection_date}"
                ),
            },
        )

    if doomed:
        connection.execute(
            sa.text("DELETE FROM service_selections WHERE id = ANY(:ids)"),
            {"ids": [row.id for row in doomed]},
        )

    # Verify before constraining: if anything is still duplicated the create
    # would fail anyway, and this says why.
    remaining = connection.execute(
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
    if remaining:
        raise RuntimeError(
            f"{remaining} groups still duplicated after cleanup removed "
            f"{len(doomed)} rows. The deduplication is wrong, not the data."
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
