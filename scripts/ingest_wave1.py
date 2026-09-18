#!/usr/bin/env python3
"""Ingest Wave 1 historical fixtures, and account for every one.

**Two destinations, one pass.** Every fixture the provider returns is written
to ``provider_fixtures`` with its status. Those that can train a model are also
written to ``historical_matches``, which is the evidence base the modelling
path reads and whose goals are ``NOT NULL`` by design. A fixture that cannot
train is still stored, with the reason — so a data gap is visible rather than
looking like an ingestion failure.

**Idempotent on the provider's own key.** Keyed on
``(provider_name, provider_fixture_id)``. A second run updates rows and inserts
none. A fixture whose status legitimately changes, postponed then played, is
the same fixture and must not become two.

**Reconciliation is the point, not a by-product.** The report requires

    returned = stored, and stored = trainable + explained exclusions

and says so explicitly when it does not hold. Nothing may go missing between
the provider and the database without a stated reason.

**It does not activate anything.** Ingestion moves a competition to INGESTED.
Model fitting and chronological validation are separate gates, and no
competition enters the daily scan on the strength of having history stored.

Run::

    railway ssh "python scripts/ingest_wave1.py --dry-run"
    railway ssh "python scripts/ingest_wave1.py --leagues 382,291"
    railway ssh "python scripts/ingest_wave1.py --summary"
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.competitions import code_for_api_id
from app.core.config import get_settings
from app.core.fixture_status import (
    classify_status,
    exclusion_reason,
    is_training_eligible,
)
from app.database.models import HistoricalMatch, ProviderFixture
from app.database.models.team import Competition as CompetitionRow
from app.infrastructure.database import Database
from app.services.team_names import normalize_team_name
from app.services.team_resolution import TeamResolver

PROVIDER = "api_football"
DATA_VERSION = "api-football-wave1-v1"
RESULTS_PATH = Path("/tmp/quantsport_wave1_ingest.json")

WAVE_1: dict[int, tuple[str, str]] = {
    382: ("Liga Leumit", "Israel"),
    291: ("Azadegan League", "Iran"),
    287: ("Prva Liga", "Serbia"),
    240: ("Primera B", "Colombia"),
    496: ("Liga Alef", "Israel"),
    399: ("NPFL", "Nigeria"),
    317: ("1st League - RS", "Bosnia"),
    243: ("Liga Pro Serie B", "Ecuador"),
}

EXPECTED_TEAMS: dict[int, int] = {
    382: 36, 291: 57, 287: 48, 240: 23,
    496: 76, 399: 34, 317: 30, 243: 29,
}
"""Distinct teams each competition's history contains, from the history audit.

Compared against what actually resolves. Seeding once covered a single season
and left 43% of Liga Leumit's fixtures unresolvable; the two scripts disagreed
about what "the teams in this competition" meant and nothing checked them
against each other. This is that check.
"""

IDENTITY_FAILURE_LIMIT = 0.05
"""Share of fixtures that may fail identity before ingestion is not worth doing.

