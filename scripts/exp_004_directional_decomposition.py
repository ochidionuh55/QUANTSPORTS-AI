#!/usr/bin/env python3
"""EXP-004 — directional-combination decomposition. Diagnostic only.

Reconstructs the frozen ``model-only-v2-dc`` grid walk-forward over the
historical corpus (team strength -> expected goals -> Dixon-Coles grid, no
odds), then decomposes each ``<direction> or Any Clean Sheet`` combination into
the mass genuinely contributed by the direction versus the shared Any-Clean-
Sheet component. It answers four questions:

  1. What drives the union probability of each combo service?
  2. Would a selection still rank highly without the AnyCS component?
  3. How much redundancy exists across the three combo services, and is it
     because they lean on the same AnyCS mass?
  4. When these bets win, which component actually won them?

**Nothing is changed.** The canonical predicate is untouched; the control is
frozen. The only write is one summary row in ``research_experiments``. This is
diagnostic evidence, not a tuning step: if it exposes a structural problem we
freeze and register it, and test any fix as its own challenger experiment.

Run::

    railway ssh "python scripts/exp_004_directional_decomposition.py"
    railway ssh "python scripts/exp_004_directional_decomposition.py --since 2023-01-01"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text

from app.core.competitions import code_for_name
from app.core.config import get_settings
from app.database.models import ResearchExperiment
from app.infrastructure.database import Database
from app.quant.grid import build_grid, grid_version
from app.quant.poisson import LeagueAverages, expected_goals, team_strength

EXPERIMENT_ID = "EXP-004"
CONTROL = "model-only-v2-dc"
RULE = "=" * 92

# direction key per combo service
SERVICES: dict[str, str] = {
    "home_or_cs": "home",
    "draw_or_cs": "draw",
    "away_or_cs": "away",
}
MIN_LEAGUE_RESULTS = 20  # LeagueAverages.is_reliable
PROD_FLOOR = 0.5  # production min_probability for a selection
TOP_K = 10  # Best-of depth
DEFAULT_WINDOW_DAYS = 730  # test window when --since is not given


@dataclass
class Decomp:
    """One fixture's v2-dc grid, reduced to the masses the decomposition needs."""

    match_date: date
    competition: str
    fixture: str
    hg: int
    ag: int
    p_home: float
    p_draw: float
    p_away: float
    p_acs: float
    inter: dict[str, float]  # P(direction AND AnyCS) per direction key

    def direction_p(self, key: str) -> float:
        return {"home": self.p_home, "draw": self.p_draw, "away": self.p_away}[key]

    def union(self, key: str) -> float:
        return self.direction_p(key) + self.p_acs - self.inter[key]

    def unique_dir(self, key: str) -> float:
        return self.direction_p(key) - self.inter[key]

    def pure_acs(self, key: str) -> float:
        return self.p_acs - self.inter[key]

    def directional_unique_share(self, key: str) -> float:
        u = self.union(key)
        return (self.unique_dir(key) / u) if u > 0 else 0.0

    def shared_share_of_union(self, key: str) -> float:
        u = self.union(key)
        return (self.pure_acs(key) / u) if u > 0 else 0.0

    def direction_won(self, key: str) -> bool:
        if key == "home":
            return self.hg > self.ag
        if key == "away":
            return self.hg < self.ag
        return self.hg == self.ag

    def acs_won(self) -> bool:
        return self.hg == 0 or self.ag == 0


@dataclass
class Accumulator:
    """Per-competition running history, appended to only after each forecast."""

    home_matches: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    away_matches: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    results: list[tuple[int, int]] = field(default_factory=list)
    sum_home: int = 0
    sum_away: int = 0

    def played(self, team: int) -> int:
        return len(self.home_matches.get(team, [])) + len(self.away_matches.get(team, []))

    def averages(self) -> LeagueAverages:
        n = len(self.results)
        return LeagueAverages(self.sum_home / n, self.sum_away / n, n)

    def record(self, home: int, away: int, hg: int, ag: int) -> None:
        self.home_matches.setdefault(home, []).append((hg, ag))
        self.away_matches.setdefault(away, []).append((ag, hg))
        self.results.append((hg, ag))
        self.sum_home += hg
        self.sum_away += ag


Reduced = tuple[float, float, float, float, dict[str, float]]


