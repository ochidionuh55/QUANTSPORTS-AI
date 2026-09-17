#!/usr/bin/env python3
"""Reduce the discovered competitions to a research queue, using evidence.

**Not prestige.** A competition is not a candidate because it is famous and
not disqualified because it is a third division. The question is whether we
have enough trustworthy information to model it and whether it would survive
validation — and a fourth tier with nine seasons of complete fixture coverage
is a better prospect than a celebrated league with three.

**Two axes, never multiplied together.** Model readiness and supply value are
reported separately and deliberately not combined into one score. A single
number lets volume compensate for thin data, which is how a competition with
400 matches and eight midweek fixtures would outrank one with 4,500 matches
and two. Midweek supply decides what we investigate first. Readiness decides
what QUANTSPORT is eventually allowed to trust.

**Cheap first.** This is a metadata pass over the league catalogue the probe
already fetches: seasons available, years covered, whether the provider claims
fixture and standings coverage. No ingestion, no per-competition requests, no
modelling. It exists to reduce hundreds of candidates to a handful worth the
expensive work, not to decide anything.

**What it cannot tell you.** Whether the history is *usable*, whether team
identities resolve against our canonical records, and whether a fitted model
calibrates. Those need ingestion and a backtest. Nothing here promotes a
competition past ``HISTORY AVAILABLE``.

Run::

    railway ssh "python scripts/research_queue.py"
    railway ssh "python scripts/research_queue.py --days 7 --min-seasons 5"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.competition_scope import Scope, classify_scope, is_in_scope
from app.core.competitions import BY_API_ID

# The standard our existing divisions were held to. Used as a reference point
# rather than a hard gate: a competition below it is reported with its real
# numbers, not silently dropped.
REFERENCE_MATCHES = 1200

# Scope classification lives in app.core.competition_scope so the same rules
# apply everywhere. Keeping local copies is how "Femenil" passed one script's
# filter after being fixed in another.


@dataclass
class Candidate:
    """One competition, with what the catalogue says about it."""

    league_id: int
    name: str
    country: str
    kind: str
    seasons: list[int] = field(default_factory=list)
    fixture_coverage: int = 0
    standings_coverage: int = 0
    fixtures_midweek: int = 0
    fixtures_weekend: int = 0

    @property
    def is_ours(self) -> bool:
        return self.league_id in BY_API_ID

    @property
    def lowered(self) -> str:
        return f"{self.name} {self.country}".lower()

    @property
    def is_out_of_scope(self) -> bool:
        """Reserve, youth and women's competitions need their own model.

        Not a judgement of quality. A reserve league's scoring environment and
        squad turnover differ enough that our rate model would be estimating
        something other than what it assumes.
        """
        return not is_in_scope(
            classify_scope(self.name, self.country, self.kind, self.league_id)
        )

    @property
    def season_count(self) -> int:
        return len(self.seasons)

    @property
    def season_span(self) -> str:
        if not self.seasons:
            return "-"
        return f"{min(self.seasons)}-{max(self.seasons)}"

    @property
    def covered_seasons(self) -> int:
        """Seasons the provider claims fixture coverage for."""
        return self.fixture_coverage

    @property
    def estimated_matches(self) -> int:
        """Rough historical depth.

        A deliberately crude estimate — 180 matches per covered season, near a
        20-team double round-robin — and labelled as such wherever it appears.
        It is a filter for deciding what to ingest, never evidence about a
        competition.
        """
        return self.covered_seasons * 180

    @property
    def readiness(self) -> tuple[int, str]:
        """Model readiness from catalogue metadata alone.

        Scored on depth and coverage only. It says nothing about whether the
        data is usable or the model would calibrate.
        """
        if self.is_out_of_scope:
            return (0, "out of current scope")
        if self.covered_seasons == 0:
            return (0, "no seasons with fixture coverage")
        if self.covered_seasons < 3:
            return (1, f"{self.covered_seasons} covered season(s) — too thin to fit")
        if self.covered_seasons < 6:
            return (2, f"{self.covered_seasons} covered seasons — fittable, thin")
        if self.covered_seasons < 9:
            return (3, f"{self.covered_seasons} covered seasons — solid depth")
        return (4, f"{self.covered_seasons} covered seasons — deep")

    @property
    def supply(self) -> int:
        """Midweek fixtures contributed in the observed window."""
        return self.fixtures_midweek


async def build(days: int, min_seasons: int) -> int:
    """Fetch the catalogue and the fixture window, then rank."""
    key = os.getenv("API_FOOTBALL_KEY", "")
    if not key:
        print("API_FOOTBALL_KEY is not set on this service.")
        return 1

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass

    from app.providers.api_football import ApiFootballProvider

    provider = ApiFootballProvider(api_key=key)
    candidates: dict[int, Candidate] = {}

    print("  fetching league catalogue ...", flush=True)
    try:
        catalogue: list[dict[str, Any]] = await asyncio.wait_for(
            provider._get("leagues", {}),
            timeout=120,
        )
    except Exception as error:  # noqa: BLE001
        print(f"  catalogue failed: {type(error).__name__}: {error}")
        return 1

    for entry in catalogue:
        league = entry.get("league") or {}
        country = entry.get("country") or {}
        seasons = entry.get("seasons") or []
        try:
            league_id = int(str(league.get("id")))
        except (TypeError, ValueError):
            continue

        candidate = Candidate(
            league_id=league_id,
            name=str(league.get("name") or "?"),
            country=str(country.get("name") or ""),
            kind=str(league.get("type") or ""),
        )
        for season in seasons:
            try:
                candidate.seasons.append(int(str(season.get("year"))))
            except (TypeError, ValueError):
                continue
            coverage = season.get("coverage") or {}
            fixtures = coverage.get("fixtures") or {}
            if fixtures.get("events") or fixtures.get("statistics_fixtures"):
                candidate.fixture_coverage += 1
            elif coverage.get("standings"):
                candidate.standings_coverage += 1
        candidates[league_id] = candidate

    print(f"  catalogue: {len(candidates)} competitions", flush=True)

    today = datetime.now(UTC).date()
    for offset in range(days):
        day = today + timedelta(days=offset)
        try:
            items = await asyncio.wait_for(
                provider._get("fixtures", {"date": day.isoformat()}),
                timeout=60,
            )
        except Exception as error:  # noqa: BLE001
            print(f"  {day}: skipped — {type(error).__name__}", flush=True)
            continue
        for item in items:
            league = item.get("league") or {}
            try:
                league_id = int(str(league.get("id")))
            except (TypeError, ValueError):
                continue
            seen = candidates.get(league_id)
            if seen is None:
                continue
            if day.weekday() <= 3:
                seen.fixtures_midweek += 1
            else:
                seen.fixtures_weekend += 1
        print(f"  {day}: {len(items)} fixtures", flush=True)

    # Senior domestic competitions we do not already carry, that appeared in
    # the window. A competition with no fixtures observed is not rejected — it
    # simply has no supply evidence yet.
    pool = [
        c
        for c in candidates.values()
        if not c.is_ours
        and classify_scope(c.name, c.country, c.kind, c.league_id)
        is Scope.SENIOR_MENS_LEAGUE
        and (c.fixtures_midweek or c.fixtures_weekend)
        and c.country
        and c.country.lower() != "world"
    ]

    print("\n" + "=" * 100)
    print(f"RESEARCH QUEUE — {today} to {today + timedelta(days=days - 1)}")
    print("=" * 100)
    print(f"\n  Requests used: {provider.budget.used}")
    print(f"  Senior domestic leagues outside our universe, seen in window: {len(pool)}")

    in_scope = [c for c in pool if not c.is_out_of_scope]
    out_of_scope = [c for c in pool if c.is_out_of_scope]
    print(f"  Reserve / youth / women's excluded from this queue: {len(out_of_scope)}")

    ready = [c for c in in_scope if c.covered_seasons >= min_seasons]
    thin = [c for c in in_scope if 0 < c.covered_seasons < min_seasons]
    unknown = [c for c in in_scope if c.covered_seasons == 0]

    print("\n" + "-" * 100)
    print("MODEL READINESS (catalogue metadata only — not validation)")
    print("-" * 100)
    print(f"  Meets the depth bar ({min_seasons}+ covered seasons) : {len(ready)}")
    print(f"  Below the bar but has some coverage              : {len(thin)}")
    print(f"  No fixture coverage claimed                      : {len(unknown)}")
    print(
        f"\n  Depth is estimated at ~180 matches per covered season. Our existing\n"
        f"  divisions were held to roughly {REFERENCE_MATCHES} usable matches; a\n"
        "  competition below that is reported with its real numbers, not dropped."
    )

    # Two rankings, printed separately. Combining them would let a shallow
    # competition outrank a deep one on midweek volume alone.
    print("\n" + "-" * 100)
    print("RANKED BY MODEL READINESS (then midweek supply)")
    print("-" * 100)
    print(f"  {'id':>6}  {'competition':<38}{'country':<18}{'seas':>5}{'span':>12}"
          f"{'~matches':>10}{'Mon-Thu':>9}")
    for candidate in sorted(
        ready, key=lambda c: (-c.readiness[0], -c.supply, -c.covered_seasons)
    )[:40]:
        print(
            f"  {candidate.league_id:>6}  {candidate.name[:37]:<38}"
            f"{candidate.country[:17]:<18}{candidate.covered_seasons:>5}"
            f"{candidate.season_span:>12}{candidate.estimated_matches:>10,}"
            f"{candidate.fixtures_midweek:>9}"
        )

    print("\n" + "-" * 100)
    print("RANKED BY MIDWEEK SUPPLY (readiness shown, not applied)")
    print("-" * 100)
    print("  Investigation order only. A competition here is not a candidate")
    print("  for activation until it has been ingested and validated.\n")
    print(f"  {'id':>6}  {'competition':<38}{'country':<18}{'Mon-Thu':>8}"
          f"{'seas':>6}  readiness")
    for candidate in sorted(in_scope, key=lambda c: (-c.supply, -c.covered_seasons))[:25]:
        if not candidate.supply:
            continue
        print(
            f"  {candidate.league_id:>6}  {candidate.name[:37]:<38}"
            f"{candidate.country[:17]:<18}{candidate.fixtures_midweek:>8}"
            f"{candidate.covered_seasons:>6}  {candidate.readiness[1]}"
        )

    print("\n" + "-" * 100)
    print("BOTH: MEETS THE DEPTH BAR AND SUPPLIES MIDWEEK FOOTBALL")
    print("-" * 100)
    both = [c for c in ready if c.supply > 0]
    both.sort(key=lambda c: (-c.supply, -c.covered_seasons))
    if not both:
        print("  None in this window. Widen --days before concluding anything.")
    for candidate in both[:25]:
        print(
            f"  {candidate.league_id:>6}  {candidate.name[:37]:<38}"
            f"{candidate.country[:17]:<18}"
            f"{candidate.covered_seasons:>3} seasons · "
            f"{candidate.fixtures_midweek:>3} midweek · "
            f"{candidate.fixtures_weekend:>3} weekend"
        )

    print("\n" + "=" * 100)
    print("READ-ONLY. Nothing ingested, activated or modelled.")
    print("Catalogue metadata is a filter for what to research. It is not")
    print("evidence that a competition can be modelled: usable history, team")
    print("identity resolution and chronological validation all remain unknown.")
    print("Lifecycle: DISCOVERED -> METADATA VERIFIED -> HISTORY AVAILABLE ->")
    print("IDENTITY FEASIBLE -> INGESTED -> MODEL FITTED -> TEMPORALLY")
    print("VALIDATED -> ACTIVE / REJECTED. This script reaches step two.")
    print("=" * 100)
    return 0


async def main() -> int:
    """Run the metadata pass."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument(
        "--min-seasons",
        type=int,
        default=6,
        help="Covered seasons required to meet the depth bar.",
    )
    args = parser.parse_args()
    return await build(max(1, args.days), max(1, args.min_seasons))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
