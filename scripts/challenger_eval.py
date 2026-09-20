#!/usr/bin/env python3
"""Challenger evaluation harness — establishes the frozen control's baseline.

Recomputes ``model-only-v2-dc`` walk-forward over the corpus (team strength ->
expected goals -> Dixon-Coles grid, per-competition rho, no odds) and scores its
1X2 forecasts against outcomes on the full metric suite: Brier, log loss, ECE,
probability-band reliability, H/D/A bias, per-competition and per-season
stability, and a bootstrap CI on Brier.

This run is the **control**. EXP-002 (form/recency) and EXP-003 (dynamic
strength) will register as ``--variant`` alternatives scored on the *identical*
fixtures, so their deltas are like-for-like. A headline aggregate delta is
never sufficient for promotion — stability across competitions and seasons and
overlapping bootstrap intervals are what this measures.

Nothing production changes. The only write is one summary row in
``research_experiments``.

Run::

    railway ssh "python scripts/challenger_eval.py"
    railway ssh "python scripts/challenger_eval.py --variant control --since 2022-01-01"
"""

from __future__ import annotations

import argparse
import asyncio
import math
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
from app.quant.poisson import LeagueAverages, expected_goals, team_strength

Probs = tuple[float, float, float]

CONTROL = "model-only-v2-dc"
RULE = "=" * 92
OUTCOMES = ("home", "draw", "away")
MIN_LEAGUE_RESULTS = 20
DEFAULT_WINDOW_DAYS = 1095  # 3 years of test fixtures
EPS = 1e-12
BOOTSTRAP = 400


@dataclass
class Row:
    """One scored fixture: model 1X2 vs the realised outcome."""

    competition: str
    season: str
    p: tuple[float, float, float]  # home, draw, away
    y: tuple[int, int, int]  # one-hot outcome


@dataclass
class Accumulator:
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


def _control_1x2(acc: Accumulator, home: int, away: int, code: str | None) -> Probs | None:
    """Frozen v2-dc 1X2 from the Dixon-Coles grid, or None if unmodellable."""
    averages = acc.averages()
    hs = team_strength(
        home, acc.home_matches.get(home, []), acc.away_matches.get(home, []), averages
    )
    as_ = team_strength(
        away, acc.home_matches.get(away, []), acc.away_matches.get(away, []), averages
    )
    if not (hs.is_reliable and as_.is_reliable and averages.is_reliable):
        return None
    lam_h, lam_a = expected_goals(hs, as_, averages)
    grid = build_grid(lam_h, lam_a, corrected=True, competition=code)
    p_home = p_draw = p_away = 0.0
    for (h, a), p in grid.items():
        fp = float(p)
        if h > a:
            p_home += fp
        elif h == a:
            p_draw += fp
        else:
            p_away += fp
    total = p_home + p_draw + p_away
    if total <= 0:
        return None
    return (p_home / total, p_draw / total, p_away / total)


# Challenger variants (EXP-002/003) register here later, same signature.
VARIANTS = {"control": _control_1x2}


def _walk(names: dict[int, str], rows: list[tuple], since: date, variant: str) -> list[Row]:
    predict = VARIANTS[variant]
    by_comp: dict[int, list[tuple]] = defaultdict(list)
    for r in rows:
        by_comp[int(r[0])].append(r)

    out: list[Row] = []
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
                probs = predict(acc, home_id, away_id, code)
                if probs is not None:
                    y = (int(hg > ag), int(hg == ag), int(hg < ag))
                    out.append(Row(name, str(season or "?"), probs, y))
            acc.record(home_id, away_id, hg, ag)
    return out


# ── metrics ──────────────────────────────────────────────────────────────────
def _brier(rows: list[Row]) -> float:
    return sum(sum((p - y) ** 2 for p, y in zip(r.p, r.y, strict=True)) for r in rows) / len(rows)


def _log_loss(rows: list[Row]) -> float:
    total = 0.0
    for r in rows:
        for p, y in zip(r.p, r.y, strict=True):
            if y:
                total -= math.log(max(p, EPS))
    return total / len(rows)


def _ece(rows: list[Row], bins: int = 10) -> float:
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for r in rows:
        for p, y in zip(r.p, r.y, strict=True):
            idx = min(bins - 1, int(p * bins))
            buckets[idx].append((p, y))
    n = sum(len(b) for b in buckets)
    ece = 0.0
    for b in buckets:
        if not b:
            continue
        mean_p = sum(p for p, _ in b) / len(b)
        mean_y = sum(y for _, y in b) / len(b)
        ece += (len(b) / n) * abs(mean_p - mean_y)
    return ece


def _bands(rows: list[Row], bins: int = 10) -> list[tuple[str, int, float, float]]:
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for r in rows:
        for p, y in zip(r.p, r.y, strict=True):
            buckets[min(bins - 1, int(p * bins))].append((p, y))
    out = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        mp = sum(p for p, _ in b) / len(b)
        my = sum(y for _, y in b) / len(b)
        out.append((f"{i * 10:>2}-{i * 10 + 10}%", len(b), mp, my))
    return out


def _hda_bias(rows: list[Row]) -> dict[str, float]:
    n = len(rows)
    return {
        o: (sum(r.p[i] for r in rows) - sum(r.y[i] for r in rows)) / n
        for i, o in enumerate(OUTCOMES)
    }


