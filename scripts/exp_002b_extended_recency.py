#!/usr/bin/env python3
"""EXP-002B — extended recency range, on a genuinely untouched test window.

EXP-002 found the recency optimum at the *edge* of its predeclared range (365d,
the longest value), so the curve was not bracketed. EXP-002B asks: **does the
optimum lie beyond 365 days, and is there a stable interior region rather than a
lucky single value?**

**Leakage-safe chronology — and it does not reuse EXP-002's test window.**
EXP-002 evaluated only fixtures dated on/after its ``since`` (2023-09-04); every
fixture *before* that date is untouched by it. EXP-002B lives entirely in that
earlier era:

    warmup  [ …            )   ingested into strength estimates, never scored
    validation [val_start, test_start)   half-life selected here, and only here
    test       [test_start, test_end)    ONE committed read; test_end <= 2023-09-04

So the committed final evaluation is a period EXP-002 never scored — a genuinely
fresh holdout — and selection touches validation only. The present-era
confirmation for whichever half-life wins is the forward shadow (EXP-002-365d and,
if this promotes a different value, its own shadow), not a re-read of any window
already observed.

Predeclared range: 365 / 540 / 730 / 1095 days. The report is the full curve plus
paired Brier delta, log-loss delta, ECE, H/D/A bias, probability bands,
competition breadth, per-season temporal stability, per-fold optimum stability,
and bootstrap uncertainty. Frozen control unchanged; the only write is one
summary row in ``research_experiments``. Diagnostic, never promotion.

Run::

    railway ssh "python scripts/exp_002b_extended_recency.py"
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
HALF_LIVES: tuple[float, ...] = (365.0, 540.0, 730.0, 1095.0)  # predeclared, in days
MIN_LEAGUE_RESULTS = 20
# EXP-002 evaluated only fixtures on/after this date; ending the test strictly
# before it keeps EXP-002B's committed holdout genuinely untouched.
DEFAULT_TEST_END = "2023-09-04"
TEST_DAYS = 365
VAL_DAYS = 730
BOOTSTRAP = 400
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
    recency: dict[float, Probs] = field(default_factory=dict)


@dataclass
class Row:
    """One scored fixture under a chosen variant: model 1X2 vs realised outcome."""

    competition: str
    season: str
    p: Probs
    y: tuple[int, int, int]


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


def _walk(
    names: dict[int, str], rows: list[tuple], val_start: date, test_end: date
) -> list[Fixture]:
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
                and val_start <= mdate < test_end
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


def _rows(fixtures: list[Fixture], pick) -> list[Row]:
    return [Row(f.competition, f.season, pick(f), f.y) for f in fixtures]


def _brier_row(p: Probs, y: tuple[int, int, int]) -> float:
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y, strict=True))


def _brier(rows: list[Row]) -> float:
    return sum(_brier_row(r.p, r.y) for r in rows) / len(rows) if rows else 0.0


def _log_loss(rows: list[Row]) -> float:
    total = 0.0
    for r in rows:
        for p, y in zip(r.p, r.y, strict=True):
            if y:
                total -= math.log(max(p, EPS))
    return total / len(rows) if rows else 0.0


def _ece(rows: list[Row], bins: int = 10) -> float:
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for r in rows:
        for p, y in zip(r.p, r.y, strict=True):
            buckets[min(bins - 1, int(p * bins))].append((p, y))
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


def _paired_delta_ci(
    fixtures: list[Fixture], hl: float, n: int = BOOTSTRAP
) -> tuple[float, float, float]:
    deltas = [_brier_row(f.recency[hl], f.y) - _brier_row(f.control, f.y) for f in fixtures]
    rng = random.Random(20260920)
    size = len(deltas)
    means = []
    for _ in range(n):
        means.append(sum(deltas[rng.randrange(size)] for _ in range(size)) / size)
    means.sort()
    mean = sum(deltas) / size
    return mean, means[int(0.025 * n)], means[int(0.975 * n)]


def _fold_optima(fixtures: list[Fixture], folds: int = 4) -> list[tuple[str, float]]:
    """Argmin half-life per chronological validation fold — interior stability."""
    ordered = sorted(fixtures, key=lambda f: f.ordinal)
    size = len(ordered)
    out: list[tuple[str, float]] = []
    for i in range(folds):
        chunk = ordered[i * size // folds : (i + 1) * size // folds]
        if not chunk:
            continue
        briers = {hl: _brier(_rows(chunk, lambda f, h=hl: f.recency[h])) for hl in HALF_LIVES}
        best = min(briers, key=lambda hl: briers[hl])
        span = f"{ordered[i * size // folds].ordinal}"
        out.append((span, best))
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
    experiment_id = "EXP-002B"
    existing = (
        await session.execute(
            select(ResearchExperiment).where(ResearchExperiment.experiment_id == experiment_id)
        )
    ).scalar_one_or_none()
    payload = {
        "title": "Extended recency range (boundary investigation)",
        "hypothesis": (
            "The recency optimum lies beyond 365 days with a stable interior region, "
            "not a boundary or lucky single value."
        ),
        "status": "completed",
        "lane": "B",
        "control_version": CONTROL,
        "challenger_version": f"exp002b-recency-hl{int(chosen)}",
        "features": {
            "grid_version": grid_version(),
            "half_lives": list(HALF_LIVES),
            "chosen": chosen,
        },
        "metrics": metrics,
        "notes": (
            "Untouched chronology: test window ends before EXP-002's evaluation "
            "era, selection on validation only. Diagnostic, not promotion."
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
    parser.add_argument("--val-days", type=int, default=VAL_DAYS)
    args = parser.parse_args()

    test_end = datetime.strptime(args.test_end, "%Y-%m-%d").replace(tzinfo=UTC).date()
    test_start = test_end - timedelta(days=args.test_days)
    val_start = test_start - timedelta(days=args.val_days)

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        names, rows = await _load(session)
        if not rows:
            print("No historical matches.")
            await database.disconnect()
            return 1
        fixtures = _walk(names, rows, val_start, test_end)
        validation = [f for f in fixtures if f.ordinal < test_start.toordinal()]
        test = [f for f in fixtures if f.ordinal >= test_start.toordinal()]
        if not validation or not test:
            print(
                f"Not enough fixtures in the untouched era "
                f"(validation {len(validation):,}, test {len(test):,}). "
                "Widen --val-days/--test-days or check corpus depth before 2023-09."
            )
            await database.disconnect()
            return 1

        # Select the half-life on validation only.
        val_brier = {
            hl: _brier(_rows(validation, lambda f, h=hl: f.recency[h])) for hl in HALF_LIVES
        }
        val_control = _brier(_rows(validation, lambda f: f.control))
        chosen = min(val_brier, key=lambda hl: val_brier[hl])
        interior = chosen not in (HALF_LIVES[0], HALF_LIVES[-1])
        folds = _fold_optima(validation)

        # One committed read on the untouched test window.
        ctrl_rows = _rows(test, lambda f: f.control)
        rec_rows = _rows(test, lambda f, h=chosen: f.recency[h])
        c_brier, r_brier = _brier(ctrl_rows), _brier(rec_rows)
        delta, lo, hi = _paired_delta_ci(test, chosen)
        c_ll, r_ll = _log_loss(ctrl_rows), _log_loss(rec_rows)
        c_ece, r_ece = _ece(ctrl_rows), _ece(rec_rows)
        c_bias, r_bias = _hda_bias(ctrl_rows), _hda_bias(rec_rows)
        by_comp_c = _group_brier(ctrl_rows, lambda r: r.competition)
        by_comp_r = _group_brier(rec_rows, lambda r: r.competition)
        helped = sum(1 for c in by_comp_c if c in by_comp_r and by_comp_r[c][1] < by_comp_c[c][1])
        by_season_c = _group_brier(ctrl_rows, lambda r: r.season)
        by_season_r = _group_brier(rec_rows, lambda r: r.season)

        print(RULE)
        print(f"EXP-002B — EXTENDED RECENCY RANGE  ({CONTROL}, {grid_version()})")
        print(RULE)
        print(f"\n  untouched era — validation [{val_start}, {test_start})")
        print(f"                 test       [{test_start}, {test_end})  (test_end < EXP-002 since)")
        print(f"  validation {len(validation):,} fixtures · test {len(test):,} fixtures")
        print("\n  VALIDATION Brier by half-life (selection set):")
        print(f"    control (equal weight) : {val_control:.4f}")
        for hl in HALF_LIVES:
            mark = "  <- chosen" if hl == chosen else ""
            print(f"    half-life {int(hl):>4}d    : {val_brier[hl]:.4f}{mark}")
        print(f"    optimum is {'INTERIOR' if interior else 'AT A BOUNDARY'} of the range")
        print("\n  per-fold argmin half-life (chronological, interior-stability):")
        for span, best in folds:
            print(f"    fold @ord {span} : {int(best)}d")

        print(f"\n  TEST (untouched) — chosen half-life {int(chosen)}d:")
        print(f"    Brier     control {c_brier:.4f}  recency {r_brier:.4f}")
        print(f"    paired Δ  {delta:+.5f}   bootstrap 95% CI [{lo:+.5f}, {hi:+.5f}]")
        verdict = (
            "challenger better, CI excludes 0"
            if hi < 0
            else "control better, CI excludes 0"
            if lo > 0
            else "indistinguishable (CI spans 0)"
        )
        print(f"    verdict   {verdict}")
        print(f"    log loss  control {c_ll:.4f}  recency {r_ll:.4f}   Δ {r_ll - c_ll:+.5f}")
        print(f"    ECE       control {c_ece:.4f}  recency {r_ece:.4f}")
        print(
            "    H/D/A bias (pred-obs)  "
            f"control H{c_bias['home']:+.3f} D{c_bias['draw']:+.3f} A{c_bias['away']:+.3f}  |  "
            f"recency H{r_bias['home']:+.3f} D{r_bias['draw']:+.3f} A{r_bias['away']:+.3f}"
        )
        print(f"    competition breadth: recency helped {helped}/{len(by_comp_c)} competitions")
        print("\n  recency probability bands (band, n, mean p, empirical):")
        for label, n, mp, my in _bands(rec_rows):
            print(f"    {label:>8}  n={n:>6,}  p={mp:.3f}  obs={my:.3f}")

        metrics = {
            "validation_control_brier": val_control,
            "validation_brier_by_half_life": val_brier,
            "chosen_half_life": chosen,
            "optimum_interior": interior,
            "fold_optima": [b for _, b in folds],
            "test_control_brier": c_brier,
            "test_recency_brier": r_brier,
            "test_paired_delta": delta,
            "test_delta_ci": [lo, hi],
            "test_control_log_loss": c_ll,
            "test_recency_log_loss": r_ll,
            "test_control_ece": c_ece,
            "test_recency_ece": r_ece,
            "test_competitions_helped": helped,
            "test_competitions_total": len(by_comp_c),
            "delta_by_season": {
                s: {
                    "n": by_season_c[s][0],
                    "control": by_season_c[s][1],
                    "recency": by_season_r.get(s, (0, 0.0))[1],
                }
                for s in by_season_c
            },
        }
        await _persist(session, chosen, metrics)
        print("\n  Registered EXP-002B in research_experiments. No other writes.")
        print(RULE)
    await database.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
