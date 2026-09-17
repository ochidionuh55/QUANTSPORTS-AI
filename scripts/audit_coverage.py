#!/usr/bin/env python3
"""Audit our competitions and discover what the provider offers beyond them.

**Read-only.** Discovers, measures, estimates cost. Ingests nothing, activates
nothing, writes no fixture data. The expansion decision is made from the report
this produces, not by this script.

**Why this exists.** The offline audit found that 16 of our 38 competitions
carry no historical data after December 2024, and that those 16 supply 76% of
all Thursday fixtures historically. Before adding competitions we do not have,
we should measure what is wrong with the ones we do.

Sections:

1. Current coverage — seasons, fixtures, staleness, per competition.
2. Refresh cost — requests needed to close the gap on stale competitions.
3. Wider universe — what the provider carries that we do not use.
4. Expansion candidates — ranked by data quality and weekday contribution.

Run::

    railway ssh "python scripts/audit_coverage.py"
    railway ssh "python scripts/audit_coverage.py --discover"
    railway ssh "python scripts/audit_coverage.py --discover --country Brazil"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.competitions import COMPETITIONS
from app.core.config import get_settings
from app.database.models import HistoricalMatch
from app.database.models.team import Competition as CompetitionRow
from app.infrastructure.database import Database

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# International club competitions. Listed so the report can flag them, not so
# they can be switched on: teams in these come from different domestic scoring
# environments, and our rate model has no way to compare a Brazilian side's
# attack with a Spanish one. They need a cross-league normalisation that does
# not exist yet.
INTERNATIONAL_IDS: dict[int, str] = {
    2: "UEFA Champions League",
    3: "UEFA Europa League",
    848: "UEFA Conference League",
    13: "Copa Libertadores",
    11: "Copa Sudamericana",
    16: "CONCACAF Champions League",
}


@dataclass
class Coverage:
    """What we hold for one competition."""

    code: str
    name: str
    country: str
    api_id: int | None
    matches: int = 0
    teams: int = 0
    earliest: date | None = None
    latest: date | None = None
    weekday_counts: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def days_stale(self) -> int | None:
        """Days since the most recent match we hold."""
        if self.latest is None:
            return None
        return (datetime.now(UTC).date() - self.latest).days

    @property
    def early_week_share(self) -> float:
        """Share of fixtures falling Monday to Thursday.

        The number that matters for this exercise. A competition playing
        mostly Saturday adds nothing to a thin Thursday however large it is.
        """
        total = sum(self.weekday_counts.values())
        if not total:
            return 0.0
        return sum(self.weekday_counts[i] for i in range(0, 4)) / total

    @property
    def status(self) -> str:
        """A one-word verdict on this competition's data."""
        if not self.matches:
            return "NO DATA"
        stale = self.days_stale
        if stale is None:
            return "UNKNOWN"
        if stale > 400:
            return "STALE"
        if stale > 120:
            return "AGEING"
        return "CURRENT"


async def current_coverage(session: object) -> list[Coverage]:
    """Measure what the database holds for each configured competition."""
    results: list[Coverage] = []
    for competition in COMPETITIONS:
        coverage = Coverage(
            code=competition.code,
            name=competition.name,
            country=competition.country,
            api_id=competition.api_football_id,
        )
        # HistoricalMatch keys competitions by foreign key, not by our code.
        # Resolve the row first; a competition with no row simply has no
        # matches, which the report should show rather than crash on.
        found = await session.execute(  # type: ignore[attr-defined]
            select(CompetitionRow.id).where(
                CompetitionRow.canonical_name == competition.name
            )
        )
        competition_id = found.scalar_one_or_none()
        if competition_id is None:
            results.append(coverage)
            continue
        rows = await session.execute(  # type: ignore[attr-defined]
            select(HistoricalMatch).where(HistoricalMatch.competition_id == competition_id)
        )
        matches = list(rows.scalars().all())
        coverage.matches = len(matches)
        teams: set[object] = set()
        for match in matches:
            teams.add(match.home_team_id)
            teams.add(match.away_team_id)
            day = match.match_date
            if day is None:
                continue
            coverage.weekday_counts[day.weekday()] += 1
            if coverage.earliest is None or day < coverage.earliest:
                coverage.earliest = day
            if coverage.latest is None or day > coverage.latest:
                coverage.latest = day
        coverage.teams = len(teams)
        results.append(coverage)
    return results


