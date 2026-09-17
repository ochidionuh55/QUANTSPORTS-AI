#!/usr/bin/env python3
"""How much football exists today that QUANTSPORT never sees.

**The missing top of the funnel.** Scan telemetry measures everything from
"fixtures seen" downward, and "fixtures seen" is already filtered: ``get_events``
requests ``fixtures?date=...``, receives the provider's whole card for that
date, then discards every fixture outside our 38 league ids **client-side**.
So the funnel's first number is what we asked for, not what exists, and
``UNSUPPORTED_COMPETITION`` can essentially never fire.

**This costs no extra data.** The same endpoint the scanner already calls
returns the full card. This probe simply declines to throw the rest away.

**It classifies, it does not activate.** A competition appearing here is a
candidate for investigation. Carrying fixtures is not the same as having the
history, the team identities or the validation to model them, and nothing is
ingested, enabled or modelled by running this.

Run::

    railway ssh "python scripts/probe_universe.py"
    railway ssh "python scripts/probe_universe.py --days 3"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.competitions import BY_API_ID, COMPETITIONS

STALE_16 = {
    "ARG", "AUT", "BRA", "CHN", "DEN", "FIN", "IRL", "JAP",
    "MEX", "NOR", "POL", "ROM", "RUS", "SUI", "SWE", "USA",
}

# Competition types that need cross-league modelling before they could ever be
# eligible. Teams in these come from different domestic scoring environments,
# and our rate model has no way to compare a Brazilian side's attack with a
# Spanish one.
INTERNATIONAL_HINTS = (
    "uefa", "conmebol", "concacaf", "champions", "europa", "conference",
    "libertadores", "sudamericana", "afc ", "caf ", "club world",
    "super cup", "nations league", "world cup", "euro ", "copa america",
    "friendlies", "olympic", "qualification",
)

YOUTH_HINTS = (
    "u17", "u18", "u19", "u20", "u21", "u23", "youth", "primavera",
    "women", "feminin", "femenin", "frauen", "reserve", "reserves",
)


@dataclass
class Competition:
    """One competition on the provider's card."""

    league_id: int
    name: str
    country: str
    fixtures: int = 0
    by_weekday: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def code(self) -> str | None:
        """Our code, if this is one of ours."""
        found = BY_API_ID.get(self.league_id)
        return found.code if found else None

    @property
    def is_ours(self) -> bool:
        """Whether this is inside the configured universe."""
        return self.code is not None

    @property
    def lowered(self) -> str:
        return f"{self.name} {self.country}".lower()

    @property
    def is_international(self) -> bool:
        return any(hint in self.lowered for hint in INTERNATIONAL_HINTS)

    @property
    def is_youth_or_secondary(self) -> bool:
        return any(hint in self.lowered for hint in YOUTH_HINTS)

    def classify(self) -> tuple[str, str]:
        """Return a class and the reason for it.

        Deliberately conservative. A competition is only a domestic expansion
        candidate when nothing about it suggests otherwise; everything unclear
        goes to D rather than being assumed suitable.
        """
        if self.is_ours:
            code = self.code or ""
            if code in STALE_16:
                return ("B", f"ours ({code}) — history stops December 2024")
            return ("B", f"ours ({code}) — already configured")
        if self.is_international:
            return ("C", "cross-league: teams from different scoring environments")
        if self.is_youth_or_secondary:
            return ("E", "youth, reserve or women's competition — separate model")
        if not self.country or self.country.lower() in {"world", ""}:
            return ("D", "no country — cannot establish a domestic baseline")
        if self.fixtures <= 1:
            return ("D", "one fixture seen — too little to judge")
        return ("A", "domestic league, needs history and validation before use")


CLASS_NAMES = {
    "A": "domestic expansion candidate",
    "B": "ours — configured or needing refresh",
    "C": "international / cross-league, separate modelling",
    "D": "insufficient or unknown data quality",
    "E": "not suitable",
}


