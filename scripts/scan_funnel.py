#!/usr/bin/env python3
"""Where today's fixtures are lost between the provider and a published selection.

**The question this answers.** The offline audit found that 16 of our 38
competitions hold no history after December 2024, and that those 16 supply 76%
of historical Thursday fixtures. That is an inference from ten years of
weekday distributions. It is not evidence about *today*, and acting on it
without production counts would be exactly the kind of reasoning we refuse
elsewhere.

This measures the real funnel, stage by stage, with a reason attached to every
fixture that falls out:

    provider fixtures
        -> competition supported
        -> both team identities resolved
        -> enough recent history
        -> model produced
        -> threshold passed
        -> published

**Read-only.** Reads stored analyses and, optionally, asks the provider what
fixtures exist today. Ingests nothing.

**What would refute the hypothesis.** If fixtures are lost mainly at identity
resolution, or at the threshold, or the provider simply has few fixtures
today, then stale history is not the binding constraint and a backfill would
not help. That result is as useful as confirmation and is reported plainly.

Run::

    railway ssh "python scripts/scan_funnel.py"
    railway ssh "python scripts/scan_funnel.py --probe-provider"
    railway ssh "python scripts/scan_funnel.py --days 7"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from datetime import date as date_type
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.competitions import COMPETITIONS
from app.core.config import get_settings
from app.database.models import ServiceSelection, StoredAnalysis
from app.infrastructure.database import Database
from app.services.best_of_day import SERVICES

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# The 16 competitions the offline audit found stale. Named here so the funnel
# can attribute losses to them specifically rather than reporting an aggregate
# that cannot distinguish the hypothesis from its alternatives.
SUSPECTED_STALE = {
    "ARG", "AUT", "BRA", "CHN", "DEN", "FIN", "IRL", "JAP",
    "MEX", "NOR", "POL", "ROM", "RUS", "SUI", "SWE", "USA",
}


@dataclass
class Funnel:
    """Counts at each stage, with reasons for every loss."""

    provider_fixtures: int | None = None
    stored: int = 0
    supported: int = 0
    identities_resolved: int = 0
    enough_history: int = 0
    modelled: int = 0
    fully_modelled: int = 0
    published: int = 0

    reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    lost_by_competition: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    modelled_by_competition: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def note(self, reason: str, competition: str | None) -> None:
        """Record one fixture lost, and where it came from."""
        self.reasons[reason] += 1
        if competition:
            self.lost_by_competition[competition] += 1


def _classify(analysis: StoredAnalysis) -> str:
    """Return the stage at which this fixture stopped.

    Derived from the stored coverage tier and the reason text the analyser
    wrote at the time, so the classification reflects what actually happened
    rather than a re-run that might behave differently now.
    """
    coverage = str(analysis.coverage)
    reason = (analysis.unavailable_reason or "").lower()

    if coverage == "unsupported":
        if "could not match" in reason:
            return "identity unresolved, no odds"
        return "unsupported competition"
    if coverage == "data_only":
        if "no matched history" in reason:
            return "identity unresolved, odds only"
        if "below the" in reason or "recent matches on record" in reason:
            return "INSUFFICIENT RECENT HISTORY"
        if "no model could be applied" in reason:
            return "INSUFFICIENT RECENT HISTORY"
        return "data only, other"
    if coverage == "partially_modelled":
        return "partially modelled (under 3 components)"
    return "fully modelled"


async def build(session: object, day: date_type, funnel: Funnel) -> None:
    """Walk one day's stored analyses through the funnel."""
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)

    rows = await session.execute(  # type: ignore[attr-defined]
        select(StoredAnalysis).where(
            StoredAnalysis.kickoff >= start, StoredAnalysis.kickoff < end
        )
    )
    analyses = list(rows.scalars().all())
    funnel.stored += len(analyses)

    by_name = {c.name: c.code for c in COMPETITIONS}

    for analysis in analyses:
        code = by_name.get(str(analysis.competition or ""))
        stage = _classify(analysis)

        if stage == "unsupported competition":
            funnel.note(stage, code)
            continue
        funnel.supported += 1

        if "identity unresolved" in stage:
            funnel.note(stage, code)
            continue
        funnel.identities_resolved += 1

        if stage == "INSUFFICIENT RECENT HISTORY":
            funnel.note(stage, code)
            continue
        funnel.enough_history += 1

        if stage == "data only, other":
            funnel.note(stage, code)
            continue
        funnel.modelled += 1

        if stage == "fully modelled":
            funnel.fully_modelled += 1
            if code:
                funnel.modelled_by_competition[code] += 1
        else:
            funnel.note(stage, code)

    published = await session.execute(  # type: ignore[attr-defined]
        select(ServiceSelection).where(ServiceSelection.selection_date == day)
    )
    funnel.published += len(list(published.scalars().all()))