def _reduce_grid(gf: dict[tuple[int, int], float]) -> Reduced:
    """Return (p_home, p_draw, p_away, p_acs, intersections) from a grid."""
    p_home = p_draw = p_away = p_acs = 0.0
    inter = {"home": 0.0, "draw": 0.0, "away": 0.0}
    for (h, a), p in gf.items():
        acs = h == 0 or a == 0
        if acs:
            p_acs += p
        if h > a:
            p_home += p
            if acs:
                inter["home"] += p
        elif h == a:
            p_draw += p
            if acs:
                inter["draw"] += p
        else:
            p_away += p
            if acs:
                inter["away"] += p
    return p_home, p_draw, p_away, p_acs, inter


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


async def _load(session) -> tuple[dict[int, str], list[tuple]]:
    """Return competition id->name and every historical match, ordered."""
    comp_rows = await session.execute(
        text("SELECT id, canonical_name FROM competitions")
    )
    names = {int(r[0]): str(r[1]) for r in comp_rows.fetchall()}
    match_rows = await session.execute(
        text(
            "SELECT competition_id, home_team_id, away_team_id, match_date, "
            "home_goals, away_goals FROM historical_matches "
            "WHERE competition_id IS NOT NULL "
            "ORDER BY competition_id, match_date, id"
        )
    )
    return names, match_rows.fetchall()


def _walk(names: dict[int, str], rows: list[tuple], since: date) -> list[Decomp]:
    """Walk-forward per competition, decomposing fixtures on/after ``since``."""
    by_comp: dict[int, list[tuple]] = defaultdict(list)
    for r in rows:
        by_comp[int(r[0])].append(r)

    out: list[Decomp] = []
    for comp_id, matches in by_comp.items():
        name = names.get(comp_id, str(comp_id))
        code = code_for_name(name)
        acc = Accumulator()
        for _cid, home_id, away_id, mdate, hg, ag in matches:
            home_id, away_id, hg, ag = int(home_id), int(away_id), int(hg), int(ag)
            if (
                len(acc.results) >= MIN_LEAGUE_RESULTS
                and acc.played(home_id) >= 5
                and acc.played(away_id) >= 5
                and mdate >= since
            ):
                averages = acc.averages()
                hs = team_strength(
                    home_id, acc.home_matches[home_id], acc.away_matches[home_id], averages
                )
                as_ = team_strength(
                    away_id, acc.home_matches[away_id], acc.away_matches[away_id], averages
                )
                if hs.is_reliable and as_.is_reliable and averages.is_reliable:
                    lam_h, lam_a = expected_goals(hs, as_, averages)
                    grid = build_grid(lam_h, lam_a, corrected=True, competition=code)
                    gf = {k: float(v) for k, v in grid.items()}
                    p_home, p_draw, p_away, p_acs, inter = _reduce_grid(gf)
                    out.append(
                        Decomp(
                            match_date=mdate,
                            competition=name,
                            fixture=f"{home_id}v{away_id}",
                            hg=hg,
                            ag=ag,
                            p_home=p_home,
                            p_draw=p_draw,
                            p_away=p_away,
                            p_acs=p_acs,
                            inter=inter,
                        )
                    )
            acc.record(home_id, away_id, hg, ag)
    return out


def _decomposition(decomps: list[Decomp]) -> dict[str, dict[str, float]]:
    """Q1 — average composition of each combo service's union."""
    summary: dict[str, dict[str, float]] = {}
    for service, key in SERVICES.items():
        dus = [d.directional_unique_share(key) for d in decomps]
        summary[service] = {
            "n": float(len(decomps)),
            "mean_union": _mean([d.union(key) for d in decomps]),
            "mean_direction": _mean([d.direction_p(key) for d in decomps]),
            "mean_anycs": _mean([d.p_acs for d in decomps]),
            "mean_directional_unique_share": _mean(dus),
            "mean_shared_share_of_union": _mean([d.shared_share_of_union(key) for d in decomps]),
            "share_below_0_10": _mean([1.0 if x < 0.10 else 0.0 for x in dus]),
            "share_below_0_20": _mean([1.0 if x < 0.20 else 0.0 for x in dus]),
        }
    return summary


