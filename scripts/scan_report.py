#!/usr/bin/env python3
"""Report what the scans saw, and why fixtures did not reach a selection.

**Reads durable telemetry only.** ``scan_runs`` and ``scan_decisions`` are
written by every scan and retained by scan age, so unlike the first attempt at
this report the population does not disappear when fixtures finish.

**The funnel counts unique fixtures at every stage.** Selections are reported
separately, because one fixture can publish in several services and counting
rows at the last stage is what made the earlier diagnostic show more published
than analysed.

**Nothing here is reconstructed.** Telemetry begins when it was deployed. Dates
before that have no scan evidence and are not estimated, inferred or filled in.
Published and settled history remains available separately and is unaffected.

Run::

    railway ssh "python scripts/scan_report.py"
    railway ssh "python scripts/scan_report.py --days 7"
    railway ssh "python scripts/scan_report.py --check"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models import ScanDecision, ScanRun
from app.infrastructure.database import Database

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

STALE_16 = {
    "ARG", "AUT", "BRA", "CHN", "DEN", "FIN", "IRL", "JAP",
    "MEX", "NOR", "POL", "ROM", "RUS", "SUI", "SWE", "USA",
}
"""The competitions whose history stops in December 2024.

Grouped separately so the refresh hypothesis can be tested against scan
evidence rather than against ten-year weekday distributions.
"""


async def check_tables(session: AsyncSession) -> bool:
    """Confirm the telemetry tables exist before reporting on them."""
    result = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN ('scan_runs', 'scan_decisions')"
        )
    )
    found = sorted(row[0] for row in result.all())
    print("Telemetry tables present:", found or "NONE")
    if len(found) < 2:
        print("\n  Missing. Run: alembic upgrade head")
        return False
    return True


async def report_runs(session: AsyncSession, days: int) -> list[ScanRun]:
    """List the scans in the window."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = await session.execute(
        select(ScanRun).where(ScanRun.started_at >= cutoff).order_by(ScanRun.started_at)
    )
    runs = list(rows.scalars().all())

    print("=" * 92)
    print(f"SCAN RUNS — last {days} day(s)")
    print("=" * 92)

    if not runs:
        print("\n  No scans recorded yet.")
        print("  Telemetry records the next scheduled scan; nothing is backfilled.")
        return []

    print(
        f"\n  {'id':>4}  {'started':<17}{'status':<17}{'seen':>6}{'supp':>6}"
        f"{'ident':>7}{'hist':>6}{'model':>7}{'full':>6}{'qual':>6}{'sel':>6}"
    )
    print("  " + "-" * 88)
    for run in runs:
        print(
            f"  {run.id:>4}  {run.started_at:%Y-%m-%d %H:%M}  {run.status:<17}"
            f"{run.fixtures_seen:>6}{run.supported:>6}{run.identity_resolved:>7}"
            f"{run.sufficient_history:>6}{run.model_produced:>7}{run.fully_modelled:>6}"
            f"{run.fixtures_with_qualifying_selection:>6}{run.selections_published:>6}"
        )
        if not run.funnel_is_monotonic():
            print(f"        !! funnel widens on run {run.id} — stage mapping is wrong")
        if (
            run.fully_modelled
            and not run.fixtures_with_qualifying_selection
            and not run.selections_published
        ):
            # Publication attaches after the scan. A run recorded before that
            # wiring existed, or read before publication ran, has a truncated
            # funnel and must not be mistaken for a conversion failure.
            print(
                f"        note: run {run.id} has no publication telemetry "
                "(pre-fix run, or publication has not run yet)"
            )

    earliest = min(r.started_at for r in runs)
    print(f"\n  Scan telemetry available since: {earliest:%d %b %Y %H:%M} UTC")
    return runs