Above this the competition would train on a biased subset — every club that
left the division missing — which is worse than not training on it at all.
"""

WITHHELD: dict[int, str] = {
    138: "Serie C - Girone A — identity review outstanding (8 of 20 ambiguous)",
    563: "Ettan - Norra — identity review outstanding (5 of 16 ambiguous)",
}


@dataclass
class Preflight:
    """Identity coverage for a competition's whole historical universe.

    Run before any fixture is stored. Two orderings have already produced a
    silently truncated dataset: seeding one season then ingesting eight, and
    ingesting before seeding at all. Both left a competition looking ingested
    while 43% and 69% of its fixtures had no team to attach to — and a model
    fitted on that trains only on the clubs that happened to still be in the
    division.

    The remedy is not to remember the order. It is for ingestion to establish
    its own prerequisite.
    """

    league_id: int
    provider_teams: int = 0
    already_known: int = 0
    seeded: int = 0
    unresolvable: list[tuple[str, str, float]] = field(default_factory=list)
    seasons_checked: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def covered(self) -> int:
        return self.already_known + self.seeded

    @property
    def coverage(self) -> float:
        if not self.provider_teams:
            return 0.0
        return self.covered / self.provider_teams

    @property
    def passes(self) -> bool:
        """Whether ingestion may proceed to a training-ready state.

        Every team the provider lists across every ingested season must
        resolve. A single unresolvable club means some fixtures cannot be
        attached, and a partial training set is worse than none: the gap is
        invisible once the numbers look plausible.
        """
        return bool(self.provider_teams) and not self.unresolvable and self.coverage >= 1.0


async def run_preflight(
    session: AsyncSession,
    provider: Any,
    league_id: int,
    country: str,
    seasons: list[int],
    apply: bool,
) -> Preflight:
    """Establish identity coverage, seeding what is safely seedable.

    Uses the same resolver safeguards as seeding: an uncertain match is never
    forced, and a fuzzy match against another team from the same provider is
    refused outright. Anything that cannot be resolved *or* safely created is
    reported by name rather than skipped.
    """
    report = Preflight(league_id=league_id)
    resolver = TeamResolver(session)

    collected: dict[str, dict[str, Any]] = {}
    for season in seasons:
        try:
            items = await asyncio.wait_for(
                provider._get(
                    "teams", {"league": league_id, "season": season}
                ),
                timeout=60,
            )
        except Exception as error:  # noqa: BLE001
            report.errors.append(f"season {season}: {type(error).__name__}")
            continue
        report.seasons_checked.append(season)
        for entry in items:
            team = entry.get("team") or {}
            key = str(team.get("id") or team.get("name") or "")
            if key and key not in collected:
                collected[key] = entry

    report.provider_teams = len(collected)

    for entry in collected.values():
        team = entry.get("team") or {}
        name = str(team.get("name") or "").strip()
        if not name:
            continue
        external_id = str(team.get("id") or "") or None

        existing = await resolver.resolve(
            provider_name=PROVIDER,
            raw_name=name,
            sport="football",
            country=country,
            external_team_id=external_id,
            learn=False,
        )
        if existing.is_resolved:
            report.already_known += 1
            continue

        if not apply:
            # Dry run: an unknown team would be created, not blocked.
            report.seeded += 1
            continue

        try:
            resolution, created = await resolver.resolve_or_create(
                provider_name=PROVIDER,
                raw_name=name,
                sport="football",
                country=country,
                external_team_id=external_id,
            )
        except Exception as error:  # noqa: BLE001
            report.unresolvable.append((name, f"error: {type(error).__name__}", 0.0))
            continue

        if created or resolution.is_resolved:
            report.seeded += 1
            continue

        # Queued for review by the resolver. No record was made, and forcing
        # one is the thing these safeguards exist to prevent.
        candidate = getattr(resolution.candidate, "canonical_name", "no candidate")
        report.unresolvable.append((name, str(candidate), float(resolution.confidence)))

    return report


@dataclass
class Reconciliation:
    """Every fixture accounted for, per competition."""

    league_id: int
    label: str
    returned: int = 0
    stored_new: int = 0
    stored_updated: int = 0
    trainable: int = 0
    historical_new: int = 0
    historical_existing: int = 0
    identity_failures: int = 0
    historical_total: int = 0
    """Rows actually present in ``historical_matches`` for this competition.

    Not ``historical_new``, which counts only what this run inserted. On a
    second pass that is correctly zero, and treating it as the invariant would
    make a clean idempotency proof look like a total failure. What must equal
    ``trainable`` is the total present.
    """

    historical_duplicates: int = 0
    excluded: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    seasons: set[str] = field(default_factory=set)
    teams: set[int] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    @property
    def stored(self) -> int:
        return self.stored_new + self.stored_updated

    @property
    def excluded_total(self) -> int:
        return sum(self.excluded.values())

    @property
    def balances(self) -> bool:
        """Whether nothing vanished between provider and database."""
        return (
            self.returned == self.stored
            and self.stored == self.trainable + self.excluded_total
        )

    @property
    def training_complete(self) -> bool:
        """Whether every trainable fixture is actually in the evidence base.

        Measured against rows present, not rows inserted by this run, so a
        second pass inserting nothing still passes.
        """
        return (
            self.historical_total == self.trainable
            and self.identity_failures == 0
            and self.historical_duplicates == 0
        )


def _hash(payload: dict[str, Any]) -> str:
    """Stable hash of the stored fields, so a provider revision is detectable."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:64]