def _counterfactual(decomps: list[Decomp]) -> dict[str, dict[str, float]]:
    """Q2 — rank by union, then by direction alone (AnyCS removed); measure drift."""
    by_day: dict[date, list[Decomp]] = defaultdict(list)
    for d in decomps:
        by_day[d.match_date].append(d)

    result: dict[str, dict[str, float]] = {}
    for service, key in SERVICES.items():
        survived: list[float] = []
        displacement: list[float] = []
        for fixtures in by_day.values():
            if len(fixtures) < 3:
                continue
            by_union = sorted(fixtures, key=lambda d: d.union(key), reverse=True)
            by_dir = sorted(fixtures, key=lambda d: d.direction_p(key), reverse=True)
            k = min(TOP_K, len(fixtures))
            top_union = by_union[:k]
            top_dir_ids = {id(d) for d in by_dir[:k]}
            survived.append(sum(1 for d in top_union if id(d) in top_dir_ids) / k)
            dir_rank = {id(d): i for i, d in enumerate(by_dir)}
            displacement.append(_mean([abs(dir_rank[id(d)] - i) for i, d in enumerate(top_union)]))
        result[service] = {
            "days": float(len(displacement)),
            "top_k_survival": _mean(survived),
            "mean_rank_displacement": _mean(displacement),
        }
    return result


def _cross_service(decomps: list[Decomp]) -> dict[str, float]:
    """Q3 — do the three services pick the same fixtures, driven by AnyCS?"""
    by_day: dict[date, list[Decomp]] = defaultdict(list)
    for d in decomps:
        by_day[d.match_date].append(d)

    shared_2plus: list[float] = []
    shared_acs_dominant: list[float] = []
    for fixtures in by_day.values():
        if len(fixtures) < 3:
            continue
        tops: dict[str, set[int]] = {}
        for service, key in SERVICES.items():
            ranked = sorted(fixtures, key=lambda d: d.union(key), reverse=True)
            tops[service] = {id(d) for d in ranked[: min(TOP_K, len(fixtures))]}
        counts: dict[int, int] = defaultdict(int)
        for ids in tops.values():
            for fid in ids:
                counts[fid] += 1
        multi = [fid for fid, c in counts.items() if c >= 2]
        union_ids = set().union(*tops.values())
        shared_2plus.append(len(multi) / len(union_ids) if union_ids else 0.0)
        if multi:
            by_id = {id(d): d for d in fixtures}
            dominant = sum(
                1
                for fid in multi
                if by_id[fid].p_acs > max(by_id[fid].p_home, by_id[fid].p_draw, by_id[fid].p_away)
            )
            shared_acs_dominant.append(dominant / len(multi))
    return {
        "days": float(len(shared_2plus)),
        "mean_shared_2plus_fraction": _mean(shared_2plus),
        "mean_shared_are_acs_dominant": _mean(shared_acs_dominant),
    }


def _attribution(decomps: list[Decomp]) -> dict[str, dict[str, float]]:
    """Q4 — over selected fixtures, which component won?"""
    result: dict[str, dict[str, float]] = {}
    for service, key in SERVICES.items():
        selected = [d for d in decomps if d.union(key) >= PROD_FLOOR]
        both = dir_only = acs_only = neither = 0
        for d in selected:
            dw, aw = d.direction_won(key), d.acs_won()
            if dw and aw:
                both += 1
            elif dw:
                dir_only += 1
            elif aw:
                acs_only += 1
            else:
                neither += 1
        n = len(selected)
        wins = both + dir_only + acs_only
        result[service] = {
            "selected": float(n),
            "win_rate": (wins / n) if n else 0.0,
            "of_wins_both": (both / wins) if wins else 0.0,
            "of_wins_direction_only": (dir_only / wins) if wins else 0.0,
            "of_wins_anycs_only": (acs_only / wins) if wins else 0.0,
            "loss_rate": (neither / n) if n else 0.0,
        }
    return result


