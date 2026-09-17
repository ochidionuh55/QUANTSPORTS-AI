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
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.competitions import BY_API_ID, COMPETITIONS

STALE_16 = {
    "ARG", "AUT", "BRA", "CHN", "DEN", "FIN", "IRL", "JAP",
    "MEX", "NOR", "POL", "ROM", "RUS", "SUI", "SWE", "USA",
}

class CompetitionType(str, Enum):
    """What kind of competition this is.

    Separated because the classification drives which leagues get researched,
    and the first version got it wrong in three ways: domestic cups were filed
    as expansion candidates, "USL Championship" matched a substring of
    "champions" and landed under international, and Toppserien — Norway's
    women's top division — passed every filter because its name contains no
    word my heuristics looked for.
    """

    SENIOR_MENS_LEAGUE = "senior men's domestic league"
    SENIOR_MENS_CUP = "senior men's domestic cup"
    WOMENS = "women's competition"
    YOUTH_RESERVE = "youth or reserve"
    INTERNATIONAL_CLUB = "international club competition"
    INTERNATIONAL_NATIONAL = "international national-team competition"
    UNKNOWN = "unknown"


WOMENS_LEAGUE_IDS: frozenset[int] = frozenset(
    {
        725,  # Toppserien (Norway). Named nothing like a women's competition.
        724,  # Damallsvenskan (Sweden)
        696,  # Frauen-Bundesliga (Germany)
        44,  # FA Women's Super League (England)
        139,  # Primera Division Femenina (Spain)
        64,  # Division 1 Feminine (France)
    }
)
"""Women's competitions whose names give no clue.

A curated list because name matching cannot catch these. Incomplete by nature,
which is why anything unmatched falls to UNKNOWN rather than being assumed to
be a men's league.
"""

WOMENS_HINTS = ("women", "feminin", "femenin", "frauen", "femminile", "dames")

YOUTH_HINTS = (
    "u17", "u18", "u19", "u20", "u21", "u23", "youth", "junior",
    "primavera", "reserve", "reserves", "academy",
)

