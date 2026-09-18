#!/usr/bin/env python3
"""One fixture may appear once in a service's list for a day. Not twice.

**The defect.** ``uq_service_selection_day`` is on
``(service_key, selection_date, rank)``, and ``_store`` checks the same three
columns. So the question asked before every insert is "is rank 5 taken?" —
never "is this fixture already in this list?". The scan runs every three hours;
a fixture whose probability shifts slightly lands at a different rank on a
later run, finds that slot free, and is published again. Users saw the same
match at positions 4 and 5 of the same service.

Every individual insert satisfied the database. The rule that was missing was
one nobody had written down.

**The earliest publication is canonical.** It is what users first saw, what the
audit trail records, and what any screenshot of the board shows. Renumbering
ranks to close the gap would rewrite a board people already read, so the later
row is removed and the earlier one left exactly where it was.

**Removals are recorded, not erased.** Each one writes a ``SelectionAudit``
entry against the surviving row naming the removed id, its rank, its publication
time and the reason. A migration that quietly drops rows is the same failure as
settling a bet under a predicate nobody checked: internally tidy, externally
unaccountable.

Run::

    railway ssh "python scripts/audit_duplicate_selections.py"
    railway ssh "python scripts/audit_duplicate_selections.py --apply"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import SelectionAudit, ServiceSelection
from app.infrastructure.database import Database
from app.services.best_of_day import SERVICES

REASON = "DUPLICATE_FIXTURE_IN_SERVICE"


async def main() -> int:
    """Find, report and optionally remove duplicate fixture entries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        rows = await session.execute(
            select(ServiceSelection).order_by(
                ServiceSelection.service_key,
                ServiceSelection.selection_date,
                ServiceSelection.provider_event_id,
                # Earliest publication first, so the first of each group is the
                # one to keep. Rank breaks ties for rows published in the same
                # transaction.
                ServiceSelection.published_at,
                ServiceSelection.rank,
            )
        )
        selections = list(rows.scalars().all())

        grouped: dict[tuple[str, object, str], list[ServiceSelection]] = defaultdict(list)
        for selection in selections:
            grouped[
                (
                    selection.service_key,
                    selection.selection_date,
                    selection.provider_event_id,
                )
            ].append(selection)

        duplicates = {key: group for key, group in grouped.items() if len(group) > 1}

        print("=" * 96)
        print("DUPLICATE SELECTION AUDIT")
        print("=" * 96)
        print(f"\n  total selection rows        : {len(selections):,}")
        print(f"  distinct service/date/fixture: {len(grouped):,}")
        print(f"  groups holding duplicates    : {len(duplicates)}")
        removable = sum(len(group) - 1 for group in duplicates.values())
        print(f"  rows to remove               : {removable}")

        if not duplicates:
            print("\n  No duplicates. Every fixture appears at most once per")
            print("  service and date.")
            await database.disconnect()
            print("\n" + "=" * 96)
            return 0

        by_service: dict[str, int] = defaultdict(int)
        affected_fixtures: set[str] = set()
        for (service_key, _, fixture_id), group in duplicates.items():
            by_service[service_key] += len(group) - 1
            affected_fixtures.add(fixture_id)

        print("\n" + "-" * 96)
        print("AFFECTED SERVICES")
        print("-" * 96)
        labels = {s.key: s.label for s in SERVICES}
        for service_key, count in sorted(by_service.items(), key=lambda i: -i[1]):
            print(f"  {labels.get(service_key, service_key)[:44]:<46}{count:>5} to remove")
        print(f"\n  distinct fixtures affected: {len(affected_fixtures)}")

        print("\n" + "-" * 96)
        print("EVERY DUPLICATE")
        print("-" * 96)
        now = datetime.now(UTC)
        removed = 0
        for key in sorted(duplicates, key=lambda k: (str(k[1]), k[0])):
            group = duplicates[key]
            keep = group[0]
            drop = group[1:]
            print(
                f"\n  {keep.selection_date}  {keep.service_key}"
                f"  {keep.home_name} v {keep.away_name}"
            )
            print(
                f"    keep   #{keep.id} rank {keep.rank}"
                f"  published {keep.published_at:%H:%M:%S}"
            )
            for row in drop:
                print(
                    f"    remove #{row.id} rank {row.rank}"
                    f"  published {row.published_at:%H:%M:%S}"
                )
                if args.apply:
                    # Recorded against the survivor, so the removal is
                    # discoverable from the row that remains.
                    session.add(
                        SelectionAudit(
                            selection_id=keep.id,
                            event=REASON,
                            detail=(
                                f"removed duplicate #{row.id} (rank {row.rank}, "
                                f"published {row.published_at:%Y-%m-%d %H:%M:%S}); "
                                f"kept #{keep.id} (rank {keep.rank}) as the earliest "
                                f"publication of {row.home_name} v {row.away_name} "
                                f"for {row.service_key}"
                            ),
                            occurred_at=now,
                        )
                    )
                    await session.delete(row)
                    removed += 1

        if args.apply:
            await session.flush()
            # Prove it before committing: a re-count on the same session.
            check = await session.execute(select(ServiceSelection))
            remaining: dict[tuple[str, object, str], int] = defaultdict(int)
            for selection in check.scalars().all():
                remaining[
                    (
                        selection.service_key,
                        selection.selection_date,
                        selection.provider_event_id,
                    )
                ] += 1
            still_duplicated = [k for k, count in remaining.items() if count > 1]
            if still_duplicated:
                await session.rollback()
                print(f"\n  *** {len(still_duplicated)} groups still duplicated after")
                print("  *** cleanup. Rolled back; nothing was written.")
                await database.disconnect()
                return 1

            await session.commit()
            print(f"\n  APPLIED. {removed} duplicate rows removed, {len(duplicates)}")
            print("  groups reconciled. Every removal is recorded against the")
            print("  surviving selection in selection_audits.")
            print("  Verified: zero duplicate service/date/fixture groups remain.")
        else:
            await session.rollback()
            print("\n  DRY RUN — nothing written.")

    await database.disconnect()
    print("\n" + "=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
