#!/usr/bin/env python3
"""Create canonical team records for competitions that passed identity audit.

**The step between IDENTITY_READY and INGESTED.** Fixtures reference teams, so
a competition's history cannot be stored until its clubs exist as canonical
records. This creates them.

**It does not decide anything.** Every name goes through
``TeamResolver.resolve_or_create``, which creates a record only when it found
*nothing* plausible. An uncertain match is queued for review and no record is
made, because creating one would split a club's identity in two — which is the
failure that made Serie C and Ettan Norra risky in the first place. This
script cannot override that; it has no path to force a match.

**Every season, not the current one.** Seeding once ran against a single
season and produced 16 teams for Liga Leumit; its eight-season history contains
36, and ingestion then failed identity on 1,022 of 2,374 fixtures — 43% of the
competition. Clubs relegated or promoted out of a division still played the
matches a model needs to learn from, so the squad list has to span the same
seasons the history does.

**Dry run by default.** Nothing is written without ``--apply``, and the dry run
reports exactly what would be created so the list can be read before it exists.

**Competitions that failed identity audit are refused.** Serie C Girone A and
Ettan Norra carry ambiguous names — Triestina scoring against Fiorentina,
Enköping against Norrköping — and seeding them now would create records beside
clubs they might actually be. They need those names decided by a person first.

Run::

    railway ssh "python scripts/seed_teams.py"
    railway ssh "python scripts/seed_teams.py --apply --leagues 382,291"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.infrastructure.database import Database
from app.services.team_resolution import TeamResolver

# Competitions that passed identity audit with zero ambiguous names.
READY: dict[int, tuple[str, str]] = {
    382: ("Liga Leumit (Israel)", "Israel"),
    291: ("Azadegan League (Iran)", "Iran"),
    287: ("Prva Liga (Serbia)", "Serbia"),
    240: ("Primera B (Colombia)", "Colombia"),
    496: ("Liga Alef (Israel)", "Israel"),
    399: ("NPFL (Nigeria)", "Nigeria"),
    317: ("1st League - RS (Bosnia)", "Bosnia"),
    243: ("Liga Pro Serie B (Ecuador)", "Ecuador"),
}

# Held back deliberately. Their ambiguous names must be decided by a person
# before any record is created, and this script will not seed them even when
# named explicitly.
WITHHELD: dict[int, str] = {
    138: "Serie C - Girone A (Italy) — 8 of 20 teams ambiguous against Serie A clubs",
    563: "Ettan - Norra (Sweden) — 5 of 16 teams ambiguous against Allsvenskan clubs",
}


@dataclass
class SeedResult:
    """What happened for one competition."""

    league_id: int
    label: str
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.existing) + len(self.queued) + len(self.failed)


async def seed(
    session: Any,
    provider: Any,
    league_id: int,
    label: str,
    country: str,
    seasons: list[int],
    apply: bool,
) -> SeedResult:
    """Seed one competition's teams across every season it will be ingested for.

    The union of all seasons' squads, not the current one. Seeding a single
    season produced 16 teams for Liga Leumit against 36 in its eight-season
    history, and ingestion then lost 1,022 of 2,374 fixtures to identity
    failure. A club relegated out of a division still played the matches a
    model learns from.
    """
    result = SeedResult(league_id=league_id, label=label)
    resolver = TeamResolver(session)

    collected: dict[str, dict[str, Any]] = {}
    for season in seasons:
        try:
            season_items = await asyncio.wait_for(
                provider._get("teams", {"league": league_id, "season": season}),
                timeout=60,
            )
        except Exception as error:  # noqa: BLE001
            result.failed.append(f"season {season}: {type(error).__name__}")
            print(f"    {season}: request failed — {type(error).__name__}", flush=True)
            continue
        added = 0
        for entry in season_items:
            team_block = entry.get("team") or {}
            key = str(team_block.get("id") or team_block.get("name") or "")
            if key and key not in collected:
                collected[key] = entry
                added += 1
        print(f"    {season}: {len(season_items)} teams, {added} new", flush=True)

    items = list(collected.values())

    for item in items:
        team = item.get("team") or {}
        name = str(team.get("name") or "").strip()
        if not name:
            continue
        external_id = str(team.get("id") or "") or None

        if not apply:
            # Dry run: ask what would happen without the create path.
            preview = await resolver.resolve(
                provider_name="api_football",
                raw_name=name,
                sport="football",
                country=country,
                external_team_id=external_id,
                learn=False,
            )
            if preview.is_resolved:
                result.existing.append(name)
            elif preview.needs_review or preview.ambiguous:
                result.queued.append(name)
            else:
                result.created.append(name)
            continue

        try:
            resolution, created = await resolver.resolve_or_create(
                provider_name="api_football",
                raw_name=name,
                sport="football",
                country=country,
                external_team_id=external_id,
            )
        except Exception as error:  # noqa: BLE001
            result.failed.append(f"{name}: {type(error).__name__}")
            continue

        if created:
            result.created.append(name)
        elif resolution.is_resolved:
            result.existing.append(name)
        else:
            # Queued for review by the resolver. No record was made.
            result.queued.append(name)

    return result


async def main() -> int:
    """Seed canonical records."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leagues", default="")
    parser.add_argument(
        "--seasons",
        type=int,
        default=8,
        help="How many seasons back to collect squads from. Must cover the "
        "seasons ingestion will store, or clubs from earlier ones resolve to "
        "nothing and their fixtures are lost to identity failure.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the records. Without this nothing is created.",
    )
    args = parser.parse_args()

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

    chosen = dict(READY)
    if args.leagues:
        wanted = {int(x.strip()) for x in args.leagues.split(",") if x.strip().isdigit()}
        refused = wanted & set(WITHHELD)
        for league_id in sorted(refused):
            print(f"  REFUSED {WITHHELD[league_id]}")
        chosen = {k: v for k, v in READY.items() if k in wanted}
        if not chosen:
            print("\n  Nothing to seed. Named competitions are withheld or unknown.")
            return 1

    from app.providers.api_football import ApiFootballProvider

    provider = ApiFootballProvider(api_key=key)
    current = datetime.now().year
    seasons = [current - 1 - offset for offset in range(args.seasons)]
    database = Database(get_settings())
    await database.connect()

    results: list[SeedResult] = []
    async with database.session() as session:
        for league_id, (label, country) in chosen.items():
            print(f"\n  {label} (id {league_id}, {len(seasons)} seasons)", flush=True)
            outcome = await seed(
                session, provider, league_id, label, country, seasons, args.apply
            )
            results.append(outcome)
            print(
                f"    {outcome.total} teams: {len(outcome.created)} "
                f"{'created' if args.apply else 'would create'}, "
                f"{len(outcome.existing)} already known, "
                f"{len(outcome.queued)} queued for review, "
                f"{len(outcome.failed)} failed",
                flush=True,
            )

        if args.apply:
            await session.commit()
        else:
            await session.rollback()

    await database.disconnect()

    print("\n" + "=" * 96)
    print("TEAM SEEDING — " + ("APPLIED" if args.apply else "DRY RUN"))
    print("=" * 96)
    print(f"\n  Requests used: {provider.budget.used}")
    print(
        f"\n  {'competition':<34}{'teams':>7}{'created':>9}{'known':>7}"
        f"{'queued':>8}{'failed':>8}"
    )
    for outcome in results:
        print(
            f"  {outcome.label[:33]:<34}{outcome.total:>7}{len(outcome.created):>9}"
            f"{len(outcome.existing):>7}{len(outcome.queued):>8}{len(outcome.failed):>8}"
        )

    total_created = sum(len(r.created) for r in results)
    total_queued = sum(len(r.queued) for r in results)
    print(f"\n  Total {'created' if args.apply else 'to create'}: {total_created}")
    if total_queued:
        print(f"  Queued for review (no record made): {total_queued}")

    for outcome in results:
        if outcome.queued or outcome.failed:
            print(f"\n  {outcome.label}")
            for name in outcome.queued:
                print(f"    queued: {name}")
            for name in outcome.failed:
                print(f"    failed: {name}")

    print("\n" + "=" * 96)
    if args.apply:
        print("APPLIED. Canonical records created. This reaches INGESTED only for")
        print("team identity — no fixture history has been stored, no model has")
        print("been fitted, and no competition is ACTIVE.")
    else:
        print("DRY RUN. Nothing was written; the transaction was rolled back.")
        print("Re-run with --apply once the list above has been read.")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
