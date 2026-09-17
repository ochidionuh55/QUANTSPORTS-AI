#!/usr/bin/env python3
"""Real historical depth for candidate competitions. No estimates.

**Where estimates stop.** The research queue reported depth as
``covered_seasons × 180``, a constant invented to rank a long list cheaply.
Serie C Girone A's "1,980 matches" was arithmetic, not a count. This script
asks the provider for each season's fixtures and counts what actually comes
back, so nothing downstream carries a number nobody measured.

**It also answers the window question.** Every fixture is checked against the
season and the date it claims, and unique provider ids are counted separately
from rows returned. If a supply figure was inflated by a feed repeating
fixtures, or by fixtures outside the window being counted, that shows here
rather than silently ranking a competition too high.

**It reaches HISTORY AVAILABLE and stops.** Counting fixtures says nothing
about whether team identities resolve or whether a fitted model calibrates.
Nothing is ingested and no competition is promoted past this step.

Run::

    railway ssh "python scripts/history_audit.py"
    railway ssh "python scripts/history_audit.py --leagues 138,382,291 --seasons 6"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The depth-plus-midweek intersection from the research queue, in the order it
# produced. Not a shortlist: the point of this audit is to let counted history
# reorder them, and to let some fail.
DEFAULT_CANDIDATES: dict[int, str] = {
    496: "Liga Alef (Israel)",
    138: "Serie C - Girone A (Italy)",
    399: "NPFL (Nigeria)",
    243: "Liga Pro Serie B (Ecuador)",
    287: "Prva Liga (Serbia)",
    382: "Liga Leumit (Israel)",
    291: "Azadegan League (Iran)",
    317: "1st League - RS (Bosnia)",
    240: "Primera B (Colombia)",
    563: "Ettan - Norra (Sweden)",
}

FINISHED_STATUSES = {"FT", "AET", "PEN"}

ABANDONED_STATUSES = {"ABD", "CANC", "PST", "SUSP", "AWD", "WO", "INT"}
"""Fixtures that will never produce a usable result.

Reported separately because a competition carrying two seasons of these is
telling us something a single completeness percentage hid entirely.
"""

SCHEDULED_STATUSES = {"NS", "TBD", "PST"}
"""Not yet played. A legitimate gap in the current season, not a data fault."""

RESULTS_PATH = Path("/tmp/quantsport_history_audit.json")
"""Where completed competitions are kept between runs.

