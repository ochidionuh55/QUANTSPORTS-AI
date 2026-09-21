#!/usr/bin/env python3
"""EXP-002 — recency-weighted form challenger vs frozen control.

The control's ``team_strength`` weights a team's entire competition history
equally. EXP-002 changes exactly one thing: exponential time-decay weighting of
the attack/defence estimate, everything downstream (expected goals, Dixon-Coles
grid, rho) identical — so any difference is attributable to recency alone.

**No magic constant.** A predeclared set of half-lives is evaluated. The best is
selected on an *earlier validation window*; the challenger is then reported on a
*later test window it never touched for selection*. The headline is the
**paired** Brier delta (challenger minus control on the same fixtures) with a
bootstrap CI, plus per-competition and per-season deltas — a single aggregate
number is not enough.

Frozen control unchanged; canonical predicate untouched; the only write is one
summary row in ``research_experiments``. Diagnostic/validation, not promotion.

Run::

    railway ssh "python scripts/exp_002_recency.py"
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
HALF_LIVES: tuple[float, ...] = (90.0, 180.0, 270.0, 365.0)  # predeclared, in days
MIN_LEAGUE_RESULTS = 20
DEFAULT_WINDOW_DAYS = 1095  # 3 years total
TEST_DAYS = 365  # newest year is the held-out test window; older part is validation
BOOTSTRAP = 400
Probs = tuple[float, float, float]


@dataclass
class Fixture:
    competition: str
    season: str
    ordinal: int
    y: tuple[int, int, int]
    control: Probs
    recency: dict[float, Probs] = field(default_factory=dict)


@dataclass
class Accumulator:
    home: dict[int, list[tuple[int, int, int]]] = field(default_factory=dict)  # (gf, ga, ord)
    away: dict[int, list[tuple[int, int, int]]] = field(default_factory=dict)
    results: list[tuple[int, int]] = field(default_factory=list)
    sum_home: int = 0
    sum_away: int = 0

    def played(self, team: int) -> int:
        return len(self.home.get(team, [])) + len(self.away.get(team, []))

    def averages(self) -> LeagueAverages:
        n = len(self.results)
        return LeagueAverages(self.sum_home / n, self.sum_away / n, n)

    def record(self, home: int, away: int, hg: int, ag: int, ordinal: int) -> None:
        self.home.setdefault(home, []).append((hg, ag, ordinal))
        self.away.setdefault(away, []).append((ag, hg, ordinal))
        self.results.append((hg, ag))
        self.sum_home += hg
        self.sum_away += ag


def _control_strength(acc: Accumulator, team: int, avg: LeagueAverages) -> TeamStrength:
    home = [(g, c) for g, c, _ in acc.home.get(team, [])]
    away = [(g, c) for g, c, _ in acc.away.get(team, [])]
    return team_strength(team, home, away, avg)


def _recency_strength(
    acc: Accumulator, team: int, avg: LeagueAverages, ref: int, half_life: float
) -> TeamStrength:
    matches = acc.home.get(team, []) + acc.away.get(team, [])
    played = len(matches)
    if played == 0:
        return TeamStrength(team, 1.0, 1.0, 0)
    baseline = (avg.home_goals + avg.away_goals) / 2
    if baseline <= 0:
        return TeamStrength(team, 1.0, 1.0, played)
    wsum = wscored = wconc = 0.0
    for g, c, o in matches:
        w = 0.5 ** ((ref - o) / half_life)
        wsum += w
        wscored += w * g
        wconc += w * c
    expected = baseline * wsum
    if expected <= 0:
        return TeamStrength(team, 1.0, 1.0, played)
    return TeamStrength(team, wscored / expected, wconc / expected, played)


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

    out: list[Fixture] = []
    for comp_id, matches in by_comp.items():
        name = names.get(comp_id, str(comp_id))
        code = code_for_name(name)
        acc = Accumulator()
        for _cid, home_id, away_id, season, mdate, hg, ag in matches:
            home_id, away_id, hg, ag = int(home_id), int(away_id), int(hg), int(ag)
            ordinal = mdate.toordinal()
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
                    recency = {
                        hl: _grid_1x2(
                            _recency_strength(acc, home_id, avg, ordinal, hl),
                            _recency_strength(acc, away_id, avg, ordinal, hl),
                            avg,
                            code,
                        )
                        for hl in HALF_LIVES
                    }
                    out.append(
                        Fixture(
                            competition=name,
                            season=str(season or "?"),
                            ordinal=ordinal,
                            y=(int(hg > ag), int(hg == ag), int(hg < ag)),
                            control=control,
                            recency=recency,
                        )
                    )
            acc.record(home_id, away_id, hg, ag, ordinal)
    return out


def _brier_row(p: Probs, y: tuple[int, int, int]) -> float:
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y, strict=True))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _brier(fixtures: list[Fixture], pick) -> float:
    return _mean([_brier_row(pick(f), f.y) for f in fixtures])


def _paired_delta_ci(
    fixtures: list[Fixture], hl: float, n: int = BOOTSTRAP
) -> tuple[float, float, float]:
    deltas = [_brier_row(f.recency[hl], f.y) - _brier_row(f.control, f.y) for f in fixtures]
    rng = random.Random(20260920)
    size = len(deltas)
    means = []
    for _ in range(n):
        means.append(_mean([deltas[rng.randrange(size)] for _ in range(size)]))
    means.sort()
    return _mean(deltas), means[int(0.025 * n)], means[int(0.975 * n)]


def _group_delta(fixtures: list[Fixture], hl: float, key) -> dict[str, tuple[int, float]]:
    groups: dict[str, list[Fixture]] = defaultdict(list)
    for f in fixtures:
        groups[key(f)].append(f)
    out: dict[str, tuple[int, float]] = {}
    for g, fs in groups.items():
        d = _brier(fs, lambda f: f.recency[hl]) - _brier(fs, lambda f: f.control)
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


async def _persist(session, chosen: float, metrics: dict) -> None:
    experiment_id = "EXP-002"
    existing = (
        await session.execute(
            select(ResearchExperiment).where(ResearchExperiment.experiment_id == experiment_id)
        )
    ).scalar_one_or_none()
    payload = {
        "title": "Recency-weighted form challenger",
        "hypothesis": "Exponential time-decay weighting of team strength beats equal weighting.",
        "status": "completed",
        "lane": "B",
        "control_version": CONTROL,
        "challenger_version": f"exp002-recency-hl{int(chosen)}",
        "features": {
            "grid_version": grid_version(),
            "half_lives": list(HALF_LIVES),
            "chosen": chosen,
        },
        "metrics": metrics,
        "notes": (
            "Half-life selected on validation window; reported on untouched "
            "test window. Diagnostic."
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
        cutoff = max_date - timedelta(days=TEST_DAYS)  # validation < cutoff <= test
        fixtures = _walk(names, rows, since)
        validation = [f for f in fixtures if f.ordinal < cutoff.toordinal()]
        test = [f for f in fixtures if f.ordinal >= cutoff.toordinal()]
        if not validation or not test:
            print("Not enough fixtures to split validation/test.")
            await database.disconnect()
            return 1

        # Select the half-life on validation only.
        val_brier = {hl: _brier(validation, lambda f, h=hl: f.recency[h]) for hl in HALF_LIVES}
        chosen = min(val_brier, key=lambda hl: val_brier[hl])

        control_test = _brier(test, lambda f: f.control)
        chosen_test = _brier(test, lambda f, h=chosen: f.recency[h])
        delta, lo, hi = _paired_delta_ci(test, chosen)
        by_comp = _group_delta(test, chosen, lambda f: f.competition)
        by_season = _group_delta(test, chosen, lambda f: f.season)
        helped = sum(1 for _, d in by_comp.values() if d < 0)

        print(RULE)
        print(f"EXP-002 — RECENCY vs CONTROL  ({CONTROL}, {grid_version()})")
        print(RULE)
        print(f"\n  window since {since} · validation {len(validation):,} · test {len(test):,}")
        print(f"  test window starts {cutoff} (never used for half-life selection)")
        print("\n  VALIDATION Brier by half-life (selection set):")
        for hl in HALF_LIVES:
            mark = "  <- chosen" if hl == chosen else ""
            print(f"    half-life {int(hl):>3}d : {val_brier[hl]:.4f}{mark}")
        print(f"\n  TEST (held out) — chosen half-life {int(chosen)}d:")
        print(f"    control Brier    : {control_test:.4f}")
        print(f"    recency Brier    : {chosen_test:.4f}")
        print(f"    paired Δ (rec-ctrl): {delta:+.5f}   bootstrap 95% CI [{lo:+.5f}, {hi:+.5f}]")
        verdict = (
            "challenger better, CI excludes 0"
            if hi < 0
            else "control better, CI excludes 0"
            if lo > 0
            else "indistinguishable (CI spans 0)"
        )
        print(f"    verdict          : {verdict}")
        print(f"\n  per-competition: recency helped {helped}/{len(by_comp)} competitions")
        worst = sorted(by_comp.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
        print("    largest regressions (Δ>0 = worse):")
        for comp, (nc, d) in worst:
            print(f"      {comp[:34]:<36} n={nc:>6,}  Δ {d:+.4f}")

        metrics = {
            "validation_brier_by_half_life": val_brier,
            "chosen_half_life": chosen,
            "test_control_brier": control_test,
            "test_recency_brier": chosen_test,
            "test_paired_delta": delta,
            "test_delta_ci": [lo, hi],
            "test_competitions_helped": helped,
            "test_competitions_total": len(by_comp),
            "delta_by_competition": {c: {"n": n, "delta": d} for c, (n, d) in by_comp.items()},
            "delta_by_season": {s: {"n": n, "delta": d} for s, (n, d) in by_season.items()},
        }
        await _persist(session, chosen, metrics)
        print("\n  Registered EXP-002 in research_experiments. No other writes.")
        print(RULE)
    await database.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
