#!/usr/bin/env python3
"""EXP-003 — opponent-adjusted strength challenger vs frozen control.

The frozen control's ``team_strength`` is opponent-agnostic: a team's attack is
its goals scored divided by the *league* average, so beating a title-winning
defence and thrashing the bottom club count the same. EXP-003 changes exactly
one thing: the strength estimate is **opponent-adjusted** — goals are credited
relative to the actual opponent's defence (and conceded goals relative to the
opponent's attack), solved as a short fixed-point over all teams in the
competition. Everything downstream (expected goals, Dixon-Coles grid, rho) is
identical, so any difference is attributable to opponent adjustment alone.

**No magic iteration count.** A predeclared set of fixed-point iterations is
evaluated; the best is selected on an *earlier validation window* and the
challenger is reported on a *later test window it never touched for selection*.
The headline is the **paired** Brier delta (challenger minus control on the same
fixtures) with a bootstrap CI, plus per-competition and per-season deltas.

Frozen control unchanged; canonical predicate untouched; the only write is one
summary row in ``research_experiments``. Diagnostic, never promotion.

Note: the fixed point is recomputed from each fixture's accumulated history
(walk-forward, past matches only), so this runs heavier than EXP-002 — expect
minutes, not seconds, on the full corpus.

Run::

    railway ssh "python scripts/exp_003_opponent_adjusted.py"
"""

from __future__ import annotations

import argparse
import asyncio
import random
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
from app.quant.poisson import LeagueAverages, TeamStrength, expected_goals, team_strength

CONTROL = "model-only-v2-dc"
RULE = "=" * 92
ITERATIONS: tuple[int, ...] = (1, 2, 3)  # predeclared fixed-point passes
MIN_LEAGUE_RESULTS = 20
DEFAULT_WINDOW_DAYS = 1095
TEST_DAYS = 365
BOOTSTRAP = 400
RATIO_FLOOR, RATIO_CEIL = 0.2, 5.0  # clamp so a fixed-point pass cannot run away
Probs = tuple[float, float, float]


@dataclass
class Fixture:
    competition: str
    season: str
    ordinal: int
    y: tuple[int, int, int]
    control: Probs
    adjusted: dict[int, Probs] = field(default_factory=dict)  # iterations -> 1X2


@dataclass
class Accumulator:
    matches: list[tuple[int, int, int, int]] = field(default_factory=list)  # h, a, hg, ag
    home: dict[int, list[tuple[int, int]]] = field(default_factory=dict)  # team -> (gf, ga)
    away: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    results: list[tuple[int, int]] = field(default_factory=list)
    sum_home: int = 0
    sum_away: int = 0

    def played(self, team: int) -> int:
        return len(self.home.get(team, [])) + len(self.away.get(team, []))

    def averages(self) -> LeagueAverages:
        n = len(self.results)
        return LeagueAverages(self.sum_home / n, self.sum_away / n, n)

    def record(self, home: int, away: int, hg: int, ag: int) -> None:
        self.matches.append((home, away, hg, ag))
        self.home.setdefault(home, []).append((hg, ag))
        self.away.setdefault(away, []).append((ag, hg))
        self.results.append((hg, ag))
        self.sum_home += hg
        self.sum_away += ag


def _control_strength(acc: Accumulator, team: int, avg: LeagueAverages) -> TeamStrength:
    return team_strength(team, acc.home.get(team, []), acc.away.get(team, []), avg)


def _clamp(x: float) -> float:
    return max(RATIO_FLOOR, min(RATIO_CEIL, x))


def _adjusted_strengths(
    acc: Accumulator, avg: LeagueAverages, max_iter: int
) -> dict[int, dict[int, TeamStrength]]:
    """Fixed-point opponent-adjusted attack/defence, snapshotted per iteration.

    ``attack[t] = goals scored by t / sum over t's matches of
    (league_side_avg x opponent_defence)`` and symmetrically for defence, iterated
    ``max_iter`` times from a neutral (1.0, 1.0) start. Returns the strengths
    after each iteration so a single sequence of passes serves every predeclared
    iteration count.
    """
    scored: dict[int, int] = defaultdict(int)
    conceded: dict[int, int] = defaultdict(int)
    counts: dict[int, int] = defaultdict(int)
    for h, a, hg, ag in acc.matches:
        scored[h] += hg
        conceded[h] += ag
        counts[h] += 1
        scored[a] += ag
        conceded[a] += hg
        counts[a] += 1
    teams = list(counts)
    attack = dict.fromkeys(teams, 1.0)
    defence = dict.fromkeys(teams, 1.0)
    snapshots: dict[int, dict[int, TeamStrength]] = {}
    for it in range(1, max_iter + 1):
        denom_a: dict[int, float] = defaultdict(float)
        denom_d: dict[int, float] = defaultdict(float)
        for h, a, _hg, _ag in acc.matches:
            denom_a[h] += avg.home_goals * defence[a]
            denom_d[h] += avg.away_goals * attack[a]
            denom_a[a] += avg.away_goals * defence[h]
            denom_d[a] += avg.home_goals * attack[h]
        attack = {
            t: _clamp(scored[t] / denom_a[t]) if denom_a[t] > 0 else 1.0 for t in teams
        }
        defence = {
            t: _clamp(conceded[t] / denom_d[t]) if denom_d[t] > 0 else 1.0 for t in teams
        }
        snapshots[it] = {
            t: TeamStrength(t, attack[t], defence[t], counts[t]) for t in teams
        }
    return snapshots