An SSH session reset has repeatedly killed this audit part-way and thrown away
every competition already counted, costing the requests again on the retry.
Each competition is written as soon as it finishes, so a dropped connection
loses at most the one in flight and ``--summary`` can aggregate whatever has
accumulated across however many invocations it took.
"""


def _load() -> dict[str, dict[str, Any]]:
    """Read previously completed competitions."""
    try:
        return dict(json.loads(RESULTS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _save(store: dict[str, dict[str, Any]]) -> None:
    """Persist completed competitions, tolerating a read-only filesystem."""
    try:
        RESULTS_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")
    except OSError as error:
        print(f"  (could not persist results: {error})", flush=True)


@dataclass
class SeasonAudit:
    """What one season of one competition actually contains."""

    season: int
    returned: int = 0
    unique_ids: set[str] = field(default_factory=set)
    completed: int = 0
    usable_scores: int = 0
    missing_scores: int = 0
    teams: set[int] = field(default_factory=set)
    missing_team_ids: int = 0
    unfinished: int = 0
    abandoned: int = 0
    scheduled: int = 0
    out_of_season: int = 0
    midweek: int = 0
    weekend: int = 0

    @property
    def duplicates(self) -> int:
        """Rows returned beyond distinct fixture ids."""
        return self.returned - len(self.unique_ids)

    @property
    def completeness(self) -> float:
        """Share of **returned** fixtures that are usable.

        Previously the denominator was ``completed``, which counts only
        fixtures already marked finished — so it asked "of the matches that
        finished, how many had a score", and the provider essentially never
        returns a finished fixture without one. Every competition scored
        100.0%, including one with two seasons of abandoned fixtures.

        Measured against everything returned, a season that never finished
        shows as the gap it is.
        """
        if not self.returned:
            return 0.0
        return self.usable_scores / self.returned


@dataclass
class CompetitionAudit:
    """Counted history for one competition."""

    league_id: int
    label: str
    seasons: list[SeasonAudit] = field(default_factory=list)

    @property
    def usable(self) -> int:
        return sum(s.usable_scores for s in self.seasons)

    @property
    def returned(self) -> int:
        return sum(s.returned for s in self.seasons)

    @property
    def completed(self) -> int:
        return sum(s.completed for s in self.seasons)

    @property
    def duplicates(self) -> int:
        return sum(s.duplicates for s in self.seasons)

    @property
    def usable_seasons(self) -> int:
        """Seasons with enough usable matches to contribute to a fit."""
        return sum(1 for s in self.seasons if s.usable_scores >= 100)

    @property
    def teams(self) -> int:
        found: set[int] = set()
        for season in self.seasons:
            found |= season.teams
        return len(found)

    @property
    def completeness(self) -> float:
        """Usable share of everything returned."""
        if not self.returned:
            return 0.0
        return self.usable / self.returned

    @property
    def unfinished(self) -> int:
        return sum(s.unfinished for s in self.seasons)

    @property
    def abandoned(self) -> int:
        return sum(s.abandoned for s in self.seasons)

    @property
    def scheduled(self) -> int:
        return sum(s.scheduled for s in self.seasons)

    @property
    def midweek(self) -> int:
        return sum(s.midweek for s in self.seasons)

    @property
    def weekend(self) -> int:
        return sum(s.weekend for s in self.seasons)

    @property
    def status(self) -> str:
        """A verdict on counted history alone.

        Says nothing about identity resolution or calibration, both of which
        remain unmeasured at this step.
        """
        if self.usable == 0:
            return "NO_HISTORY"
        if self.abandoned > self.returned * 0.05:
            return "HISTORY_GAPS"
        if self.completeness < 0.85:
            return "HISTORY_INCOMPLETE"
        if self.usable >= 1200 and self.usable_seasons >= 6:
            return "HISTORY_STRONG"
        if self.usable >= 600:
            return "HISTORY_MODERATE"
        return "HISTORY_THIN"


def _parse(item: dict[str, Any], audit: SeasonAudit) -> None:
    """Fold one fixture into a season's audit."""
    fixture = item.get("fixture") or {}
    league = item.get("league") or {}
    teams = item.get("teams") or {}
    goals = item.get("goals") or {}

    audit.returned += 1
    fixture_id = str(fixture.get("id") or "")
    if fixture_id:
        audit.unique_ids.add(fixture_id)

    # The fixture must belong to the season we asked for. A feed returning
    # neighbouring seasons would inflate every count here.
    try:
        if int(str(league.get("season"))) != audit.season:
            audit.out_of_season += 1
    except (TypeError, ValueError):
        audit.out_of_season += 1

    status = str((fixture.get("status") or {}).get("short") or "")
    if status in ABANDONED_STATUSES:
        audit.abandoned += 1
    elif status in SCHEDULED_STATUSES:
        audit.scheduled += 1
    elif status not in FINISHED_STATUSES:
        audit.unfinished += 1
    if status in FINISHED_STATUSES:
        audit.completed += 1
        home_goals = goals.get("home")
        away_goals = goals.get("away")
        if home_goals is not None and away_goals is not None:
            audit.usable_scores += 1
        else:
            audit.missing_scores += 1

    for side in ("home", "away"):
        team = (teams.get(side) or {}).get("id")
        if team is None:
            audit.missing_team_ids += 1
        else:
            try:
                audit.teams.add(int(str(team)))
            except (TypeError, ValueError):
                audit.missing_team_ids += 1

    raw_date = str(fixture.get("date") or "")
    try:
        kickoff = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    except ValueError:
        return
    if kickoff.weekday() <= 3:
        audit.midweek += 1
    else:
        audit.weekend += 1


def report_stored() -> int:
    """Print every competition counted so far, across all invocations."""
    store = _load()
    if not store:
        print("No results stored yet. Run the audit first.")
        return 1

    print("=" * 104)
    print(f"HISTORY AUDIT — {len(store)} competitions counted across all runs")
    print("=" * 104)
    print(
        f"\n  {'competition':<32}{'returned':>9}{'usable':>8}{'unfin':>7}{'abnd':>6}"
        f"{'sched':>7}{'usable/ret':>11}{'teams':>7}{'Mon-Thu':>9}  status"
    )
    rows = sorted(store.values(), key=lambda r: -int(r.get("usable", 0)))
    for row in rows:
        print(
            f"  {str(row.get('label'))[:31]:<32}{int(row.get('returned', 0)):>9,}"
            f"{int(row.get('usable', 0)):>8,}{int(row.get('unfinished', 0)):>7,}"
            f"{int(row.get('abandoned', 0)):>6,}{int(row.get('scheduled', 0)):>7,}"
            f"{float(row.get('completeness', 0)):>10.1%}{int(row.get('teams', 0)):>7}"
            f"{int(row.get('midweek', 0)):>9,}  {row.get('status')}"
        )

    print("\n" + "-" * 104)
    print("DATA INTEGRITY (all runs)")
    print("-" * 104)
    for label, field_name in (
        ("Duplicate fixture rows", "duplicates"),
        ("Fixtures outside their season", "out_of_season"),
        ("Fixtures missing a team id", "missing_team_ids"),
        ("Completed without a score", "missing_scores"),
    ):
        total = sum(int(r.get(field_name, 0)) for r in store.values())
        print(f"  {label:<32}: {total}")

    print("\n" + "=" * 104)
    print("READ-ONLY. Reaches HISTORY AVAILABLE. Identity resolution and")
    print("chronological validation remain unmeasured.")
    print("=" * 104)
    return 0