async def _competition_row(
    session: AsyncSession, code: str | None, name: str, country: str
) -> CompetitionRow | None:
    """Return the competition row, creating it if this is its first ingestion.

    Mirrors ``HistoricalIngestionService._get_or_create_competition`` rather
    than reimplementing it. Two details matter and a first attempt here got
    both wrong: ``normalized_name`` is ``NOT NULL``, and the unique constraint
    is on ``(sport, country, normalized_name)`` — so a lookup by canonical
    name alone can miss an existing row and then fail to insert a duplicate.
    """
    normalized = normalize_team_name(name)

    found = await session.execute(
        select(CompetitionRow).where(
            CompetitionRow.sport == "football",
            CompetitionRow.normalized_name == normalized,
            CompetitionRow.country == country,
        )
    )
    row = found.scalar_one_or_none()
    if row is not None:
        row.has_historical_coverage = True
        return row

    row = CompetitionRow(
        canonical_name=name,
        normalized_name=normalized,
        country=country,
        sport="football",
        has_historical_coverage=True,
        external_metadata={"source_external_id": str(code or "")},
    )
    session.add(row)
    await session.flush()
    return row


async def ingest_competition(
    session: AsyncSession,
    provider: Any,
    league_id: int,
    label: str,
    country: str,
    seasons: list[int],
    dry_run: bool,
) -> Reconciliation:
    """Ingest one competition's history."""
    code = code_for_api_id(league_id)
    report = Reconciliation(league_id=league_id, label=f"{label} ({country})")
    resolver = TeamResolver(session)
    competition = (
        None if dry_run else await _competition_row(session, code, label, country)
    )

    for season in seasons:
        try:
            items = await asyncio.wait_for(
                provider._get(
                    "fixtures", {"league": league_id, "season": season}
                ),
                timeout=60,
            )
        except Exception as error:  # noqa: BLE001
            report.errors.append(f"{season}: {type(error).__name__}")
            print(f"    {season}: request failed — {type(error).__name__}", flush=True)
            continue

        season_trainable = 0
        for item in items:
            fixture = item.get("fixture") or {}
            league = item.get("league") or {}
            teams = item.get("teams") or {}
            goals = item.get("goals") or {}

            fixture_id = str(fixture.get("id") or "")
            if not fixture_id:
                continue
            report.returned += 1
            report.seasons.add(str(season))

            status_code = str((fixture.get("status") or {}).get("short") or "")
            home_goals = goals.get("home")
            away_goals = goals.get("away")
            eligible = is_training_eligible(status_code, home_goals, away_goals)
            reason = exclusion_reason(status_code, home_goals, away_goals)
            if not eligible and reason:
                report.excluded[reason] += 1
            if eligible:
                report.trainable += 1
                season_trainable += 1

            raw_date = str(fixture.get("date") or "")
            try:
                kickoff = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                report.excluded["unparseable kickoff"] += 1
                continue

            home_name = str((teams.get("home") or {}).get("name") or "").strip()
            away_name = str((teams.get("away") or {}).get("name") or "").strip()
            if not home_name or not away_name:
                report.excluded["missing team name"] += 1
                continue

            if dry_run:
                continue

            # Resolve identities. learn=False: aliases were settled during
            # seeding, and ingestion must not quietly create new mappings.
            home = await resolver.resolve(
                provider_name=PROVIDER,
                raw_name=home_name,
                sport="football",
                country=country,
                external_team_id=str((teams.get("home") or {}).get("id") or "") or None,
                learn=False,
            )
            away = await resolver.resolve(
                provider_name=PROVIDER,
                raw_name=away_name,
                sport="football",
                country=country,
                external_team_id=str((teams.get("away") or {}).get("id") or "") or None,
                learn=False,
            )
            home_id = home.team.id if home.team is not None else None
            away_id = away.team.id if away.team is not None else None
            if home_id is None or away_id is None:
                report.identity_failures += 1
            else:
                report.teams.update({home_id, away_id})

            payload = {
                "league": league.get("id"),
                "season": season,
                "status": status_code,
                "home": home_goals,
                "away": away_goals,
                "kickoff": raw_date,
            }

            found = await session.execute(
                select(ProviderFixture).where(
                    ProviderFixture.provider_name == PROVIDER,
                    ProviderFixture.provider_fixture_id == fixture_id,
                )
            )
            row = found.scalar_one_or_none()
            if row is None:
                row = ProviderFixture(
                    provider_name=PROVIDER,
                    provider_fixture_id=fixture_id,
                    provider_league_id=league_id,
                    season=str(season),
                    kickoff=kickoff,
                    home_team_name=home_name,
                    away_team_name=away_name,
                    ingested_at=datetime.now(UTC),
                    training_eligible=False,
                )
                session.add(row)
                report.stored_new += 1
            else:
                report.stored_updated += 1

            row.competition_code = code
            row.provider_home_team_id = str((teams.get("home") or {}).get("id") or "") or None
            row.provider_away_team_id = str((teams.get("away") or {}).get("id") or "") or None
            row.home_team_id = home_id
            row.away_team_id = away_id
            row.status_code = status_code or None
            row.status_class = classify_status(status_code).value
            row.home_goals = home_goals
            row.away_goals = away_goals
            row.training_eligible = bool(eligible and home_id and away_id)
            row.exclusion_reason = (
                reason
                if reason
                else ("identity unresolved" if not (home_id and away_id) else None)
            )
            row.provider_payload_hash = _hash(payload)

            # Trainable fixtures also join the evidence base. Its goals are
            # NOT NULL, so only fully resolved, scored fixtures go there.
            if row.training_eligible and home_id and away_id:
                existing = await session.execute(
                    select(HistoricalMatch).where(
                        HistoricalMatch.provider_name == PROVIDER,
                        HistoricalMatch.provider_match_id == fixture_id,
                    )
                )
                if existing.scalar_one_or_none() is None:
                    session.add(
                        HistoricalMatch(
                            provider_name=PROVIDER,
                            provider_match_id=fixture_id,
                            home_team_id=home_id,
                            away_team_id=away_id,
                            competition_id=competition.id if competition else None,
                            season=str(season),
                            match_date=kickoff.date(),
                            home_goals=int(str(home_goals)),
                            away_goals=int(str(away_goals)),
                            data_version=DATA_VERSION,
                            ingested_at=datetime.now(UTC),
                        )
                    )
                    report.historical_new += 1
                else:
                    report.historical_existing += 1

        if not dry_run:
            await session.flush()
        print(
            f"    {season}: {len(items)} returned, {season_trainable} trainable",
            flush=True,
        )

    # Count what is actually present, and check for duplicates on the provider
    # key. These are the figures the invariant is stated against.
    if not dry_run:
        rows = await session.execute(
            select(HistoricalMatch.provider_match_id)
            .where(HistoricalMatch.provider_name == PROVIDER)
            .where(
                HistoricalMatch.provider_match_id.in_(
                    select(ProviderFixture.provider_fixture_id).where(
                        ProviderFixture.provider_league_id == league_id
                    )
                )
            )
        )
        ids = [row[0] for row in rows.all()]
        report.historical_total = len(set(ids))
        report.historical_duplicates = len(ids) - len(set(ids))

    return report