def _grid_1x2(hs: TeamStrength, as_: TeamStrength, avg: LeagueAverages, code: str | None) -> Probs:
    lam_h, lam_a = expected_goals(hs, as_, avg)
    grid = build_grid(lam_h, lam_a, corrected=True, competition=code)
    ph = pd = pa = 0.0
    for (h, a), p in grid.items():
        fp = float(p)
        if h > a:
            ph += fp
        elif h == a:
            pd += fp
        else:
            pa += fp
    total = ph + pd + pa
    return (ph / total, pd / total, pa / total)


def _walk(names: dict[int, str], rows: list[tuple], since: date) -> list[Fixture]:
    by_comp: dict[int, list[tuple]] = defaultdict(list)
    for r in rows:
        by_comp[int(r[0])].append(r)

    max_iter = max(ITERATIONS)
    out: list[Fixture] = []
    for comp_id, matches in by_comp.items():
        name = names.get(comp_id, str(comp_id))
        code = code_for_name(name)
        acc = Accumulator()
        for _cid, home_id, away_id, season, mdate, hg, ag in matches:
            home_id, away_id, hg, ag = int(home_id), int(away_id), int(hg), int(ag)
            if (
                len(acc.results) >= MIN_LEAGUE_RESULTS
                and acc.played(home_id) >= 5
                and acc.played(away_id) >= 5
                and mdate >= since
            ):
                avg = acc.averages()
                ch = _control_strength(acc, home_id, avg)
                ca = _control_strength(acc, away_id, avg)
                if ch.is_reliable and ca.is_reliable and avg.is_reliable:
                    control = _grid_1x2(ch, ca, avg, code)
                    snaps = _adjusted_strengths(acc, avg, max_iter)
                    adjusted = {
                        it: _grid_1x2(snap[home_id], snap[away_id], avg, code)
                        for it, snap in snaps.items()
                        if home_id in snap and away_id in snap
                    }
                    out.append(
                        Fixture(
                            competition=name,
                            season=str(season or "?"),
                            ordinal=mdate.toordinal(),
                            y=(int(hg > ag), int(hg == ag), int(hg < ag)),
                            control=control,
                            adjusted=adjusted,
                        )
                    )
            acc.record(home_id, away_id, hg, ag)
    return out


def _brier_row(p: Probs, y: tuple[int, int, int]) -> float:
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y, strict=True))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _brier(fixtures: list[Fixture], pick) -> float:
    return _mean([_brier_row(pick(f), f.y) for f in fixtures])


def _paired_delta_ci(
    fixtures: list[Fixture], it: int, n: int = BOOTSTRAP
) -> tuple[float, float, float]:
    deltas = [_brier_row(f.adjusted[it], f.y) - _brier_row(f.control, f.y) for f in fixtures]
    rng = random.Random(20260920)
    size = len(deltas)
    means = []
    for _ in range(n):
        means.append(_mean([deltas[rng.randrange(size)] for _ in range(size)]))
    means.sort()
    return _mean(deltas), means[int(0.025 * n)], means[int(0.975 * n)]


def _group_delta(fixtures: list[Fixture], it: int, key) -> dict[str, tuple[int, float]]:
    groups: dict[str, list[Fixture]] = defaultdict(list)
    for f in fixtures:
        groups[key(f)].append(f)
    out: dict[str, tuple[int, float]] = {}
    for g, fs in groups.items():
        d = _brier(fs, lambda f: f.adjusted[it]) - _brier(fs, lambda f: f.control)
        out[g] = (len(fs), d)
    return out


async def _load(session) -> tuple[dict[int, str], list[tuple]]:
    comp_rows = await session.execute(text("SELECT id, canonical_name FROM competitions"))
    names = {int(r[0]): str(r[1]) for r in comp_rows.fetchall()}
    match_rows = await session.execute(
        text(
            "SELECT competition_id, home_team_id, away_team_id, season, match_date, "
            "home_goals, away_goals FROM historical_matches "
            "WHERE competition_id IS NOT NULL "
            "ORDER BY competition_id, match_date, id"
        )
    )
    return names, match_rows.fetchall()