def report(funnel: Funnel, label: str) -> None:
    """Print the funnel with losses attributed."""
    print("=" * 84)
    print(f"SCAN FUNNEL — {label}")
    print("=" * 84)

    stages: list[tuple[str, int | None]] = [
        ("Provider fixtures", funnel.provider_fixtures),
        ("Stored analyses", funnel.stored),
        ("Supported competition", funnel.supported),
        ("Team identities resolved", funnel.identities_resolved),
        ("Enough recent history", funnel.enough_history),
        ("Model produced", funnel.modelled),
        ("Fully modelled (3 components)", funnel.fully_modelled),
        ("Published selections", funnel.published),
    ]

    previous: int | None = None
    for name, count in stages:
        if count is None:
            print(f"  {name:<32}{'not probed':>10}")
            continue
        drop = ""
        if previous is not None and previous > 0 and count <= previous:
            lost = previous - count
            if lost:
                drop = f"   -{lost} ({lost / previous:.0%})"
        print(f"  {name:<32}{count:>10}{drop}")
        previous = count

    if not funnel.reasons:
        print("\n  Nothing was lost.")
        return

    print("\n" + "-" * 84)
    print("WHY FIXTURES WERE LOST")
    print("-" * 84)
    total_lost = sum(funnel.reasons.values())
    for reason, count in sorted(funnel.reasons.items(), key=lambda item: -item[1]):
        marker = "  <<<" if reason == "INSUFFICIENT RECENT HISTORY" else ""
        print(f"  {reason:<46}{count:>6}  {count / total_lost:>5.0%}{marker}")

    print("\n" + "-" * 84)
    print("THE HYPOTHESIS, TESTED")
    print("-" * 84)
    history_losses = funnel.reasons.get("INSUFFICIENT RECENT HISTORY", 0)
    stale_losses = sum(
        count for code, count in funnel.lost_by_competition.items() if code in SUSPECTED_STALE
    )
    stale_modelled = sum(
        count for code, count in funnel.modelled_by_competition.items() if code in SUSPECTED_STALE
    )

    print(f"  Lost to insufficient history        : {history_losses}")
    print(f"  Lost from the 16 suspected stale    : {stale_losses}")
    print(f"  Still fully modelled from those 16  : {stale_modelled}")

    if total_lost:
        share = stale_losses / total_lost
        print(f"  Stale competitions' share of losses : {share:.0%}")
        print()
        if history_losses == 0:
            print("  VERDICT: not supported. No fixture was lost for want of")
            print("  recent history. A backfill would not add fixtures here.")
        elif share >= 0.4:
            print("  VERDICT: supported. A large share of today's losses come")
            print("  from competitions whose history stops in December 2024.")
            print("  Refreshing them should recover these fixtures directly.")
        elif share >= 0.15:
            print("  VERDICT: partly supported. Stale competitions cost us")
            print("  fixtures, but they are not the dominant cause. Refreshing")
            print("  helps; it will not resolve thin weekdays on its own.")
        else:
            print("  VERDICT: not supported. Losses are dominated by other")
            print("  causes. Look at the reason table above before backfilling.")

    if funnel.lost_by_competition:
        print("\n" + "-" * 84)
        print("LOSSES BY COMPETITION (stale marked *)")
        print("-" * 84)
        ranked = sorted(funnel.lost_by_competition.items(), key=lambda item: -item[1])
        for code, count in ranked[:20]:
            mark = " *" if code in SUSPECTED_STALE else "  "
            print(f"  {code or '(unmapped)':<10}{mark}{count:>6}")


async def probe_provider(day_count: int) -> int | None:
    """Ask the provider how many fixtures exist, to anchor the top of the funnel."""
    from app.providers.api_football import ApiFootballProvider

    key = os.getenv("API_FOOTBALL_KEY", "")
    if not key:
        print("API_FOOTBALL_KEY is not set on this service; skipping probe.\n")
        return None

    provider = ApiFootballProvider(api_key=key)
    try:
        events = await provider.get_events(hours_ahead=24 * day_count)
    except Exception as error:  # noqa: BLE001
        print(f"Provider probe failed: {error}\n")
        return None
    print(f"Provider probe used {provider.budget.used} request(s).\n")
    return len(events)


async def main() -> int:
    """Build and print the funnel."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=1, help="How many days back to include.")
    parser.add_argument(
        "--probe-provider",
        action="store_true",
        help="Ask the provider how many fixtures exist, for the top of the funnel.",
    )
    args = parser.parse_args()

    provider_count = await probe_provider(args.days) if args.probe_provider else None

    database = Database(get_settings())
    await database.connect()

    today = datetime.now(UTC).date()
    async with database.session() as session:
        if args.days <= 1:
            funnel = Funnel(provider_fixtures=provider_count)
            await build(session, today, funnel)
            report(funnel, f"{today:%A %d %B %Y}")
        else:
            combined = Funnel(provider_fixtures=provider_count)
            per_weekday: dict[int, Funnel] = {}
            for offset in range(args.days):
                day = today - timedelta(days=offset)
                await build(session, day, combined)
                single = Funnel()
                await build(session, day, single)
                per_weekday.setdefault(day.weekday(), Funnel())
                target = per_weekday[day.weekday()]
                target.stored += single.stored
                target.fully_modelled += single.fully_modelled
                target.published += single.published
                for reason, count in single.reasons.items():
                    target.reasons[reason] += count

            report(combined, f"last {args.days} days to {today}")

            print("\n" + "-" * 84)
            print("BY WEEKDAY (the number this exercise is about)")
            print("-" * 84)
            print(f"  {'day':<6}{'stored':>8}{'fully modelled':>16}{'published':>11}")
            for index in range(7):
                day_funnel = per_weekday.get(index)
                if day_funnel is None:
                    continue
                print(
                    f"  {WEEKDAYS[index]:<6}{day_funnel.stored:>8}"
                    f"{day_funnel.fully_modelled:>16}{day_funnel.published:>11}"
                )

    await database.disconnect()

    print("\n" + "-" * 84)
    print("SERVICE THRESHOLDS (unchanged, and not to be loosened)")
    print("-" * 84)
    print(f"  {len(SERVICES)} services configured.")
    print("  A service publishing 2 of 40 analysed fixtures is selectivity.")
    print("  A service publishing 2 of 3 analysed fixtures is a coverage problem.")
    print("  The funnel above says which of those we have.")

    print("\n" + "=" * 84)
    print("READ-ONLY. Nothing was ingested, activated or written.")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