async def report_funnel(session: AsyncSession, runs: list[ScanRun], days: int) -> None:
    """Aggregate the funnel over the window, counting each fixture once."""
    if not runs:
        return

    run_ids = [r.id for r in runs]
    rows = await session.execute(
        select(ScanDecision).where(ScanDecision.scan_run_id.in_(run_ids))
    )
    decisions = list(rows.scalars().all())
    if not decisions:
        print("\n  No fixture decisions recorded.")
        return

    # A fixture scanned repeatedly across runs is one fixture. Keeping the
    # furthest state it ever reached avoids counting the same match once per
    # scan, which would inflate every stage equally and hide the real shape.
    best: dict[str, ScanDecision] = {}
    for decision in decisions:
        current = best.get(decision.provider_event_id)
        if current is None or _rank(decision) > _rank(current):
            best[decision.provider_event_id] = decision
    unique = list(best.values())

    stages = [
        ("Fixtures seen", len(unique)),
        ("Supported competition", sum(1 for d in unique if d.competition_supported)),
        ("Identity resolved", sum(1 for d in unique if d.identity_resolved)),
        ("Sufficient history", sum(1 for d in unique if d.history_sufficient)),
        ("Model produced", sum(1 for d in unique if d.model_produced)),
        ("Fully modelled", sum(1 for d in unique if d.fully_modelled)),
        ("Produced >=1 selection", sum(1 for d in unique if d.qualified)),
    ]

    print("\n" + "=" * 92)
    print(f"FIXTURE FUNNEL — {len(unique):,} unique fixtures over {days} day(s)")
    print("=" * 92 + "\n")

    previous: int | None = None
    for name, count in stages:
        bar = "#" * int((count / stages[0][1]) * 40) if stages[0][1] else ""
        lost = f"   -{previous - count}" if previous is not None and previous > count else ""
        print(f"  {name:<24}{count:>7}  {bar}{lost}")
        if previous is not None and count > previous:
            print("        !! this stage is larger than the one above it")
        previous = count

    published = sum(d.selections_published for d in unique)
    print(f"\n  Total selections published{published:>7}   (outside the funnel: "
          "one fixture, many services)")

    # Rejections.
    reasons: dict[str, int] = defaultdict(int)
    for decision in unique:
        if decision.rejection_code:
            reasons[decision.rejection_code] += 1

    if reasons:
        total = sum(reasons.values())
        print("\n" + "-" * 92)
        print(f"WHY FIXTURES DID NOT PUBLISH — {total:,} rejections")
        print("-" * 92)
        for code, count in sorted(reasons.items(), key=lambda item: -item[1]):
            print(f"  {code:<32}{count:>7}{count / total:>8.0%}")

    # The stale-16 hypothesis, against scan evidence.
    stale_seen = [d for d in unique if (d.competition_code or "") in STALE_16]
    stale_lost = [d for d in stale_seen if d.rejection_code]
    history_lost = [
        d for d in unique if d.rejection_code == "INSUFFICIENT_HISTORY"
    ]
    stale_history = [
        d for d in history_lost if (d.competition_code or "") in STALE_16
    ]

    print("\n" + "-" * 92)
    print("THE STALE-16 HYPOTHESIS, AGAINST SCAN EVIDENCE")
    print("-" * 92)
    print(f"  Fixtures seen from the 16 stale competitions : {len(stale_seen)}")
    print(f"  Of those, rejected                           : {len(stale_lost)}")
    print(f"  Rejected anywhere for insufficient history   : {len(history_lost)}")
    print(f"  Of those, from the stale 16                  : {len(stale_history)}")

    if not history_lost:
        print("\n  No fixture was lost for want of recent history in this window.")
        print("  A backfill would not have added fixtures here.")
    elif len(stale_history) / len(history_lost) >= 0.5:
        print("\n  Most history rejections come from the stale competitions.")
        print("  Refreshing them should recover these fixtures directly.")
    else:
        print("\n  History rejections are not concentrated in the stale 16.")
        print("  A refresh would help less than the coverage audit implied.")

    # Competitions.
    lost_by_competition: dict[str, int] = defaultdict(int)
    for decision in unique:
        if decision.rejection_code:
            lost_by_competition[decision.competition_code or "(unmapped)"] += 1

    if lost_by_competition:
        print("\n" + "-" * 92)
        print("REJECTIONS BY COMPETITION (stale marked *)")
        print("-" * 92)
        ranked = sorted(lost_by_competition.items(), key=lambda item: -item[1])
        for code, count in ranked[:25]:
            mark = "*" if code in STALE_16 else " "
            print(f"  {code:<14}{mark}{count:>6}")

    # Weekday, by kickoff rather than scan time.
    by_weekday: dict[int, list[ScanDecision]] = defaultdict(list)
    for decision in unique:
        if decision.kickoff:
            by_weekday[decision.kickoff.weekday()].append(decision)

    if by_weekday:
        print("\n" + "-" * 92)
        print("BY KICKOFF WEEKDAY")
        print("-" * 92)
        print(f"  {'day':<6}{'seen':>7}{'supported':>11}{'fully modelled':>16}{'qualified':>11}")
        for index in range(7):
            group = by_weekday.get(index, [])
            if not group:
                continue
            print(
                f"  {WEEKDAYS[index]:<6}{len(group):>7}"
                f"{sum(1 for d in group if d.competition_supported):>11}"
                f"{sum(1 for d in group if d.fully_modelled):>16}"
                f"{sum(1 for d in group if d.qualified):>11}"
            )

    # Service-level conversion.
    #
    # "analysed" is the number of fixtures that were actually eligible for a
    # service's qualification test: fully modelled fixtures kicking off that
    # day. Unsupported fixtures and ones that never produced a model
    # probability are excluded, because counting them would inflate the
    # denominator with fixtures the service never had the chance to consider.
    from app.database.models import ServiceSelection
    from app.services.best_of_day import SERVICES

    eligible_by_day: dict[object, int] = defaultdict(int)
    for decision in unique:
        if decision.fully_modelled and decision.kickoff:
            eligible_by_day[decision.kickoff.date()] += 1

    if eligible_by_day:
        selection_days = sorted(eligible_by_day, key=str)
        selection_rows = await session.execute(
            select(ServiceSelection).where(
                ServiceSelection.selection_date.in_(selection_days)
            )
        )
        selections = list(selection_rows.scalars().all())
        qualified_by_service: dict[str, set[str]] = defaultdict(set)
        rows_by_service: dict[str, int] = defaultdict(int)
        for selection in selections:
            qualified_by_service[selection.service_key].add(selection.provider_event_id)
            rows_by_service[selection.service_key] += 1

        total_eligible = sum(eligible_by_day.values())
        print("\n" + "-" * 92)
        print("SERVICE CONVERSION")
        print("-" * 92)
        print("  analysed = fully modelled fixtures eligible for the test that day\n")
        for service in SERVICES:
            qualified = len(qualified_by_service.get(service.key, set()))
            published = rows_by_service.get(service.key, 0)
            extra = f"   ({published} selections)" if published != qualified else ""
            print(
                f"  {service.label:<34}{total_eligible:>5} analysed"
                f" · {qualified:>3} qualified{extra}"
            )

    # Coverage tiers.
    tiers: dict[str, int] = defaultdict(int)
    for decision in unique:
        tiers[decision.coverage_tier or "(none)"] += 1
    print("\n" + "-" * 92)
    print("COVERAGE TIER")
    print("-" * 92)
    for tier, count in sorted(tiers.items(), key=lambda item: -item[1]):
        print(f"  {tier:<28}{count:>7}")


def _rank(decision: ScanDecision) -> int:
    """How far this decision got, for keeping the best across repeated scans."""
    return sum(
        (
            decision.competition_supported,
            decision.identity_resolved,
            decision.history_sufficient,
            decision.model_produced,
            decision.fully_modelled,
            decision.qualified,
        )
    )


async def main() -> int:
    """Print the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument(
        "--check", action="store_true", help="Only verify the tables exist."
    )
    args = parser.parse_args()

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        ready = await check_tables(session)
        if not ready or args.check:
            await database.disconnect()
            return 0 if ready else 1
        print()
        runs = await report_runs(session, args.days)
        await report_funnel(session, runs, args.days)
    await database.disconnect()

    print("\n" + "=" * 92)
    print("READ-ONLY. Telemetry is not reconstructed for dates before it was deployed.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
