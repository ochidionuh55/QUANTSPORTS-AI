#!/usr/bin/env python3
"""Can a competition's teams be identified? Measured, not assumed.

**The gate between HISTORY AVAILABLE and INGESTED.** A competition with eight
clean seasons is worthless if its teams cannot be tied to canonical records:
the scan already loses 35 fixtures a day to unresolved identities inside the
existing universe, and adding competitions that resolve badly would enlarge
that loss rather than the candidate pool.

**A new league is allowed to bring new teams.** Resolution rate alone would
reject every competition we have never seen, which is exactly backwards. What
matters is the split between three outcomes:

* **resolved** — matches a canonical team we already hold.
* **seedable** — genuinely new, with a stable provider id and no rival
  candidate. Safe to create as a canonical record.
* **ambiguous** — a candidate was found but not trusted, or two candidates
  scored within the margin. These must stay unresolved and reviewable; forcing
  them is how one club's history silently becomes another's.

A competition is feasible when it has few ambiguous teams, not when it has
many already-known ones.

**Nothing is written.** The resolver is called with ``learn=False`` throughout,
so no alias is persisted and no canonical record created. This measures; a
later step seeds.

Run::

    railway ssh "python scripts/identity_audit.py"
    railway ssh "python scripts/identity_audit.py --leagues 382,138 --season 2025"
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

from app.core.config import get_settings
from app.infrastructure.database import Database
from app.services.team_resolution import TeamResolver

RESULTS_PATH = Path("/tmp/quantsport_identity_audit.json")

CANDIDATES: dict[int, tuple[str, str]] = {
    496: ("Liga Alef (Israel)", "Israel"),
    138: ("Serie C - Girone A (Italy)", "Italy"),
    399: ("NPFL (Nigeria)", "Nigeria"),
    382: ("Liga Leumit (Israel)", "Israel"),
    291: ("Azadegan League (Iran)", "Iran"),
    287: ("Prva Liga (Serbia)", "Serbia"),
    240: ("Primera B (Colombia)", "Colombia"),
    563: ("Ettan - Norra (Sweden)", "Sweden"),
    317: ("1st League - RS (Bosnia)", "Bosnia"),
    243: ("Liga Pro Serie B (Ecuador)", "Ecuador"),
}


@dataclass
class TeamOutcome:
    """What happened when one team name was resolved."""

    name: str
    external_id: str
    resolved: bool
    seedable: bool
    ambiguous: bool
    needs_review: bool
    confidence: float
    method: str
    candidate: str | None = None


@dataclass
class CompetitionIdentity:
    """Identity feasibility for one competition."""

    league_id: int
    label: str
    outcomes: list[TeamOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def resolved(self) -> int:
        return sum(1 for o in self.outcomes if o.resolved)

    @property
    def seedable(self) -> int:
        return sum(1 for o in self.outcomes if o.seedable)

    @property
    def ambiguous(self) -> int:
        return sum(1 for o in self.outcomes if o.ambiguous or o.needs_review)

    def share(self, count: int) -> float:
        return count / self.total if self.total else 0.0

    @property
    def status(self) -> str:
        """Feasibility from the three-way split.

        Ambiguity is what disqualifies, not novelty. A competition of entirely
        new clubs with stable provider ids is straightforward to seed; one with
        a handful of near-matches to existing clubs is the dangerous case.
        """
        if not self.total:
            return "NO_TEAMS"
        ambiguous_share = self.share(self.ambiguous)
        if ambiguous_share > 0.15:
            return "IDENTITY_RISKY"
        if ambiguous_share > 0.05:
            return "IDENTITY_REVIEW_NEEDED"
        return "IDENTITY_READY"


async def audit_competition(
    session: Any, provider: Any, league_id: int, label: str, country: str, season: int
) -> CompetitionIdentity:
    """Resolve every team appearing in one season, writing nothing."""
    identity = CompetitionIdentity(league_id=league_id, label=label)
    resolver = TeamResolver(session)

    try:
        items = await asyncio.wait_for(
            provider._get("teams", {"league": league_id, "season": season}),
            timeout=60,
        )
    except Exception as error:  # noqa: BLE001
        print(f"    teams request failed — {type(error).__name__}: {error}", flush=True)
        return identity

    for item in items:
        team = item.get("team") or {}
        name = str(team.get("name") or "").strip()
        if not name:
            continue
        external_id = str(team.get("id") or "")

        result = await resolver.resolve(
            provider_name="api_football",
            raw_name=name,
            sport="football",
            country=country,
            external_team_id=external_id or None,
            # Measuring only. A confident match must not persist an alias here,
            # because a mistaken one would be hard to find later.
            learn=False,
        )

        resolved = result.is_resolved
        ambiguous = bool(result.ambiguous)
        needs_review = bool(result.needs_review)
        # Genuinely new: nothing matched, nothing close enough to review, and
        # the provider gives a stable id to anchor the new record to.
        seedable = (
            not resolved and not ambiguous and not needs_review and bool(external_id)
        )

        identity.outcomes.append(
            TeamOutcome(
                name=name,
                external_id=external_id,
                resolved=resolved,
                seedable=seedable,
                ambiguous=ambiguous,
                needs_review=needs_review,
                confidence=float(result.confidence),
                method=str(getattr(result.method, "value", result.method)),
                candidate=getattr(result.candidate, "canonical_name", None),
            )
        )

    return identity


def _save(results: list[CompetitionIdentity]) -> None:
    """Persist so a dropped session does not lose counted work."""
    try:
        existing: dict[str, Any] = {}
        if RESULTS_PATH.exists():
            existing = dict(json.loads(RESULTS_PATH.read_text(encoding="utf-8")))
        for identity in results:
            existing[str(identity.league_id)] = {
                "label": identity.label,
                "total": identity.total,
                "resolved": identity.resolved,
                "seedable": identity.seedable,
                "ambiguous": identity.ambiguous,
                "status": identity.status,
                "ambiguous_names": [
                    {"name": o.name, "candidate": o.candidate, "confidence": o.confidence}
                    for o in identity.outcomes
                    if o.ambiguous or o.needs_review
                ],
            }
        RESULTS_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except OSError as error:
        print(f"  (could not persist: {error})", flush=True)


async def main() -> int:
    """Measure identity feasibility."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leagues", default="")
    parser.add_argument("--season", type=int, default=datetime.now().year - 1)
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

    chosen = dict(CANDIDATES)
    if args.leagues:
        wanted = {int(x.strip()) for x in args.leagues.split(",") if x.strip().isdigit()}
        chosen = {k: v for k, v in CANDIDATES.items() if k in wanted}

    from app.providers.api_football import ApiFootballProvider

    provider = ApiFootballProvider(api_key=key)
    database = Database(get_settings())
    await database.connect()

    results: list[CompetitionIdentity] = []
    async with database.session() as session:
        for league_id, (label, country) in chosen.items():
            print(f"\n  {label} (id {league_id}, season {args.season})", flush=True)
            identity = await audit_competition(
                session, provider, league_id, label, country, args.season
            )
            results.append(identity)
            print(
                f"    {identity.total} teams: {identity.resolved} resolved, "
                f"{identity.seedable} seedable, {identity.ambiguous} ambiguous"
                f"  -> {identity.status}",
                flush=True,
            )
            _save([identity])
        # Nothing was written by the resolver, but roll back explicitly so a
        # stray flush cannot leave anything behind.
        await session.rollback()

    await database.disconnect()

    print("\n" + "=" * 100)
    print("IDENTITY FEASIBILITY")
    print("=" * 100)
    print(f"\n  Requests used: {provider.budget.used}")
    print(
        f"\n  {'competition':<32}{'teams':>7}{'resolved':>10}{'seedable':>10}"
        f"{'ambiguous':>11}  status"
    )
    for identity in sorted(results, key=lambda i: i.share(i.ambiguous)):
        print(
            f"  {identity.label[:31]:<32}{identity.total:>7}"
            f"{identity.resolved:>6} {identity.share(identity.resolved):>3.0%}"
            f"{identity.seedable:>6} {identity.share(identity.seedable):>3.0%}"
            f"{identity.ambiguous:>7} {identity.share(identity.ambiguous):>3.0%}"
            f"  {identity.status}"
        )

    print("\n" + "-" * 100)
    print("AMBIGUOUS TEAMS (must stay reviewable, never forced)")
    print("-" * 100)
    any_ambiguous = False
    for identity in results:
        rows = [o for o in identity.outcomes if o.ambiguous or o.needs_review]
        if not rows:
            continue
        any_ambiguous = True
        print(f"\n  {identity.label}")
        for outcome in rows[:15]:
            print(
                f"    {outcome.name[:40]:<42} -> "
                f"{(outcome.candidate or '(no candidate)')[:32]:<34}"
                f"{outcome.confidence:.2f}"
            )
    if not any_ambiguous:
        print("\n  None. Every team either resolved or is safely seedable.")

    print("\n" + "=" * 100)
    print("READ-ONLY. learn=False throughout: no alias written, no canonical")
    print("record created, transaction rolled back. Reaches IDENTITY_READY at")
    print("most. Ingestion, model fitting and walk-forward validation remain,")
    print("and none of them is implied by these numbers.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