# Matched as whole words. "championship" contains "champions", which is how a
# domestic American league was filed as an international competition.
INTERNATIONAL_CLUB_WORDS = {
    "uefa", "conmebol", "concacaf", "afc", "caf", "libertadores",
    "sudamericana", "recopa",
}
INTERNATIONAL_CLUB_PHRASES = (
    "champions league", "europa league", "conference league",
    "club world cup", "super cup", "caribbean club", "central american cup",
)
INTERNATIONAL_NATIONAL_PHRASES = (
    "world cup", "nations league", "euro ", "copa america", "friendlies",
    "african cup", "asian cup", "gold cup", "qualification", "olympic",
    "championship - u",
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

    kind: str = ""
    """The provider's own "League" or "Cup", from the /leagues endpoint.

    **Not available on the fixtures endpoint.** Its ``league`` object carries
    only id, name and country. Assuming a ``type`` field was there left every
    domestic league classified UNKNOWN and Class A empty, which read as "no
    expansion candidates exist" when it meant "the field was never populated".
    """

    @property
    def lowered(self) -> str:
        return f"{self.name} {self.country}".lower()

    @property
    def words(self) -> set[str]:
        """Whole words, so "championship" never matches "champions"."""
        return set(re.findall(r"[a-z]+", self.lowered))

    @property
    def competition_type(self) -> CompetitionType:
        """Classify from the provider's fields first, names only as fallback."""
        lowered = self.lowered
        if self.league_id in WOMENS_LEAGUE_IDS or any(
            hint in lowered for hint in WOMENS_HINTS
        ):
            return CompetitionType.WOMENS
        if any(hint in lowered for hint in YOUTH_HINTS):
            return CompetitionType.YOUTH_RESERVE
        if any(phrase in lowered for phrase in INTERNATIONAL_NATIONAL_PHRASES):
            return CompetitionType.INTERNATIONAL_NATIONAL
        if self.words & INTERNATIONAL_CLUB_WORDS or any(
            phrase in lowered for phrase in INTERNATIONAL_CLUB_PHRASES
        ):
            return CompetitionType.INTERNATIONAL_CLUB
        if not self.country or self.country.lower() == "world":
            return CompetitionType.INTERNATIONAL_NATIONAL
        # The provider states League or Cup. A cup draws teams from several
        # divisions, so its fixtures span scoring environments our rate model
        # cannot compare — the same obstacle as a continental competition.
        if self.kind.lower() == "cup":
            return CompetitionType.SENIOR_MENS_CUP
        if self.kind.lower() == "league":
            return CompetitionType.SENIOR_MENS_LEAGUE
        # Catalogue unavailable. Fall back to the name, marked as a guess by
        # returning UNKNOWN for anything that does not clearly read as a cup.
        # Better a smaller Class A than a confident wrong one.
        if any(word in self.lowered for word in ("cup", "kupa", "copa", "coupe", "pokal")):
            return CompetitionType.SENIOR_MENS_CUP
        return CompetitionType.UNKNOWN

    @property
    def is_international(self) -> bool:
        return self.competition_type in {
            CompetitionType.INTERNATIONAL_CLUB,
            CompetitionType.INTERNATIONAL_NATIONAL,
        }

    @property
    def is_youth_or_secondary(self) -> bool:
        return self.competition_type in {
            CompetitionType.WOMENS,
            CompetitionType.YOUTH_RESERVE,
        }

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

        kind = self.competition_type
        if kind in {CompetitionType.WOMENS, CompetitionType.YOUTH_RESERVE}:
            return ("E", f"{kind.value} — outside current scope")
        if kind in {
            CompetitionType.INTERNATIONAL_CLUB,
            CompetitionType.INTERNATIONAL_NATIONAL,
        }:
            return ("C", f"{kind.value} — needs cross-league modelling")
        if kind is CompetitionType.SENIOR_MENS_CUP:
            # A cup pairs a top-flight side with a fourth-tier one. The rate
            # model has no way to compare teams from different divisions, which
            # is the same obstacle continental competitions present.
            return ("C", "domestic cup — teams span divisions")
        if kind is CompetitionType.UNKNOWN:
            return ("D", "competition type not established from the catalogue")
        if self.fixtures <= 1:
            return ("D", "one fixture seen — too little to judge")
        # Class A now requires a senior men's domestic league, established from
        # the provider's own type field rather than inferred from its name.
        return ("A", "senior men's domestic league — needs history and validation")


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

    # One request for the league catalogue. This is the only place the provider
    # states whether a competition is a League or a Cup, and that distinction
    # decides whether a competition can be modelled at all — a cup draws teams
    # from several divisions. Worth a request; guessing from names is what
    # filed four domestic cups as expansion candidates.
    league_types: dict[int, str] = {}

    # Unbuffered. stdout is a pipe under `railway ssh`, so Python buffers it and
    # a session that closes before the process finishes loses everything
    # written so far — which is why a three-day probe returned no output at all
    # while the one-day probe worked. Progress is printed as we go for the same
    # reason: partial output beats none.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass

    try:
        print("  fetching league catalogue ...", flush=True)
        catalogue = await asyncio.wait_for(
            provider._get("leagues", {}),
            timeout=90,
        )
        for entry in catalogue:
            league = entry.get("league") or {}
            try:
                league_types[int(str(league.get("id")))] = str(league.get("type") or "")
            except (TypeError, ValueError):
                continue
        print(f"  catalogue: {len(league_types)} competitions typed", flush=True)
    except Exception as error:  # noqa: BLE001
        # Without it every competition falls to UNKNOWN, which is reported
        # honestly rather than guessed around.
        print(f"  league catalogue unavailable ({type(error).__name__}) — "
              "competition types will read as unknown", flush=True)
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
                    kind=league_types.get(league_id, ""),
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

    # Senior domestic only: the like-for-like comparison. Counting youth,
    # reserve and international fixtures on the outside against senior domestic
    # on the inside would overstate the gap.
    def senior_domestic(group: list[Competition]) -> list[Competition]:
        return [
            c for c in group if not c.is_international and not c.is_youth_or_secondary
        ]

    ours_senior = senior_domestic(ours)
    outside_senior = senior_domestic(outside)
    ours_senior_fixtures = sum(c.fixtures for c in ours_senior)
    outside_senior_fixtures = sum(c.fixtures for c in outside_senior)

    print("\n" + "-" * 96)
    print("SENIOR DOMESTIC ONLY (like for like)")
    print("-" * 96)
    print(f"  Inside our 38             : {ours_senior_fixtures} fixtures, "
          f"{len(ours_senior)} competitions")
    print(f"  Outside our 38            : {outside_senior_fixtures} fixtures, "
          f"{len(outside_senior)} competitions")
    senior_total = ours_senior_fixtures + outside_senior_fixtures
    if senior_total:
        print(f"  Our share of senior domestic: {ours_senior_fixtures / senior_total:.1%}")

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