async def _persist(session, chosen: int, metrics: dict) -> None:
    experiment_id = "EXP-003"
    existing = (
        await session.execute(
            select(ResearchExperiment).where(ResearchExperiment.experiment_id == experiment_id)
        )
    ).scalar_one_or_none()
    payload = {
        "title": "Opponent-adjusted strength challenger",
        "hypothesis": (
            "Crediting goals relative to the actual opponent's strength beats the "
            "opponent-agnostic league-average estimate."
        ),
        "status": "completed",
        "lane": "B",
        "control_version": CONTROL,
        "challenger_version": f"exp003-oppadj-it{chosen}",
        "features": {
            "grid_version": grid_version(),
            "iterations": list(ITERATIONS),
            "chosen": chosen,
        },
        "metrics": metrics,
        "notes": (
            "Fixed-point iterations selected on validation window; reported on "
            "untouched test window. Diagnostic, not promotion."
        ),
    }
    if existing is None:
        session.add(ResearchExperiment(experiment_id=experiment_id, **payload))
    else:
        for k, v in payload.items():
            setattr(existing, k, v)
    await session.commit()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=None)
    args = parser.parse_args()

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        names, rows = await _load(session)
        if not rows:
            print("No historical matches.")
            await database.disconnect()
            return 1
        max_date = max(r[4] for r in rows)
        since = (
            datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC).date()
            if args.since
            else max_date - timedelta(days=DEFAULT_WINDOW_DAYS)
        )
        cutoff = max_date - timedelta(days=TEST_DAYS)
        fixtures = _walk(names, rows, since)
        validation = [f for f in fixtures if f.ordinal < cutoff.toordinal()]
        test = [f for f in fixtures if f.ordinal >= cutoff.toordinal()]
        # Keep only fixtures where every predeclared iteration produced a value.
        validation = [f for f in validation if all(it in f.adjusted for it in ITERATIONS)]
        test = [f for f in test if all(it in f.adjusted for it in ITERATIONS)]
        if not validation or not test:
            print("Not enough fixtures to split validation/test.")
            await database.disconnect()
            return 1

        val_brier = {it: _brier(validation, lambda f, i=it: f.adjusted[i]) for it in ITERATIONS}
        val_control = _brier(validation, lambda f: f.control)
        chosen = min(val_brier, key=lambda it: val_brier[it])

        control_test = _brier(test, lambda f: f.control)
        chosen_test = _brier(test, lambda f, i=chosen: f.adjusted[i])
        delta, lo, hi = _paired_delta_ci(test, chosen)
        by_comp = _group_delta(test, chosen, lambda f: f.competition)
        by_season = _group_delta(test, chosen, lambda f: f.season)
        helped = sum(1 for _, d in by_comp.values() if d < 0)

        print(RULE)
        print(f"EXP-003 — OPPONENT-ADJUSTED vs CONTROL  ({CONTROL}, {grid_version()})")
        print(RULE)
        print(f"\n  window since {since} · validation {len(validation):,} · test {len(test):,}")
        print(f"  test window starts {cutoff} (never used for iteration selection)")
        print("\n  VALIDATION Brier by iterations (selection set):")
        print(f"    control (opponent-agnostic) : {val_control:.4f}")
        for it in ITERATIONS:
            mark = "  <- chosen" if it == chosen else ""
            print(f"    iterations {it}                : {val_brier[it]:.4f}{mark}")
        print(f"\n  TEST (held out) — chosen iterations {chosen}:")
        print(f"    control Brier      : {control_test:.4f}")
        print(f"    adjusted Brier     : {chosen_test:.4f}")
        print(f"    paired Δ (adj-ctrl): {delta:+.5f}   bootstrap 95% CI [{lo:+.5f}, {hi:+.5f}]")
        verdict = (
            "challenger better, CI excludes 0"
            if hi < 0
            else "control better, CI excludes 0"
            if lo > 0
            else "indistinguishable (CI spans 0)"
        )
        print(f"    verdict            : {verdict}")
        print(f"\n  per-competition: adjusted helped {helped}/{len(by_comp)} competitions")
        worst = sorted(by_comp.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
        print("    largest regressions (Δ>0 = worse):")
        for comp, (nc, d) in worst:
            print(f"      {comp[:34]:<36} n={nc:>6,}  Δ {d:+.4f}")

        metrics = {
            "validation_control_brier": val_control,
            "validation_brier_by_iterations": {str(k): v for k, v in val_brier.items()},
            "chosen_iterations": chosen,
            "test_control_brier": control_test,
            "test_adjusted_brier": chosen_test,
            "test_paired_delta": delta,
            "test_delta_ci": [lo, hi],
            "test_competitions_helped": helped,
            "test_competitions_total": len(by_comp),
            "delta_by_season": {s: {"n": n, "delta": d} for s, (n, d) in by_season.items()},
        }
        await _persist(session, chosen, metrics)
        print("\n  Registered EXP-003 in research_experiments. No other writes.")
        print(RULE)
    await database.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