def _print_report(
    since: date,
    n: int,
    decomp: dict[str, dict[str, float]],
    counter: dict[str, dict[str, float]],
    cross: dict[str, float],
    attrib: dict[str, dict[str, float]],
) -> None:
    print(RULE)
    print(f"EXP-004 — DIRECTIONAL-COMBINATION DECOMPOSITION  (control {CONTROL}, {grid_version()})")
    print(RULE)
    print(f"\n  test window: fixtures on/after {since} · decomposed fixtures: {n:,}")

    print("\n" + "-" * 92)
    print("Q1. WHAT DRIVES THE UNION?  (DirectionalUniqueShare = unique direction / union)")
    print("-" * 92)
    for service in SERVICES:
        s = decomp[service]
        print(f"\n  {service}")
        print(f"    mean union             : {s['mean_union']:.3f}")
        print(f"    mean P(direction)      : {s['mean_direction']:.3f}")
        print(f"    mean P(AnyCS)          : {s['mean_anycs']:.3f}")
        print(f"    DirectionalUniqueShare : {s['mean_directional_unique_share']:.3f}")
        print(f"    shared-share of union  : {s['mean_shared_share_of_union']:.3f}")
        print(f"    selections DUS < 0.10  : {s['share_below_0_10']:.1%}")
        print(f"    selections DUS < 0.20  : {s['share_below_0_20']:.1%}")

    print("\n" + "-" * 92)
    print("Q2. WOULD IT RANK WITHOUT AnyCS?  (rank by union vs by direction alone)")
    print("-" * 92)
    for service in SERVICES:
        c = counter[service]
        print(
            f"  {service:<12} top-{TOP_K} survival {c['top_k_survival']:.1%}"
            f"   mean rank displacement {c['mean_rank_displacement']:.1f}"
            f"   ({int(c['days'])} match-days)"
        )

    print("\n" + "-" * 92)
    print("Q3. CROSS-SERVICE REDUNDANCY  (same fixtures, and are they AnyCS-dominant?)")
    print("-" * 92)
    shared_frac = cross["mean_shared_2plus_fraction"]
    print(f"  fixtures shared by >=2 services : {shared_frac:.1%} of the day's top set")
    print(f"  of those, AnyCS is the largest mass : {cross['mean_shared_are_acs_dominant']:.1%}")
    print(f"  ({int(cross['days'])} match-days)")

    print("\n" + "-" * 92)
    print("Q4. WHEN THEY WIN, WHICH COMPONENT WON?  (selected = union >= 0.50)")
    print("-" * 92)
    for service in SERVICES:
        a = attrib[service]
        print(f"\n  {service}  (selected {int(a['selected']):,}, win rate {a['win_rate']:.1%})")
        print(f"    of wins — both won      : {a['of_wins_both']:.1%}")
        print(f"    of wins — direction only: {a['of_wins_direction_only']:.1%}")
        print(f"    of wins — AnyCS only    : {a['of_wins_anycs_only']:.1%}")
        print(f"    losses (neither)        : {a['loss_rate']:.1%}")
    print("\n" + RULE)


async def _persist(session, since: date, n: int, metrics: dict) -> None:
    """Register/update EXP-004 in research_experiments. The only write."""
    existing = (
        await session.execute(
            select(ResearchExperiment).where(ResearchExperiment.experiment_id == EXPERIMENT_ID)
        )
    ).scalar_one_or_none()
    payload = {
        "title": "Directional-combination decomposition",
        "hypothesis": (
            "Ranking <direction> or Any Clean Sheet by union probability lets the "
            "shared AnyCS mass carry weak directional predictions into top selections."
        ),
        "status": "completed",
        "lane": "A",
        "control_version": CONTROL,
        "challenger_version": None,
        "data_cutoff": since,
        "features": {"grid_version": grid_version(), "decomposed_fixtures": n},
        "metrics": metrics,
        "notes": "Diagnostic only. Canonical predicate unchanged. No tuning applied.",
    }
    if existing is None:
        session.add(ResearchExperiment(experiment_id=EXPERIMENT_ID, **payload))
    else:
        for k, v in payload.items():
            setattr(existing, k, v)
    await session.commit()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=str, default=None, help="Test window start, YYYY-MM-DD.")
    args = parser.parse_args()

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        names, rows = await _load(session)
        if not rows:
            print("No historical matches found.")
            await database.disconnect()
            return 1
        max_date = max(r[3] for r in rows)
        since = (
            datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC).date()
            if args.since
            else max_date - timedelta(days=DEFAULT_WINDOW_DAYS)
        )
        decomps = _walk(names, rows, since)
        if not decomps:
            print(f"No fixtures qualified in the test window since {since}.")
            await database.disconnect()
            return 1

        decomp = _decomposition(decomps)
        counter = _counterfactual(decomps)
        cross = _cross_service(decomps)
        attrib = _attribution(decomps)
        _print_report(since, len(decomps), decomp, counter, cross, attrib)

        metrics = {
            "decomposition": decomp,
            "counterfactual_ranking": counter,
            "cross_service": cross,
            "outcome_attribution": attrib,
        }
        await _persist(session, since, len(decomps), metrics)
        print("Registered EXP-004 in research_experiments (summary metrics). No other writes.")
    await database.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