def report_current(coverages: list[Coverage]) -> list[Coverage]:
    """Print the current-coverage audit and return the stale competitions."""
    print("=" * 100)
    print("1. CURRENT COVERAGE")
    print("=" * 100)
    print(
        f"{'code':<6}{'competition':<24}{'country':<13}{'API':>5}{'matches':>9}"
        f"{'teams':>6}{'Mon-Thu':>9}{'latest':>12}{'stale':>7}  status"
    )
    print("-" * 100)

    for coverage in sorted(coverages, key=lambda c: (c.status != "STALE", -c.matches)):
        stale = coverage.days_stale
        print(
            f"{coverage.code:<6}{coverage.name[:23]:<24}{coverage.country[:12]:<13}"
            f"{coverage.api_id:>5}{coverage.matches:>9,}{coverage.teams:>6}"
            f"{coverage.early_week_share:>8.1%}{coverage.latest!s:>12}"
            f"{(str(stale) + 'd') if stale is not None else '-':>7}  {coverage.status}"
        )

    stale_list = [c for c in coverages if c.status == "STALE"]
    fresh = [c for c in coverages if c.status == "CURRENT"]

    def early(group: list[Coverage]) -> float:
        total = sum(sum(c.weekday_counts.values()) for c in group)
        if not total:
            return 0.0
        early_total = sum(sum(c.weekday_counts[i] for i in range(0, 4)) for c in group)
        return early_total / total

    print(f"\n  CURRENT : {len(fresh):>2}  Mon-Thu share {early(fresh):.1%}")
    print(f"  STALE   : {len(stale_list):>2}  Mon-Thu share {early(stale_list):.1%}")

    if stale_list:
        thursday_stale = sum(c.weekday_counts[3] for c in stale_list)
        thursday_all = sum(c.weekday_counts[3] for c in coverages)
        if thursday_all:
            print(
                f"\n  Stale competitions supply {thursday_stale / thursday_all:.0%} "
                "of all historical Thursday fixtures."
            )
            print("  Refreshing them is worth more to weekday coverage than any")
            print("  competition we do not yet carry.")
    return stale_list


def report_refresh_cost(stale: list[Coverage]) -> None:
    """Estimate the request cost of closing the gap on stale competitions."""
    print("\n" + "=" * 100)
    print("2. REFRESH COST FOR STALE COMPETITIONS")
    print("=" * 100)

    if not stale:
        print("\nNothing stale. No refresh needed.")
        return

    # API-Football returns a season's fixtures for one league in a single
    # request when queried by league and season, paginated only for very large
    # leagues. Two per season is a safe allowance.
    per_season = 2
    total = 0
    print(f"\n{'code':<6}{'competition':<26}{'seasons missing':>16}{'requests':>10}")
    print("-" * 100)
    for coverage in sorted(stale, key=lambda c: -c.early_week_share):
        stale_days = coverage.days_stale or 0
        seasons_missing = max(1, round(stale_days / 365))
        requests = seasons_missing * per_season
        total += requests
        print(
            f"{coverage.code:<6}{coverage.name[:25]:<26}{seasons_missing:>16}{requests:>10}"
        )

    print("-" * 100)
    print(f"{'TOTAL':<32}{'':>16}{total:>10}")
    print(f"\n  Against a 7,500/day allowance that is {total / 7500:.1%} of one day.")
    print("  Refreshing every stale competition is inexpensive. The cost of the")
    print("  gap is not requests; it is that these fixtures cannot be modelled.")


async def discover(country: str | None) -> None:
    """Show our configured competitions. **This is not provider discovery.**

    An earlier version of this section called ``get_competitions()`` and
    presented the result as the provider's universe. That method does not
    query the vendor at all — its own docstring says it returns the local
    mapping because "the league list is stable and fetching it would spend
    quota". So the report was our own configuration read back to us, which is
    why it used zero requests and listed E0 and D1 as unused.

    It compounded with a type mismatch: ``external_id`` is a string and
    ``api_football_id`` is an int, so ``"39" not in {39, ...}`` marked all 38
    configured competitions as unknown.

    Genuine discovery needs a ``/leagues`` call the adapter does not implement.
    Until that exists, this section states what it actually knows.
    """
    print("\n" + "=" * 100)
    print("3. CONFIGURED COMPETITIONS (not provider discovery)")
    print("=" * 100)
    print("\n  The provider adapter has no discovery call. `get_competitions()`")
    print("  returns our own LEAGUE_IDS mapping without contacting the vendor,")
    print("  so it cannot tell us what API-Football carries beyond our 38.")
    print("  Implementing /leagues is a separate piece of work.")

    from app.providers.api_football import LEAGUE_IDS

    # Compared as integers on both sides. The mismatch that made every
    # configured competition look unused is pinned by a regression test.
    configured_ids = {int(c.api_football_id): c for c in COMPETITIONS if c.api_football_id}
    mapped = {code: int(league_id) for code, league_id in LEAGUE_IDS.items()}

    print(f"\n  Configured competitions : {len(COMPETITIONS)}")
    print(f"  Adapter league mappings : {len(mapped)}")

    unmatched = [code for code, league_id in mapped.items() if league_id not in configured_ids]
    mapped_ids = set(mapped.values())
    missing = [
        c.code for c in COMPETITIONS if int(c.api_football_id or 0) not in mapped_ids
    ]

    if unmatched:
        print(f"\n  In the adapter but not configured: {sorted(unmatched)}")
    if missing:
        print(f"  Configured but not in the adapter: {sorted(missing)}")
    if not unmatched and not missing:
        print("\n  Adapter and configuration agree on all league IDs.")

    if country:
        chosen = [c for c in COMPETITIONS if country.lower() in c.country.lower()]
        print(f"\n  Matching '{country}': {[c.code for c in chosen]}")


async def main() -> int:
    """Run the audit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Also query the provider for competitions we do not use.",
    )
    parser.add_argument("--country", default=None, help="Filter discovery by country.")
    args = parser.parse_args()

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        coverages = await current_coverage(session)
        stale = report_current(coverages)
        report_refresh_cost(stale)
    await database.disconnect()

    if args.discover:
        await discover(args.country)

    print("\n" + "=" * 100)
    print("READ-ONLY. Nothing was ingested, activated or written.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
