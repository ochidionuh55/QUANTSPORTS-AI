#!/usr/bin/env python3
"""EXP-009 — totals-preserving stack, as a first-class experiment.

The market audit showed the plain stack (EXP-007) and plain recency (EXP-002)
improve the *result* but distort the *total goals*, hurting O/U and BTTS. EXP-009
keeps the recency + opponent-adjusted **ratio** (which drives the result) but
rescales each fixture's expected goals so the **total** matches the calibrated
control — recency/opponent-adjustment for *who wins*, control for *how many
goals*. The audit confirmed this neutralises the goals regression while keeping
the result gain; this script makes it a first-class, registered result.

Three arms on identical fixtures:

* CTRL     — frozen ``model-only-v2-dc``
* EXP-002  — recency (half-life 365), for reference (the result gain to keep)
* EXP-009  — totals-preserving stack (recency + opponent-adjusted ratio, control
             total; opponent-adjustment iterations selected on validation)

**Predeclared:** recency half-life fixed at 365; the opponent-adjustment
iteration count is selected on the *validation* window from {1, 2, 3}; the test
window is read once. No post-test tuning.

Two runs matter:
* default (newest ``TEST_DAYS`` as test) — comparable to EXP-002/007/008;
* ``--test-end 2023-09-04`` — the **untouched** pre-EXP-002 era for a clean OOS
  read (the promotion gate that the market audit could not provide).

Frozen control unchanged; EXP-002/003/007/008 records untouched; the only write
is one summary row in ``research_experiments`` (EXP-009). Diagnostic, never
promotion. Runs heavier than EXP-002 (a fixed point per fixture).

Run::

    railway ssh "python scripts/exp_009_stack_norm.py"
    railway ssh "python scripts/exp_009_stack_norm.py --test-end 2023-09-04"
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
from app.quant.poisson import LeagueAverages, TeamStrength, expected_goals, team_strength

CONTROL = "model-only-v2-dc"
RULE = "=" * 92
HALF_LIFE = 365.0
ITERATIONS: tuple[int, ...] = (1, 2, 3)
MIN_LEAGUE_RESULTS = 20
DEFAULT_WINDOW_DAYS = 1095
DEFAULT_TEST_END: str | None = None
TEST_DAYS = 365
BOOTSTRAP = 400
RATIO_FLOOR, RATIO_CEIL = 0.2, 5.0
EPS = 1e-12
OUTCOMES = ("home", "draw", "away")
Probs = tuple[float, float, float]


@dataclass
class Fixture:
    competition: str
    season: str
    ordinal: int
    y: tuple[int, int, int]
    control: Probs
    recency: Probs
    stack_norm: dict[int, Probs] = field(default_factory=dict)


@dataclass
class Accumulator:
    matches: list[tuple[int, int, int, int, int]] = field(default_factory=list)
    home: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    away: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    dated: dict[int, list[tuple[int, int, int]]] = field(default_factory=dict)
    results: list[tuple[int, int]] = field(default_factory=list)
    sum_home: int = 0
    sum_away: int = 0

    def played(self, team: int) -> int:
        return len(self.home.get(team, [])) + len(self.away.get(team, []))

    def averages(self) -> LeagueAverages:
        n = len(self.results)
        return LeagueAverages(self.sum_home / n, self.sum_away / n, n)

    def record(self, home: int, away: int, hg: int, ag: int, ordinal: int) -> None:
        self.matches.append((home, away, hg, ag, ordinal))
        self.home.setdefault(home, []).append((hg, ag))
        self.away.setdefault(away, []).append((ag, hg))
        self.dated.setdefault(home, []).append((hg, ag, ordinal))
        self.dated.setdefault(away, []).append((ag, hg, ordinal))
        self.results.append((hg, ag))
        self.sum_home += hg
        self.sum_away += ag


def _clamp(x: float) -> float:
    return max(RATIO_FLOOR, min(RATIO_CEIL, x))


def _control_strength(acc: Accumulator, team: int, avg: LeagueAverages) -> TeamStrength:
    return team_strength(team, acc.home.get(team, []), acc.away.get(team, []), avg)


def _recency_strength(
    acc: Accumulator, team: int, avg: LeagueAverages, ref: int, half_life: float
) -> TeamStrength:
    matches = acc.dated.get(team, [])
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


def _adjusted_strengths(
    acc: Accumulator, avg: LeagueAverages, max_iter: int, ref: int, half_life: float
) -> dict[int, dict[int, TeamStrength]]:
    def w(o: int) -> float:
        return 0.5 ** ((ref - o) / half_life)

    wscored: dict[int, float] = defaultdict(float)
    wconc: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    for h, a, hg, ag, o in acc.matches:
        ww = w(o)
        wscored[h] += ww * hg
        wconc[h] += ww * ag
        counts[h] += 1
        wscored[a] += ww * ag
        wconc[a] += ww * hg
        counts[a] += 1
    teams = list(counts)
    attack = dict.fromkeys(teams, 1.0)
    defence = dict.fromkeys(teams, 1.0)
    snapshots: dict[int, dict[int, TeamStrength]] = {}
    for it in range(1, max_iter + 1):
        denom_a: dict[int, float] = defaultdict(float)
        denom_d: dict[int, float] = defaultdict(float)
        for h, a, _hg, _ag, o in acc.matches:
            ww = w(o)
            denom_a[h] += ww * avg.home_goals * defence[a]
            denom_d[h] += ww * avg.away_goals * attack[a]
            denom_a[a] += ww * avg.away_goals * defence[h]
            denom_d[a] += ww * avg.home_goals * attack[h]
        attack = {t: _clamp(wscored[t] / denom_a[t]) if denom_a[t] > 0 else 1.0 for t in teams}
        defence = {t: _clamp(wconc[t] / denom_d[t]) if denom_d[t] > 0 else 1.0 for t in teams}
        snapshots[it] = {t: TeamStrength(t, attack[t], defence[t], counts[t]) for t in teams}
    return snapshots


def _grid_1x2(lam_h: float, lam_a: float, code: str | None) -> Probs:
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


def _walk(
    names: dict[int, str], rows: list[tuple], val_start: date, test_end: date
) -> list[Fixture]:
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
            ordinal = mdate.toordinal()
            if (
                len(acc.results) >= MIN_LEAGUE_RESULTS
                and acc.played(home_id) >= 5
                and acc.played(away_id) >= 5
                and val_start <= mdate < test_end
            ):
                avg = acc.averages()
                ch = _control_strength(acc, home_id, avg)
                ca = _control_strength(acc, away_id, avg)
                if ch.is_reliable and ca.is_reliable and avg.is_reliable:
                    cl_h, cl_a = expected_goals(ch, ca, avg)
                    control_total = cl_h + cl_a
                    control = _grid_1x2(cl_h, cl_a, code)
                    rh = _recency_strength(acc, home_id, avg, ordinal, HALF_LIFE)
                    ra = _recency_strength(acc, away_id, avg, ordinal, HALF_LIFE)
                    rl_h, rl_a = expected_goals(rh, ra, avg)
                    recency = _grid_1x2(rl_h, rl_a, code)
                    snaps = _adjusted_strengths(acc, avg, max_iter, ordinal, HALF_LIFE)
                    stack_norm: dict[int, Probs] = {}
                    for it, snap in snaps.items():
                        if home_id in snap and away_id in snap:
                            sl_h, sl_a = expected_goals(snap[home_id], snap[away_id], avg)
                            tot = sl_h + sl_a
                            if tot > 0:
                                scale = control_total / tot
                                stack_norm[it] = _grid_1x2(sl_h * scale, sl_a * scale, code)
                    out.append(
                        Fixture(
                            competition=name,
                            season=str(season or "?"),
                            ordinal=ordinal,
                            y=(int(hg > ag), int(hg == ag), int(hg < ag)),
                            control=control,
                            recency=recency,
                            stack_norm=stack_norm,
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


def _log_loss(fixtures: list[Fixture], pick) -> float:
    total = 0.0
    for f in fixtures:
        for p, y in zip(pick(f), f.y, strict=True):
            if y:
                total -= math.log(max(p, EPS))
    return total / len(fixtures) if fixtures else 0.0


def _ece(fixtures: list[Fixture], pick, bins: int = 10) -> float:
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for f in fixtures:
        for p, y in zip(pick(f), f.y, strict=True):
            buckets[min(bins - 1, int(p * bins))].append((p, y))
    n = sum(len(b) for b in buckets)
    ece = 0.0
    for b in buckets:
        if not b:
            continue
        mp = sum(p for p, _ in b) / len(b)
        my = sum(y for _, y in b) / len(b)
        ece += (len(b) / n) * abs(mp - my)
    return ece


def _paired_delta_ci(fixtures: list[Fixture], pick_a, pick_b) -> tuple[float, float, float]:
    deltas = [_brier_row(pick_a(f), f.y) - _brier_row(pick_b(f), f.y) for f in fixtures]
    rng = random.Random(20260920)
    size = len(deltas)
    means = []
    for _ in range(BOOTSTRAP):
        means.append(_mean([deltas[rng.randrange(size)] for _ in range(size)]))
    means.sort()
    return _mean(deltas), means[int(0.025 * BOOTSTRAP)], means[int(0.975 * BOOTSTRAP)]


def _group_delta(fixtures: list[Fixture], pick_a, pick_b, key) -> dict[str, tuple[int, float]]:
    groups: dict[str, list[Fixture]] = defaultdict(list)
    for f in fixtures:
        groups[key(f)].append(f)
    return {g: (len(fs), _brier(fs, pick_a) - _brier(fs, pick_b)) for g, fs in groups.items()}


def _verdict(lo: float, hi: float) -> str:
    if hi < 0:
        return "A better, CI excludes 0"
    if lo > 0:
        return "B better, CI excludes 0"
    return "indistinguishable (CI spans 0)"


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


async def _persist(session, stack_it: int, untouched: bool, metrics: dict) -> None:
    experiment_id = "EXP-009"
    existing = (
        await session.execute(
            select(ResearchExperiment).where(ResearchExperiment.experiment_id == experiment_id)
        )
    ).scalar_one_or_none()
    payload = {
        "title": "Totals-preserving stack (recency + opponent-adjusted ratio, control total)",
        "hypothesis": (
            "Keeping recency+opponent-adjusted ratio but the control's total-goals "
            "level captures the result gain without the goals-market regression."
        ),
        "status": "completed",
        "lane": "B",
        "control_version": CONTROL,
        "challenger_version": f"exp009-stack-norm-hl{int(HALF_LIFE)}-it{stack_it}",
        "features": {
            "grid_version": grid_version(),
            "half_life": HALF_LIFE,
            "iterations": list(ITERATIONS),
            "chosen_iterations": stack_it,
            "untouched_oos": untouched,
        },
        "metrics": metrics,
        "notes": (
            "Totals-preserving. Iterations selected on validation; test read once. "
            f"{'Untouched pre-EXP-002 OOS window.' if untouched else 'Standard newest-year test.'} "
            "Market-clean per full audit. Diagnostic, not promotion."
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
    parser.add_argument("--test-end", default=DEFAULT_TEST_END)
    parser.add_argument("--test-days", type=int, default=TEST_DAYS)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
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
        test_end = (
            datetime.strptime(args.test_end, "%Y-%m-%d").replace(tzinfo=UTC).date()
            if args.test_end
            else max_date
        )
        test_start = test_end - timedelta(days=args.test_days)
        val_start = test_start - timedelta(days=args.window_days)

        fixtures = _walk(names, rows, val_start, test_end)
        needed = set(ITERATIONS)
        fixtures = [f for f in fixtures if needed <= set(f.stack_norm)]
        validation = [f for f in fixtures if f.ordinal < test_start.toordinal()]
        test = [f for f in fixtures if f.ordinal >= test_start.toordinal()]
        if not validation or not test:
            print("Not enough fixtures to split validation/test.")
            await database.disconnect()
            return 1

        stack_val = {it: _brier(validation, lambda f, i=it: f.stack_norm[i]) for it in ITERATIONS}
        stack_it = min(stack_val, key=lambda it: stack_val[it])

        def pick_ctrl(f: Fixture) -> Probs:
            return f.control

        def pick_rec(f: Fixture) -> Probs:
            return f.recency

        def pick_stk(f: Fixture) -> Probs:
            return f.stack_norm[stack_it]

        b_ctrl = _brier(test, pick_ctrl)
        b_rec = _brier(test, pick_rec)
        b_stk = _brier(test, pick_stk)
        d_stk_ctrl = _paired_delta_ci(test, pick_stk, pick_ctrl)
        d_stk_rec = _paired_delta_ci(test, pick_stk, pick_rec)
        comp = _group_delta(test, pick_stk, pick_ctrl, lambda f: f.competition)
        helped = sum(1 for _, d in comp.values() if d < 0)
        season = _group_delta(test, pick_stk, pick_ctrl, lambda f: f.season)

        untouched = bool(args.test_end)
        print(RULE)
        print(f"EXP-009 — TOTALS-PRESERVING STACK  ({CONTROL}, {grid_version()})")
        print(RULE)
        era = " (UNTOUCHED pre-EXP-002 OOS)" if untouched else ""
        print(f"\n  validation {len(validation):,} · test {len(test):,} · test_end {test_end}{era}")
        print(f"  test starts {test_start} (never used for iteration selection)")
        print("\n  VALIDATION Brier by iterations (selection):")
        for it in ITERATIONS:
            mark = "  <- chosen" if it == stack_it else ""
            print(f"    it{it}: {stack_val[it]:.4f}{mark}")
        print(f"\n  TEST — chosen it{stack_it}:")
        print(f"    CTRL      {b_ctrl:.4f}")
        print(f"    EXP-002   {b_rec:.4f}  (recency hl{int(HALF_LIFE)})")
        print(f"    EXP-009   {b_stk:.4f}  (totals-preserving stack)")
        for label, (d, lo, hi) in (
            ("EXP-009 - CTRL", d_stk_ctrl),
            ("EXP-009 - EXP-002", d_stk_rec),
        ):
            print(f"    {label:<20} {d:+.5f}  CI [{lo:+.5f}, {hi:+.5f}]  {_verdict(lo, hi)}")
        ll_c, ll_s = _log_loss(test, pick_ctrl), _log_loss(test, pick_stk)
        ece_c, ece_s = _ece(test, pick_ctrl), _ece(test, pick_stk)
        print(f"    log loss  CTRL {ll_c:.4f}  EXP-009 {ll_s:.4f}")
        print(f"    ECE       CTRL {ece_c:.4f}  EXP-009 {ece_s:.4f}")
        print(f"    competition breadth vs CTRL: {helped}/{len(comp)}")

        d, lo, hi = d_stk_ctrl
        metrics = {
            "half_life": HALF_LIFE,
            "chosen_iterations": stack_it,
            "untouched_oos": untouched,
            "test_ctrl_brier": b_ctrl,
            "test_recency_brier": b_rec,
            "test_stack_norm_brier": b_stk,
            "stack_norm_vs_ctrl_delta": d,
            "stack_norm_vs_ctrl_ci": [lo, hi],
            "stack_norm_vs_recency_delta": d_stk_rec[0],
            "stack_norm_vs_recency_ci": [d_stk_rec[1], d_stk_rec[2]],
            "test_ctrl_log_loss": _log_loss(test, pick_ctrl),
            "test_stack_norm_log_loss": _log_loss(test, pick_stk),
            "test_ctrl_ece": _ece(test, pick_ctrl),
            "test_stack_norm_ece": _ece(test, pick_stk),
            "breadth_vs_ctrl": [helped, len(comp)],
            "delta_by_season_vs_ctrl": {s: {"n": n, "delta": dd} for s, (n, dd) in season.items()},
        }
        await _persist(session, stack_it, untouched, metrics)
        print(RULE)
        beats = hi < 0
        tag = "UNTOUCHED OOS" if untouched else "screening window"
        if beats:
            print(f"  RESULT ({tag}): EXP-009 beats CTRL on 1X2 (CI excludes 0).")
        else:
            print(f"  RESULT ({tag}): EXP-009 does NOT clear CTRL (CI does not exclude 0).")
        print("  Registered EXP-009. EXP-002/003/007/008 untouched. No production change.")
        print(RULE)
    await database.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