def _group_brier(rows: list[Row], key) -> dict[str, tuple[int, float]]:
    groups: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return {g: (len(rs), _brier(rs)) for g, rs in groups.items()}


def _bootstrap_brier(rows: list[Row], n: int = BOOTSTRAP) -> tuple[float, float]:
    rng = random.Random(20260920)
    size = len(rows)
    samples = []
    for _ in range(n):
        pick = [rows[rng.randrange(size)] for _ in range(size)]
        samples.append(_brier(pick))
    samples.sort()
    return samples[int(0.025 * n)], samples[int(0.975 * n)]


def _print_report(variant: str, since: date, rows: list[Row]) -> dict:
    brier = _brier(rows)
    lo, hi = _bootstrap_brier(rows)
    logloss = _log_loss(rows)
    ece = _ece(rows)
    bias = _hda_bias(rows)
    by_comp = _group_brier(rows, lambda r: r.competition)
    by_season = _group_brier(rows, lambda r: r.season)
    comp_briers = [b for _, b in by_comp.values()]
    season_briers = [b for _, b in by_season.values()]

    print(RULE)
    print(f"CHALLENGER EVAL — variant '{variant}'  (control {CONTROL}, {grid_version()})")
    print(RULE)
    print(f"\n  test window: fixtures on/after {since} · scored fixtures: {len(rows):,}")
    print("\n  OVERALL 1X2")
    print(f"    Brier            : {brier:.4f}   (bootstrap 95% CI [{lo:.4f}, {hi:.4f}])")
    print(f"    Log loss         : {logloss:.4f}")
    print(f"    ECE              : {ece:.4f}")
    print(
        f"    H/D/A bias (pred-obs): home {bias['home']:+.3f}  "
        f"draw {bias['draw']:+.3f}  away {bias['away']:+.3f}"
    )

    print("\n  RELIABILITY BY PROBABILITY BAND (pooled 1X2)")
    print(f"    {'band':<9}{'n':>8}{'pred':>9}{'obs':>9}")
    for band, count, mp, my in _bands(rows):
        print(f"    {band:<9}{count:>8,}{mp:>9.3f}{my:>9.3f}")

    print("\n  STABILITY")
    print(
        f"    competitions scored: {len(by_comp)}   "
        f"Brier min {min(comp_briers):.4f} / max {max(comp_briers):.4f}"
    )
    print(
        f"    seasons scored     : {len(by_season)}   "
        f"Brier min {min(season_briers):.4f} / max {max(season_briers):.4f}"
    )
    worst = sorted(by_comp.items(), key=lambda kv: -kv[1][1])[:5]
    print("    highest-Brier competitions:")
    for comp, (nc, b) in worst:
        print(f"      {comp[:34]:<36} n={nc:>6,}  Brier {b:.4f}")

    return {
        "scored_fixtures": len(rows),
        "brier": brier,
        "brier_ci_low": lo,
        "brier_ci_high": hi,
        "log_loss": logloss,
        "ece": ece,
        "hda_bias": bias,
        "brier_by_competition": {c: {"n": n, "brier": b} for c, (n, b) in by_comp.items()},
        "brier_by_season": {s: {"n": n, "brier": b} for s, (n, b) in by_season.items()},
    }


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


async def _persist(session, variant: str, since: date, metrics: dict) -> None:
    experiment_id = "CTRL-V2DC" if variant == "control" else f"EXP-{variant}"
    existing = (
        await session.execute(
            select(ResearchExperiment).where(ResearchExperiment.experiment_id == experiment_id)
        )
    ).scalar_one_or_none()
    payload = {
        "title": f"Walk-forward 1X2 evaluation — {variant}",
        "hypothesis": "Establish the frozen control's measured baseline for challenger comparison.",
        "status": "completed",
        "lane": "B",
        "control_version": CONTROL,
        "challenger_version": None if variant == "control" else variant,
        "data_cutoff": since,
        "features": {"grid_version": grid_version(), "variant": variant},
        "metrics": metrics,
        "notes": "Control baseline. No production change; frozen v2-dc recomputed for research.",
    }
    if existing is None:
        session.add(ResearchExperiment(experiment_id=experiment_id, **payload))
    else:
        for k, v in payload.items():
            setattr(existing, k, v)
    await session.commit()
    print(f"\n  Registered {experiment_id} in research_experiments. No other writes.")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", default="control", choices=sorted(VARIANTS))
    parser.add_argument("--since", default=None, help="Test window start, YYYY-MM-DD.")
    args = parser.parse_args()

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        names, rows = await _load(session)
        if not rows:
            print("No historical matches found.")
            await database.disconnect()
            return 1
        max_date = max(r[4] for r in rows)
        since = (
            datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC).date()
            if args.since
            else max_date - timedelta(days=DEFAULT_WINDOW_DAYS)
        )
        scored = _walk(names, rows, since, args.variant)
        if not scored:
            print(f"No fixtures scored in the window since {since}.")
            await database.disconnect()
            return 1
        metrics = _print_report(args.variant, since, scored)
        await _persist(session, args.variant, since, metrics)
        print("\n" + RULE)
    await database.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
