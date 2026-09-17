#!/usr/bin/env python3
"""Audit and correct selections settled under the old clean-sheet rule.

**What went wrong.** "Home or clean sheet" was defined with a predicate meaning
*either side* keeps a clean sheet. A 0-2 home defeat therefore settled WON,
because the away team conceded nothing. No bookmaker settles it that way, so
QUANTSPORT displayed WON for a bet that lost. The same applied to "Away or
clean sheet" on any home win to nil.

**What this does.** Re-settles every affected selection under the corrected
rule, reports the effect on every record it touches, and — only when asked —
writes the correction with an audit trail preserving what was originally
recorded.

**It does not rewrite history quietly.** Every corrected row keeps its original
settlement, the corrected settlement, the reason and the timestamp. A record
that changed must be able to say what it used to say and why it does not now.

**The model version is untouched.** These selections were produced by the model
they were produced by. Correcting a settlement rule does not make them
predictions of a different model, and the version string is left exactly as
published.

Run read-only first::

    railway ssh "python scripts/audit_clean_sheet.py"

Then, having read the report::

    railway ssh "python scripts/audit_clean_sheet.py --apply"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.models import HighlightSelection, ServiceSelection
from app.database.models.selections import LOST, SETTLED_STATUSES, WON
from app.infrastructure.database import Database
from app.quant.markets import settles_won

AFFECTED_MARKET = "Result or clean sheet"

CORRECTION_NOTE = (
    "Re-settled {when}: 'clean sheet' previously meant either side keeping one, "
    "so this settled {old} against a real bet that settled {new}. Original "
    "settlement preserved here."
)


def old_rule(outcome: str, home_goals: int, away_goals: int) -> bool | None:
    """Reproduce the superseded predicate exactly.

    Retained as the record of what the old rule was, and used by the tests that
    pin this script's behaviour. Deliberately *not* used to decide which rows
    need correcting: that is decided by comparing stored status against the
    current rule, so the script can tell when its work is already done.
    """
    either_clean_sheet = home_goals == 0 or away_goals == 0
    if outcome == "Home or clean sheet":
        return home_goals > away_goals or either_clean_sheet
    if outcome == "Away or clean sheet":
        return away_goals > home_goals or either_clean_sheet
    if outcome == "Draw or clean sheet":
        return home_goals == away_goals or either_clean_sheet
    return None


@dataclass
class Affected:
    """One selection whose result changes under the corrected rule."""

    table: str
    row_id: int
    day: object
    fixture: str
    service: str
    market: str
    outcome: str
    probability: object
    home_goals: int
    away_goals: int
    old_result: bool
    new_result: bool

    @property
    def direction(self) -> str:
        """Which way the correction moves this row.

        Reads both ends. An earlier version inferred the new result from the
        old one, so a re-run of an already-corrected row reported "LOST -> WON"
        — the exact opposite of what it would write.
        """
        return (
            f"{'WON' if self.old_result else 'LOST'} -> "
            f"{'WON' if self.new_result else 'LOST'}"
        )


async def collect(session: AsyncSession) -> list[Affected]:
    """Return every settled clean-sheet selection whose result changes."""
    affected: list[Affected] = []

    service_rows = await session.execute(
        select(ServiceSelection).where(ServiceSelection.market == AFFECTED_MARKET)
    )
    for row in service_rows.scalars().all():
        if row.status not in SETTLED_STATUSES:
            continue
        if row.home_goals is None or row.away_goals is None:
            continue
        corrected = settles_won(row.market, row.outcome, row.home_goals, row.away_goals)
        if corrected is None:
            continue
        stored = row.status == WON
        # Compared against what is stored, not against what the old rule would
        # have produced. Comparing the two *rules* describes the scoreline and
        # is true forever, so the script re-flagged rows it had already fixed
        # and could never confirm its own work. Comparing against storage makes
        # a second run report nothing, which is what "done" looks like.
        if stored == corrected:
            continue
        affected.append(
            Affected(
                table="service_selections",
                row_id=row.id,
                day=row.selection_date,
                fixture=f"{row.home_name} v {row.away_name}",
                service=row.service_key,
                market=row.market,
                outcome=row.outcome,
                probability=row.probability,
                home_goals=row.home_goals,
                away_goals=row.away_goals,
                old_result=stored,
                new_result=corrected,
            )
        )

    highlight_rows = await session.execute(
        select(HighlightSelection).where(HighlightSelection.market == AFFECTED_MARKET)
    )
    for highlight in highlight_rows.scalars().all():
        if highlight.status not in SETTLED_STATUSES:
            continue
        if highlight.home_goals is None or highlight.away_goals is None:
            continue
        corrected = settles_won(
            highlight.market,
            highlight.outcome,
            highlight.home_goals,
            highlight.away_goals,
        )
        if corrected is None:
            continue
        stored = highlight.status == WON
        if stored == corrected:
            continue
        affected.append(
            Affected(
                table="highlight_selections",
                row_id=highlight.id,
                day=highlight.selection_date,
                fixture=f"{highlight.home_name} v {highlight.away_name}",
                service=getattr(row, "track", "highlight"),
                market=highlight.market,
                outcome=highlight.outcome,
                probability=highlight.probability,
                home_goals=highlight.home_goals,
                away_goals=highlight.away_goals,
                old_result=stored,
                new_result=corrected,
            )
        )

    return affected


async def totals(session: AsyncSession) -> dict[str, tuple[int, int]]:
    """Return settled won/total per table, for the before/after figures."""
    result: dict[str, tuple[int, int]] = {}

    service_rows = await session.execute(
        select(ServiceSelection).where(ServiceSelection.status.in_(SETTLED_STATUSES))
    )
    service_settled = list(service_rows.scalars().all())
    result["service_selections"] = (
        sum(1 for r in service_settled if r.status == WON),
        len(service_settled),
    )

    highlight_rows = await session.execute(
        select(HighlightSelection).where(HighlightSelection.status.in_(SETTLED_STATUSES))
    )
    highlight_settled = list(highlight_rows.scalars().all())
    result["highlight_selections"] = (
        sum(1 for r in highlight_settled if r.status == WON),
        len(highlight_settled),
    )
    return result


def report(affected: list[Affected], before: dict[str, tuple[int, int]]) -> None:
    """Print the full audit."""
    print("=" * 78)
    print("CLEAN-SHEET SETTLEMENT AUDIT")
    print("=" * 78)

    if not affected:
        print("\nNo published selection changes result under the corrected rule.")
        print("The bug was real, but no settled fixture landed on a score that")
        print("distinguishes the two rules.")
        return

    print(f"\nAffected selections: {len(affected)}")
    downgrades = sum(1 for a in affected if a.old_result)
    print(f"  WON -> LOST : {downgrades}")
    print(f"  LOST -> WON : {len(affected) - downgrades}")

    print("\n" + "-" * 78)
    print("EVERY AFFECTED SELECTION")
    print("-" * 78)
    for item in sorted(affected, key=lambda a: (str(a.day), a.fixture)):
        print(f"\n{item.day}  {item.fixture}")
        print(f"  table      : {item.table} #{item.row_id}")
        print(f"  service    : {item.service}")
        print(f"  market     : {item.market} / {item.outcome}")
        print(f"  published  : {item.probability}")
        print(f"  final      : {item.home_goals}-{item.away_goals}")
        print(f"  result     : {item.direction}")

    print("\n" + "-" * 78)
    print("DAILY RECORD IMPACT")
    print("-" * 78)
    by_day: dict[object, list[Affected]] = defaultdict(list)
    for item in affected:
        by_day[item.day].append(item)
    for day in sorted(by_day, key=str):
        items = by_day[day]
        delta = sum((1 if i.new_result else 0) - (1 if i.old_result else 0) for i in items)
        print(f"  {day}: {len(items)} corrected, net {delta:+d} wins")

    print("\n" + "-" * 78)
    print("SERVICE RECORD IMPACT")
    print("-" * 78)
    by_service: dict[str, list[Affected]] = defaultdict(list)
    for item in affected:
        by_service[item.service].append(item)
    for service in sorted(by_service):
        items = by_service[service]
        delta = sum((1 if i.new_result else 0) - (1 if i.old_result else 0) for i in items)
        print(f"  {service}: {len(items)} corrected, net {delta:+d} wins")

    print("\n" + "-" * 78)
    print("OVERALL RECORD, BEFORE AND AFTER")
    print("-" * 78)
    for table, (won, total) in before.items():
        delta = sum(
            (1 if i.new_result else 0) - (1 if i.old_result else 0)
            for i in affected
            if i.table == table
        )
        after = won + delta
        old_rate = f"{won / total:.1%}" if total else "-"
        new_rate = f"{after / total:.1%}" if total else "-"
        print(f"  {table}: {won}/{total} ({old_rate})  ->  {after}/{total} ({new_rate})")


async def apply(session: AsyncSession, affected: list[Affected]) -> None:
    """Write the corrections, preserving what was there before."""
    when = datetime.now(UTC).isoformat(timespec="seconds")
    for item in affected:
        row: ServiceSelection | HighlightSelection | None
        if item.table == "service_selections":
            row = await session.get(ServiceSelection, item.row_id)
        else:
            row = await session.get(HighlightSelection, item.row_id)
        if row is None:
            continue
        # Re-running --apply must not stack a second note onto a row that
        # already carries one. The status write is idempotent; the annotation
        # was not.
        marker = "[SETTLEMENT CORRECTION]"
        already = any(
            marker in (getattr(row, field, "") or "")
            for field in ("rationale", "reasoning")
        )
        note = CORRECTION_NOTE.format(
            when=when,
            old="WON" if item.old_result else "LOST",
            new="WON" if item.new_result else "LOST",
        )
        row.status = WON if item.new_result else LOST
        # The model carries a `corrected` flag for exactly this: a record that
        # changed must say so rather than quietly presenting the new value as
        # what it always said.
        if hasattr(row, "corrected"):
            row.corrected = True
        if not already:
            for field in ("rationale", "reasoning"):
                if hasattr(row, field):
                    existing = getattr(row, field) or ""
                    setattr(row, field, f"{existing}\n\n{marker} {note}".strip())
                    break
    await session.commit()


async def main() -> int:
    """Run the audit, and the correction when asked."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the corrections. Without this the script only reports.",
    )
    args = parser.parse_args()

    configure_logging(get_settings())
    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        before = await totals(session)
        affected = await collect(session)
        report(affected, before)

        if not affected:
            await database.disconnect()
            return 0

        if not args.apply:
            print("\n" + "=" * 78)
            print("READ-ONLY. Nothing was written.")
            print("Re-run with --apply once the above has been read.")
            print("=" * 78)
            await database.disconnect()
            return 0

        await apply(session, affected)
        print("\n" + "=" * 78)
        print(f"APPLIED. {len(affected)} selections re-settled, audit trail written.")
        print("Every corrected row keeps its original settlement in its reasoning.")
        print("=" * 78)
    await database.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