def _persist(reports: list[Reconciliation]) -> None:
    """Keep results across sessions; SSH drops mid-run."""
    try:
        store: dict[str, Any] = {}
        if RESULTS_PATH.exists():
            store = dict(json.loads(RESULTS_PATH.read_text(encoding="utf-8")))
        for report in reports:
            store[str(report.league_id)] = {
                "label": report.label,
                "returned": report.returned,
                "stored": report.stored,
                "stored_new": report.stored_new,
                "trainable": report.trainable,
                "historical_new": report.historical_new,
                "historical_existing": report.historical_existing,
                "historical_total": report.historical_total,
                "historical_duplicates": report.historical_duplicates,
                "training_complete": report.training_complete,
                "identity_failures": report.identity_failures,
                "excluded": dict(report.excluded),
                "seasons": sorted(report.seasons),
                "teams": len(report.teams),
                "balances": report.balances,
                "errors": report.errors,
            }
        RESULTS_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")
    except OSError as error:
        print(f"  (could not persist: {error})", flush=True)


def report_summary() -> int:
    """Print every competition ingested so far."""
    try:
        store = dict(json.loads(RESULTS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        print("No ingestion results stored yet.")
        return 1

    print("=" * 104)
    print(f"WAVE 1 INGESTION — {len(store)} competitions")
    print("=" * 104)
    print(
        f"\n  {'competition':<30}{'returned':>9}{'stored':>8}{'trainable':>11}"
        f"{'hist total':>11}{'dup':>5}{'excluded':>10}{'ident':>7}  bal  training"
    )
    complete = 0
    for row in sorted(store.values(), key=lambda r: -int(r.get("returned", 0))):
        excluded = sum(int(v) for v in dict(row.get("excluded", {})).values())
        if row.get("training_complete"):
            complete += 1
        print(
            f"  {str(row.get('label'))[:29]:<30}{int(row.get('returned', 0)):>9,}"
            f"{int(row.get('stored', 0)):>8,}{int(row.get('trainable', 0)):>11,}"
            f"{int(row.get('historical_total', 0)):>11,}"
            f"{int(row.get('historical_duplicates', 0)):>5}{excluded:>10,}"
            f"{int(row.get('identity_failures', 0)):>7}"
            f"  {'YES' if row.get('balances') else 'NO ':<3}"
            f"  {'COMPLETE' if row.get('training_complete') else 'INCOMPLETE'}"
        )

    print("\n" + "-" * 104)
    print("EXCLUSIONS BY REASON")
    print("-" * 104)
    totals: dict[str, int] = defaultdict(int)
    for row in store.values():
        for reason, count in dict(row.get("excluded", {})).items():
            totals[reason] += int(count)
    for reason, count in sorted(totals.items(), key=lambda item: -item[1]):
        print(f"  {reason:<60}{count:>8,}")
    if not totals:
        print("  None.")

    returned = sum(int(r.get("returned", 0)) for r in store.values())
    stored = sum(int(r.get("stored", 0)) for r in store.values())
    trainable = sum(int(r.get("trainable", 0)) for r in store.values())
    excluded = sum(totals.values())

    print("\n" + "-" * 104)
    print("RECONCILIATION")
    print("-" * 104)
    print(f"  provider returned : {returned:,}")
    print(f"  stored            : {stored:,}")
    print(f"  trainable         : {trainable:,}")
    print(f"  excluded          : {excluded:,}")
    historical_total = sum(int(r.get("historical_total", 0)) for r in store.values())
    duplicates = sum(int(r.get("historical_duplicates", 0)) for r in store.values())
    identity = sum(int(r.get("identity_failures", 0)) for r in store.values())
    print(f"  historical rows   : {historical_total:,}")
    print(f"  duplicate rows    : {duplicates}")
    print(f"  identity failures : {identity}")

    if returned == stored and stored == trainable + excluded:
        print("\n  BALANCED. Every returned fixture is stored, and every stored")
        print("  fixture is either trainable or excluded for a stated reason.")
        if historical_total == trainable and not duplicates and not identity:
            print(f"\n  TRAINING COMPLETE in {complete} of {len(store)} competitions.")
            print("  Every trainable fixture is present in historical_matches, with")
            print("  no duplicates and no unresolved identities.")
        else:
            print(f"\n  TRAINING INCOMPLETE. {trainable - historical_total:,} trainable "
                  "fixtures are not in historical_matches.")
            print("  Re-run the affected competitions; ingestion is idempotent.")
    else:
        print("\n  DOES NOT BALANCE. Fixtures are unaccounted for between the")
        print(f"  provider and the database: {returned - stored} lost in storage, "
              f"{stored - trainable - excluded} unexplained.")

    print("\n" + "=" * 104)
    print("State: INGESTED. Not ACTIVE. Model fitting and chronological")
    print("validation remain, and no competition enters the daily scan on the")
    print("strength of having history stored.")
    print("=" * 104)
    return 0


async def main() -> int:
    """Run ingestion."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leagues", default="")
    parser.add_argument("--seasons", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    if args.summary:
        return report_summary()

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

    chosen = dict(WAVE_1)
    if args.leagues:
        wanted = {int(x.strip()) for x in args.leagues.split(",") if x.strip().isdigit()}
        for league_id in sorted(wanted & set(WITHHELD)):
            print(f"  REFUSED {WITHHELD[league_id]}")
        chosen = {k: v for k, v in WAVE_1.items() if k in wanted}
        if not chosen:
            print("\n  Nothing to ingest.")
            return 1

    from app.providers.api_football import ApiFootballProvider

    provider = ApiFootballProvider(api_key=key)
    current = datetime.now().year
    seasons = [current - 1 - offset for offset in range(args.seasons)]

    database = Database(get_settings())
    await database.connect()

    reports: list[Reconciliation] = []
    blocked: list[tuple[str, Preflight]] = []
    async with database.session() as session:
        for league_id, (label, country) in chosen.items():
            print(f"\n  {label} ({country}) — id {league_id}", flush=True)

            # Preflight. Ingestion establishes its own prerequisite rather than
            # relying on a seeding script having been run first in the right
            # order with the right season range.
            print("    preflight: checking historical identity coverage ...", flush=True)
            pre = await run_preflight(
                session, provider, league_id, country, seasons, not args.dry_run
            )
            print(
                f"    preflight: {pre.provider_teams} teams across "
                f"{len(pre.seasons_checked)} seasons — "
                f"{pre.already_known} known, {pre.seeded} seeded, "
                f"{len(pre.unresolvable)} unresolvable",
                flush=True,
            )
            if not args.dry_run:
                await session.commit()

            if not pre.passes:
                blocked.append((label, pre))
                print(
                    f"    BLOCKED: identity coverage {pre.coverage:.0%}. "
                    "Ingestion would produce a silently truncated training set.",
                    flush=True,
                )
                for name, candidate, confidence in pre.unresolvable[:15]:
                    print(f"      unresolvable: {name}  ->  {candidate} ({confidence:.2f})")
                for error in pre.errors:
                    print(f"      preflight error: {error}")
                continue

            report = await ingest_competition(
                session, provider, league_id, label, country, seasons, args.dry_run
            )
            reports.append(report)

            # Refuse to leave a competition looking ingested when a large
            # share of its history could not be attached to a team.
            expected = EXPECTED_TEAMS.get(league_id)
            share = (
                report.identity_failures / report.returned if report.returned else 0.0
            )
            if share > IDENTITY_FAILURE_LIMIT:
                print(
                    f"    *** IDENTITY GAP: {report.identity_failures:,} of "
                    f"{report.returned:,} fixtures ({share:.0%}) could not be "
                    "attached to a team.",
                    flush=True,
                )
                print(
                    f"    *** {len(report.teams)} teams resolved; the history audit "
                    f"found {expected if expected else 'more'}.",
                    flush=True,
                )
                print(
                    "    *** Seed across every ingested season first:",
                    flush=True,
                )
                print(
                    f"    ***   python scripts/seed_teams.py --apply "
                    f"--leagues {league_id} --seasons {args.seasons}",
                    flush=True,
                )
                print(
                    "    *** Then re-run this; ingestion is idempotent and will "
                    "backfill what newly resolves.",
                    flush=True,
                )
            print(
                f"    {report.returned} returned, {report.stored} stored, "
                f"{report.trainable} trainable, {report.excluded_total} excluded, "
                f"{report.identity_failures} identity failures"
                f"  -> {'balances' if report.balances else 'DOES NOT BALANCE'}",
                flush=True,
            )
            if not args.dry_run:
                await session.commit()
                _persist([report])

        if args.dry_run:
            await session.rollback()

    await database.disconnect()

    print("\n" + "=" * 104)
    print("WAVE 1 INGESTION — " + ("DRY RUN" if args.dry_run else "APPLIED"))
    print("=" * 104)
    print(f"\n  Requests used: {provider.budget.used}")
    for report in reports:
        print(
            f"\n  {report.label}"
            f"\n    returned {report.returned:,}  stored {report.stored:,}"
            f"  (new {report.stored_new:,}, updated {report.stored_updated:,})"
            f"\n    trainable {report.trainable:,}  historical new {report.historical_new:,}"
            f"  already present {report.historical_existing:,}"
            f"\n    seasons {len(report.seasons)}  teams {len(report.teams)}"
            f"  identity failures {report.identity_failures}"
        )
        for reason, count in sorted(report.excluded.items(), key=lambda i: -i[1]):
            print(f"      excluded {count:>5,}  {reason}")
        if not report.balances:
            print("      *** DOES NOT BALANCE ***")
        for error in report.errors:
            print(f"      error: {error}")

    if blocked:
        print("\n" + "-" * 104)
        print("BLOCKED — REVIEW REQUIRED")
        print("-" * 104)
        for label, pre in blocked:
            print(f"\n  {label}: identity coverage {pre.coverage:.0%}")
            print(f"    {pre.provider_teams} provider teams, {pre.covered} resolved")
            for name, candidate, confidence in pre.unresolvable:
                print(f"    {name}  ->  {candidate} ({confidence:.2f})")
        print("\n  These competitions were not ingested. A team that cannot be")
        print("  safely identified means fixtures with no team to attach to, and")
        print("  a model fitted on the remainder would train only on the clubs")
        print("  that happened to resolve.")

    if args.dry_run:
        print("\n  DRY RUN. Nothing written; transaction rolled back.")
    else:
        print("\n  APPLIED. State: INGESTED, not ACTIVE.")
    print("=" * 104)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