async def audit(league_ids: dict[int, str], seasons_back: int, refresh: bool = False) -> int:
    """Count real history for each candidate."""
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
    current_year = datetime.now().year
    results: list[CompetitionAudit] = []
    store = _load()

    for league_id, label in league_ids.items():
        if str(league_id) in store and not refresh:
            print(f"\n  {label} (id {league_id}) — already counted, skipping", flush=True)
            continue
        competition = CompetitionAudit(league_id=league_id, label=label)
        print(f"\n  {label} (id {league_id})", flush=True)
        for offset in range(seasons_back):
            season = current_year - 1 - offset
            season_audit = SeasonAudit(season=season)
            try:
                items = await asyncio.wait_for(
                    provider._get(
                        "fixtures", {"league": league_id, "season": season}
                    ),
                    timeout=60,
                )
            except Exception as error:  # noqa: BLE001
                print(f"    {season}: failed — {type(error).__name__}", flush=True)
                continue
            for item in items:
                _parse(item, season_audit)
            competition.seasons.append(season_audit)
            print(
                f"    {season}: {season_audit.returned:>4} returned, "
                f"{len(season_audit.unique_ids):>4} unique, "
                f"{season_audit.usable_scores:>4} usable, "
                f"{season_audit.duplicates} dup, "
                f"{season_audit.out_of_season} out-of-season",
                flush=True,
            )
        results.append(competition)

        # Written now, not at the end. A reset after this point costs nothing.
        store[str(league_id)] = {
            "label": competition.label,
            "usable": competition.usable,
            "completed": competition.completed,
            "returned": competition.returned,
            "duplicates": competition.duplicates,
            "usable_seasons": competition.usable_seasons,
            "unfinished": competition.unfinished,
            "abandoned": competition.abandoned,
            "scheduled": competition.scheduled,
            "teams": competition.teams,
            "completeness": competition.completeness,
            "midweek": competition.midweek,
            "weekend": competition.weekend,
            "status": competition.status,
            "out_of_season": sum(x.out_of_season for x in competition.seasons),
            "missing_team_ids": sum(x.missing_team_ids for x in competition.seasons),
            "missing_scores": sum(x.missing_scores for x in competition.seasons),
        }
        _save(store)

    print("\n" + "=" * 104)
    print("HISTORY AUDIT — counted, not estimated")
    print("=" * 104)
    print(f"\n  Requests used: {provider.budget.used}")

    print("\n" + "-" * 104)
    print("AGGREGATE (ranked by usable history)")
    print("-" * 104)
    print(
        f"  {'competition':<32}{'returned':>9}{'usable':>8}{'unfin':>7}{'abnd':>6}"
        f"{'sched':>7}{'usable/ret':>11}{'teams':>7}{'Mon-Thu':>9}  status"
    )
    for competition in sorted(results, key=lambda c: -c.usable):
        print(
            f"  {competition.label[:31]:<32}{competition.returned:>9,}"
            f"{competition.usable:>8,}{competition.unfinished:>7,}"
            f"{competition.abandoned:>6,}{competition.scheduled:>7,}"
            f"{competition.completeness:>10.1%}{competition.teams:>7}"
            f"{competition.midweek:>9,}  {competition.status}"
        )

    print("\n" + "-" * 104)
    print("DATA INTEGRITY")
    print("-" * 104)
    total_duplicates = sum(c.duplicates for c in results)
    total_out = sum(s.out_of_season for c in results for s in c.seasons)
    total_missing_teams = sum(s.missing_team_ids for c in results for s in c.seasons)
    total_missing_scores = sum(s.missing_scores for c in results for s in c.seasons)
    print(f"  Duplicate fixture rows        : {total_duplicates}")
    print(f"  Fixtures outside their season : {total_out}")
    print(f"  Fixtures missing a team id    : {total_missing_teams}")
    print(f"  Completed without a score     : {total_missing_scores}")
    if total_duplicates == 0 and total_out == 0:
        print("\n  No duplication or season leakage. Fixture counts can be trusted")
        print("  as counts of distinct fixtures.")
    else:
        print("\n  Counts are affected. Supply rankings derived from raw fixture")
        print("  totals should be recomputed on unique ids before being used.")

    print("\n" + "=" * 104)
    print("READ-ONLY. Nothing ingested, activated or modelled.")
    print("This reaches HISTORY AVAILABLE. Team identity resolution and")
    print("chronological validation remain unmeasured, and no competition")
    print("advances further on the strength of these counts alone.")
    print("=" * 104)
    return 0


async def main() -> int:
    """Run the audit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--leagues",
        default="",
        help="Comma-separated provider league ids. Defaults to the queue's top ten.",
    )
    parser.add_argument("--seasons", type=int, default=8)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print everything counted so far without making any requests.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-count competitions already stored.",
    )
    args = parser.parse_args()

    if args.summary:
        return report_stored()

    if args.leagues:
        chosen: dict[int, str] = {}
        for raw in args.leagues.split(","):
            try:
                league_id = int(raw.strip())
            except ValueError:
                continue
            chosen[league_id] = DEFAULT_CANDIDATES.get(league_id, f"league {league_id}")
    else:
        chosen = DEFAULT_CANDIDATES

    return await audit(chosen, max(1, args.seasons), refresh=args.refresh)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