async def probe(days: int) -> int:
    """Fetch the provider's whole card and compare it with our universe."""
    key = os.getenv("API_FOOTBALL_KEY", "")
    if not key:
        print("API_FOOTBALL_KEY is not set on this service.")
        return 1

    from app.providers.api_football import ApiFootballProvider

    provider = ApiFootballProvider(api_key=key)
    today = datetime.now(UTC).date()
    competitions: dict[int, Competition] = {}
    total_fixtures = 0

    for offset in range(days):
        day = today + timedelta(days=offset)
        try:
            # The same call the scanner makes. The difference is that nothing
            # here is discarded for being outside our league ids.
            items: list[dict[str, Any]] = await provider._get(
                "fixtures", {"date": day.isoformat()}
            )
        except Exception as error:  # noqa: BLE001
            print(f"  {day}: request failed — {error}")
            continue

        for item in items:
            league = item.get("league") or {}
            try:
                league_id = int(str(league.get("id")))
            except (TypeError, ValueError):
                continue
            competition = competitions.get(league_id)
            if competition is None:
                competition = Competition(
                    league_id=league_id,
                    name=str(league.get("name") or "?"),
                    country=str(league.get("country") or ""),
                )
                competitions[league_id] = competition
            competition.fixtures += 1
            competition.by_weekday[day.weekday()] += 1
            total_fixtures += 1

    print("=" * 96)
    print(f"PROVIDER UNIVERSE — {today} to {today + timedelta(days=days - 1)}")
    print("=" * 96)
    print(f"\n  Requests used: {provider.budget.used}")

    ours = [c for c in competitions.values() if c.is_ours]
    outside = [c for c in competitions.values() if not c.is_ours]
    ours_fixtures = sum(c.fixtures for c in ours)
    outside_fixtures = sum(c.fixtures for c in outside)

    print("\n" + "-" * 96)
    print("ALL PROVIDER FOOTBALL")
    print("-" * 96)
    print(f"  Total fixtures available  : {total_fixtures}")
    print(f"  Total competitions        : {len(competitions)}")

    print("\n" + "-" * 96)
    print("CURRENT QUANTSPORT UNIVERSE")
    print("-" * 96)
    print(f"  Fixtures inside our 38    : {ours_fixtures}")
    print(f"  Competitions represented  : {len(ours)} of {len(COMPETITIONS)} configured")
    if total_fixtures:
        print(f"  Share of all football     : {ours_fixtures / total_fixtures:.1%}")

    print("\n" + "-" * 96)
    print("OUTSIDE QUANTSPORT")
    print("-" * 96)
    print(f"  Fixtures outside our 38   : {outside_fixtures}")
    print(f"  Competitions outside      : {len(outside)}")

    grouped: dict[str, list[Competition]] = defaultdict(list)
    for competition in competitions.values():
        grouped[competition.classify()[0]].append(competition)

    print("\n" + "-" * 96)
    print("CLASSIFICATION")
    print("-" * 96)
    for key_name in ("A", "B", "C", "D", "E"):
        group = grouped.get(key_name, [])
        fixtures = sum(c.fixtures for c in group)
        print(
            f"  {key_name}  {CLASS_NAMES[key_name]:<52}"
            f"{len(group):>4} comps {fixtures:>5} fixtures"
        )

    for key_name in ("A", "C"):
        group = sorted(grouped.get(key_name, []), key=lambda c: -c.fixtures)
        if not group:
            continue
        print("\n" + "-" * 96)
        print(f"CLASS {key_name} — {CLASS_NAMES[key_name].upper()}")
        print("-" * 96)
        print(f"  {'id':>6}  {'competition':<42}{'country':<22}{'fix':>4}  Mon-Thu")
        for competition in group[:40]:
            early = sum(competition.by_weekday[i] for i in range(0, 4))
            print(
                f"  {competition.league_id:>6}  {competition.name[:41]:<42}"
                f"{competition.country[:21]:<22}{competition.fixtures:>4}{early:>9}"
            )
        if len(group) > 40:
            print(f"  ... and {len(group) - 40} more")

    print("\n" + "=" * 96)
    print("READ-ONLY. Nothing was ingested, activated or modelled.")
    print("A competition listed here carries fixtures. That is not the same as")
    print("having the history, team identities or validation to model it.")
    print("=" * 96)
    return 0


async def main() -> int:
    """Run the probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=1)
    args = parser.parse_args()
    return await probe(max(1, args.days))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
