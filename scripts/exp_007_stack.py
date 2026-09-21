#!/usr/bin/env python3
"""EXP-007 — combined strength stack: recency + opponent adjustment.

Four arms scored on **identical chronological fixtures**:

* CTRL  — frozen ``model-only-v2-dc`` (equal weight, opponent-agnostic)
* EXP-002 — recency-weighted (half-life 365d)
* EXP-003 — opponent-adjusted (fixed point)
* STACK — recency-weighted **and** opponent-adjusted in one estimator

The primary question is not "does STACK beat CTRL" — it is **does STACK beat
EXP-002**, our strongest single challenger. Recency is the lever to clear.

**Predeclared, before the untouched test window is read:**
* recency half-life is fixed at 365d (the value EXP-002 already selected — not
  re-searched here);
* the opponent-adjustment iteration count for the EXP-003 arm and for the STACK
  arm is selected on the *validation* window only, from the predeclared set
  {1, 2, 3};
* the test window (newest ``TEST_DAYS``) is read exactly once, for the
  validation-selected configuration. No post-test tuning.

Note: the newest-year test window has already been observed by EXP-002/EXP-003,
so the STACK's *edge over those two* is the genuinely new reading here; the
clean prospective arbiter remains the forward shadow and the market audit. Pass
``--test-end 2023-09-04`` to instead evaluate on the pre-EXP-002 untouched era
as a robustness check.

Frozen control unchanged; EXP-002 and EXP-003 records are left exactly as they
are; the only write is one summary row in ``research_experiments`` (EXP-007).
Diagnostic, never promotion. Runs heavier than EXP-002 (two fixed points per
fixture) — expect minutes.

Run::

    railway ssh "python scripts/exp_007_stack.py"
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
HALF_LIFE = 365.0  # predeclared, fixed (EXP-002's selected value)
ITERATIONS: tuple[int, ...] = (1, 2, 3)  # predeclared, selected on validation
MIN_LEAGUE_RESULTS = 20
DEFAULT_WINDOW_DAYS = 1095
DEFAULT_TEST_END: str | None = None  # None -> newest TEST_DAYS; or "YYYY-MM-DD"
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
    oppadj: dict[int, Probs] = field(default_factory=dict)
    combined: dict[int, Probs] = field(default_factory=dict)


@dataclass
class Accumulator:
    matches: list[tuple[int, int, int, int, int]] = field(default_factory=list)  # h,a,hg,ag,ord
    home: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    away: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    dated: dict[int, list[tuple[int, int, int]]] = field(default_factory=dict)  # (gf,ga,ord)
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
    acc: Accumulator, avg: LeagueAverages, max_iter: int, ref: int | None, half_life: float | None
) -> dict[int, dict[int, TeamStrength]]:
    """Opponent-adjusted fixed point; recency-weighted when ``half_life`` given.

    With ``half_life`` None this is EXP-003 (equal weight). With a half-life it is
    the STACK: every match's contribution to both the scored/conceded totals and
    the opponent-strength denominators is scaled by the same exponential decay,
    so opponent adjustment and recency act in one estimator.
    """

    def weight(o: int) -> float:
        if half_life is None or ref is None:
            return 1.0
        return 0.5 ** ((ref - o) / half_life)

    wscored: dict[int, float] = defaultdict(float)
    wconc: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    for h, a, hg, ag, o in acc.matches:
        w = weight(o)
        wscored[h] += w * hg
        wconc[h] += w * ag
        counts[h] += 1
        wscored[a] += w * ag
        wconc[a] += w * hg
        counts[a] += 1
    teams = list(counts)
    attack = dict.fromkeys(teams, 1.0)
    defence = dict.fromkeys(teams, 1.0)
    snapshots: dict[int, dict[int, TeamStrength]] = {}
    for it in range(1, max_iter + 1):
        denom_a: dict[int, float] = defaultdict(float)
        denom_d: dict[int, float] = defaultdict(float)
        for h, a, _hg, _ag, o in acc.matches:
            w = weight(o)
            denom_a[h] += w * avg.home_goals * defence[a]
            denom_d[h] += w * avg.away_goals * attack[a]
            denom_a[a] += w * avg.away_goals * defence[h]
            denom_d[a] += w * avg.home_goals * attack[h]
        attack = {t: _clamp(wscored[t] / denom_a[t]) if denom_a[t] > 0 else 1.0 for t in teams}
        defence = {t: _clamp(wconc[t] / denom_d[t]) if denom_d[t] > 0 else 1.0 for t in teams}
        snapshots[it] = {t: TeamStrength(t, attack[t], defence[t], counts[t]) for t in teams}
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
                    control = _grid_1x2(ch, ca, avg, code)
                    recency = _grid_1x2(
                        _recency_strength(acc, home_id, avg, ordinal, HALF_LIFE),
                        _recency_strength(acc, away_id, avg, ordinal, HALF_LIFE),
                        avg,
                        code,
                    )
                    oppadj_snaps = _adjusted_strengths(acc, avg, max_iter, None, None)
                    stack_snaps = _adjusted_strengths(acc, avg, max_iter, ordinal, HALF_LIFE)
                    oppadj = {
                        it: _grid_1x2(s[home_id], s[away_id], avg, code)
                        for it, s in oppadj_snaps.items()
                        if home_id in s and away_id in s
                    }
                    combined = {
                        it: _grid_1x2(s[home_id], s[away_id], avg, code)
                        for it, s in stack_snaps.items()
                        if home_id in s and away_id in s
                    }
                    out.append(
                        Fixture(
                            competition=name,
                            season=str(season or "?"),
                            ordinal=ordinal,
                            y=(int(hg > ag), int(hg == ag), int(hg < ag)),
                            control=control,
                            recency=recency,
                            oppadj=oppadj,
                            combined=combined,
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


def _bands(fixtures: list[Fixture], pick, bins: int = 10) -> list[tuple[str, int, float, float]]:
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for f in fixtures:
        for p, y in zip(pick(f), f.y, strict=True):
            buckets[min(bins - 1, int(p * bins))].append((p, y))
    out = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        mp = sum(p for p, _ in b) / len(b)
        my = sum(y for _, y in b) / len(b)
        out.append((f"{i * 10:>2}-{i * 10 + 10}%", len(b), mp, my))
    return out


def _hda_bias(fixtures: list[Fixture], pick) -> dict[str, float]:
    n = len(fixtures)
    return {
        o: (sum(pick(f)[i] for f in fixtures) - sum(f.y[i] for f in fixtures)) / n
        for i, o in enumerate(OUTCOMES)
    }


def _paired_delta_ci(
    fixtures: list[Fixture], pick_a, pick_b, n: int = BOOTSTRAP
) -> tuple[float, float, float]:
    """Paired Brier delta a-b (negative => a better), with bootstrap 95% CI."""
    deltas = [_brier_row(pick_a(f), f.y) - _brier_row(pick_b(f), f.y) for f in fixtures]
    rng = random.Random(20260920)
    size = len(deltas)
    means = []
    for _ in range(n):
        means.append(_mean([deltas[rng.randrange(size)] for _ in range(size)]))
    means.sort()
    return _mean(deltas), means[int(0.025 * n)], means[int(0.975 * n)]


def _group_delta(fixtures: list[Fixture], pick_a, pick_b, key) -> dict[str, tuple[int, float]]:
    groups: dict[str, list[Fixture]] = defaultdict(list)
    for f in fixtures:
        groups[key(f)].append(f)
    return {
        g: (len(fs), _brier(fs, pick_a) - _brier(fs, pick_b)) for g, fs in groups.items()
    }


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


async def _persist(session, oppadj_it: int, stack_it: int, metrics: dict) -> None:
    experiment_id = "EXP-007"
    existing = (
        await session.execute(
            select(ResearchExperiment).where(ResearchExperiment.experiment_id == experiment_id)
        )
    ).scalar_one_or_none()
    payload = {
        "title": "Combined strength stack (recency + opponent-adjusted)",
        "hypothesis": (
            "Stacking recency weighting and opponent adjustment beats EXP-002 "
            "(recency alone), our strongest single challenger."
        ),
        "status": "completed",
        "lane": "B",
        "control_version": CONTROL,
        "challenger_version": f"exp007-stack-hl{int(HALF_LIFE)}-it{stack_it}",
        "features": {
            "grid_version": grid_version(),
            "half_life": HALF_LIFE,
            "iterations": list(ITERATIONS),
            "chosen_stack_iterations": stack_it,
            "chosen_oppadj_iterations": oppadj_it,
        },
        "metrics": metrics,
        "notes": (
            "Primary test is STACK vs EXP-002 on identical fixtures. Iterations "
            "selected on validation; test read once. EXP-002/EXP-003 untouched."
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
        fixtures = [f for f in fixtures if needed <= set(f.oppadj) and needed <= set(f.combined)]
        validation = [f for f in fixtures if f.ordinal < test_start.toordinal()]
        test = [f for f in fixtures if f.ordinal >= test_start.toordinal()]
        if not validation or not test:
            print("Not enough fixtures to split validation/test.")
            await database.disconnect()
            return 1

        # Selection on validation only.
        oppadj_val = {it: _brier(validation, lambda f, i=it: f.oppadj[i]) for it in ITERATIONS}
        stack_val = {it: _brier(validation, lambda f, i=it: f.combined[i]) for it in ITERATIONS}
        oppadj_it = min(oppadj_val, key=lambda it: oppadj_val[it])
        stack_it = min(stack_val, key=lambda it: stack_val[it])

        def pick_ctrl(f: Fixture) -> Probs:
            return f.control

        def pick_rec(f: Fixture) -> Probs:
            return f.recency

        def pick_opp(f: Fixture) -> Probs:
            return f.oppadj[oppadj_it]

        def pick_stk(f: Fixture) -> Probs:
            return f.combined[stack_it]

        b_ctrl = _brier(test, pick_ctrl)
        b_rec = _brier(test, pick_rec)
        b_opp = _brier(test, pick_opp)
        b_stk = _brier(test, pick_stk)

        # Paired deltas (a - b; negative => a better).
        d_stk_ctrl = _paired_delta_ci(test, pick_stk, pick_ctrl)
        d_stk_rec = _paired_delta_ci(test, pick_stk, pick_rec)  # PRIMARY
        d_stk_opp = _paired_delta_ci(test, pick_stk, pick_opp)
        d_rec_ctrl = _paired_delta_ci(test, pick_rec, pick_ctrl)
        d_opp_ctrl = _paired_delta_ci(test, pick_opp, pick_ctrl)

        comp_stk_rec = _group_delta(test, pick_stk, pick_rec, lambda f: f.competition)
        helped_vs_rec = sum(1 for _, d in comp_stk_rec.values() if d < 0)
        comp_stk_ctrl = _group_delta(test, pick_stk, pick_ctrl, lambda f: f.competition)
        helped_vs_ctrl = sum(1 for _, d in comp_stk_ctrl.values() if d < 0)
        season_stk_rec = _group_delta(test, pick_stk, pick_rec, lambda f: f.season)

        print(RULE)
        print(f"EXP-007 — STACK (recency hl{int(HALF_LIFE)} + opponent-adjusted)  {grid_version()}")
        print(RULE)
        era = " (UNTOUCHED pre-EXP-002 era)" if args.test_end else ""
        window_note = f"test_end {test_end}{era}"
        print(f"\n  validation {len(validation):,} · test {len(test):,} · {window_note}")
        print(f"  test window starts {test_start} (never used for iteration selection)")
        print("\n  VALIDATION Brier (selection):")
        print("    oppadj  " + "  ".join(f"it{it}:{oppadj_val[it]:.4f}" for it in ITERATIONS)
              + f"   -> chosen it{oppadj_it}")
        print("    stack   " + "  ".join(f"it{it}:{stack_val[it]:.4f}" for it in ITERATIONS)
              + f"   -> chosen it{stack_it}")

        print("\n  TEST (held out) — Brier by arm:")
        print(f"    CTRL     {b_ctrl:.4f}")
        print(f"    EXP-002  {b_rec:.4f}  (recency hl{int(HALF_LIFE)})")
        print(f"    EXP-003  {b_opp:.4f}  (oppadj it{oppadj_it})")
        print(f"    STACK    {b_stk:.4f}  (hl{int(HALF_LIFE)} + oppadj it{stack_it})")

        print("\n  Paired Brier deltas (negative => first is better):")
        for label, (d, lo, hi) in (
            ("STACK - EXP-002  (PRIMARY)", d_stk_rec),
            ("STACK - CTRL", d_stk_ctrl),
            ("STACK - EXP-003", d_stk_opp),
            ("EXP-002 - CTRL", d_rec_ctrl),
            ("EXP-003 - CTRL", d_opp_ctrl),
        ):
            print(f"    {label:<28} {d:+.5f}  CI [{lo:+.5f}, {hi:+.5f}]  {_verdict(lo, hi)}")

        ll_rec, ll_stk = _log_loss(test, pick_rec), _log_loss(test, pick_stk)
        ece_rec, ece_stk = _ece(test, pick_rec), _ece(test, pick_stk)
        print("\n  STACK vs EXP-002 — calibration & shape on test:")
        print(f"    log loss   EXP-002 {ll_rec:.4f}  STACK {ll_stk:.4f}")
        print(f"    ECE        EXP-002 {ece_rec:.4f}  STACK {ece_stk:.4f}")
        sb = _hda_bias(test, pick_stk)
        rb = _hda_bias(test, pick_rec)
        print(f"    H/D/A bias EXP-002 H{rb['home']:+.3f} D{rb['draw']:+.3f} A{rb['away']:+.3f}")
        print(f"               STACK   H{sb['home']:+.3f} D{sb['draw']:+.3f} A{sb['away']:+.3f}")
        print(f"    competition breadth: STACK beat EXP-002 in {helped_vs_rec}/{len(comp_stk_rec)}"
              f" · beat CTRL in {helped_vs_ctrl}/{len(comp_stk_ctrl)}")
        print("    largest regressions vs EXP-002 (Δ>0 = STACK worse):")
        worst = sorted(comp_stk_rec.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
        for comp, (nc, d) in worst:
            print(f"      {comp[:34]:<36} n={nc:>6,}  Δ {d:+.4f}")
        print("\n  STACK probability bands (band, n, mean p, empirical):")
        for lab, n, mp, my in _bands(test, pick_stk):
            print(f"    {lab:>8}  n={n:>6,}  p={mp:.3f}  obs={my:.3f}")

        d, lo, hi = d_stk_rec
        beats_recency = hi < 0
        metrics = {
            "half_life": HALF_LIFE,
            "chosen_stack_iterations": stack_it,
            "chosen_oppadj_iterations": oppadj_it,
            "test_brier": {"ctrl": b_ctrl, "exp002": b_rec, "exp003": b_opp, "stack": b_stk},
            "stack_vs_exp002_delta": d,
            "stack_vs_exp002_ci": [lo, hi],
            "stack_beats_exp002": beats_recency,
            "stack_vs_ctrl_delta": d_stk_ctrl[0],
            "stack_vs_ctrl_ci": [d_stk_ctrl[1], d_stk_ctrl[2]],
            "stack_vs_exp003_delta": d_stk_opp[0],
            "test_log_loss": {"exp002": ll_rec, "stack": ll_stk},
            "test_ece": {"exp002": ece_rec, "stack": ece_stk},
            "breadth_stack_beats_exp002": [helped_vs_rec, len(comp_stk_rec)],
            "delta_by_season_stack_vs_exp002": {
                s: {"n": n, "delta": dd} for s, (n, dd) in season_stk_rec.items()
            },
        }
        await _persist(session, oppadj_it, stack_it, metrics)

        print(RULE)
        if beats_recency:
            print("  RESULT: STACK beats EXP-002 on held-out test (CI excludes 0).")
            print("  -> proceed to FULL MARKET IMPACT AUDIT before any promotion consideration.")
        else:
            print("  RESULT: STACK does NOT clear EXP-002 (CI does not exclude 0 favourably).")
            print("  -> recency remains the strongest challenger; no market audit warranted yet.")
        print("  Registered EXP-007. EXP-002/EXP-003 untouched. No production change.")
        print(RULE)
    await database.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
