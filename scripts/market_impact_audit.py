#!/usr/bin/env python3
"""FULL MARKET IMPACT AUDIT — champion vs candidate across every market.

A 1X2 improvement does not mean every grid-derived market improved. This audit
measures it directly. For each settled fixture it builds the champion grid
(frozen ``model-only-v2-dc``: equal-weight, opponent-agnostic) and the candidate
grid (EXP-007 STACK: recency half-life 365 + opponent-adjusted, 3 iterations),
then derives **every registered market from the canonical predicates**
(``app.quant.markets``) — no market-specific probability hack — and scores each
one against the realised result.

Reported per market outcome:
* binary Brier for champion vs candidate, the paired delta and a bootstrap CI
  (negative delta => candidate better; a positive delta whose CI excludes zero
  is a **regression** and is flagged);
* calibration (ECE) for both;
* sample size and per-competition breadth (where the candidate improved).

Reported per registered service (its market/outcome + qualification threshold):
* how many fixtures qualify under each model, and the selections **gained**
  (candidate qualifies, champion did not) and **lost** (vice-versa);
* the realised hit-rate of the gained and lost selections — whether the changes
  the candidate makes are good ones.

Read-only. No table is written; production is untouched. This is evidence for
the promotion gate, not a promotion.

Run::

    railway ssh "python scripts/market_impact_audit.py"
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.competitions import code_for_name
from app.core.config import get_settings
from app.infrastructure.database import Database
from app.quant.grid import build_grid, grid_version
from app.quant.markets import MARKETS, MarketDefinition
from app.quant.poisson import LeagueAverages, TeamStrength, expected_goals, team_strength
from app.services.best_of_day import SERVICES

CHAMPION = "model-only-v2-dc"
HALF_LIFE = 365.0
# Candidate configs: mode -> {label, kind, iterations?, half_life?}.
#   kind "recency"      — opponent-agnostic recency (EXP-002)
#   kind "adjusted"     — opponent-adjusted fixed point; recency-weighted if half_life set
#   kind "recency_norm" — recency RATIO, control TOTAL (EXP-008 totals-preserving)
CANDIDATES = {
    "stack": {"label": "exp007-stack-hl365-it3", "kind": "adjusted", "iterations": 3,
              "half_life": HALF_LIFE},
    "recency": {"label": "exp002-recency-hl365", "kind": "recency", "half_life": HALF_LIFE},
    "oppadj": {"label": "exp003-oppadj-it2", "kind": "adjusted", "iterations": 2,
               "half_life": None},
    "recency_norm": {"label": "exp008-recency-norm-hl365", "kind": "recency_norm",
                     "half_life": HALF_LIFE},
    # EXP-009: both clean levers — recency+opponent-adjusted RATIO, control TOTAL.
    "stack_norm": {"label": "exp009-stack-norm-hl365-it3", "kind": "adjusted_norm",
                   "iterations": 3, "half_life": HALF_LIFE},
}
RULE = "=" * 92
MIN_LEAGUE_RESULTS = 20
DEFAULT_WINDOW_DAYS = 1095
TEST_DAYS = 365
BOOTSTRAP = 400
RATIO_FLOOR, RATIO_CEIL = 0.2, 5.0
SUPPORTED = tuple(d for d in MARKETS if d.supported)


@dataclass
class Scored:
    """One settled fixture's champion/candidate probability per market key."""

    competition: str
    season: str
    champ: dict[tuple[str, str], float]
    cand: dict[tuple[str, str], float]
    won: dict[tuple[str, str], int]


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
    """Opponent-agnostic recency-weighted strength (the EXP-002 estimator)."""
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
    acc: Accumulator, avg: LeagueAverages, iterations: int, ref: int, half_life: float | None
) -> dict[int, TeamStrength]:
    """Opponent-adjusted fixed point; recency-weighted when ``half_life`` given."""

    def w(o: int) -> float:
        if half_life is None:
            return 1.0
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
    for _it in range(iterations):
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
    return {t: TeamStrength(t, attack[t], defence[t], counts[t]) for t in teams}


def _market_probs(
    hs: TeamStrength, as_: TeamStrength, avg: LeagueAverages, code: str | None
) -> dict[tuple[str, str], float]:
    lam_h, lam_a = expected_goals(hs, as_, avg)
    return _market_probs_from_lambda(lam_h, lam_a, code)


def _market_probs_from_lambda(
    lam_h: float, lam_a: float, code: str | None
) -> dict[tuple[str, str], float]:
    grid = build_grid(lam_h, lam_a, corrected=True, competition=code)
    out: dict[tuple[str, str], float] = {}
    for d in SUPPORTED:
        out[d.key] = float(sum((p for (h, a), p in grid.items() if d.holds(h, a)), Decimal(0)))
    return out


def _candidate_lambdas(
    acc: Accumulator, avg: LeagueAverages, ch: TeamStrength, ca: TeamStrength,
    home_id: int, away_id: int, ordinal: int, config: dict,
) -> tuple[float, float] | None:
    """Candidate (lambda_home, lambda_away) for the chosen mode, or None.

    ``*_norm`` kinds keep the candidate's ratio but rescale the total to the
    calibrated control's total — recency/opponent adjustment for *who wins*,
    control for *how many goals*.
    """
    kind = config["kind"]
    half_life = config.get("half_life")
    normalize = kind.endswith("_norm")
    base = kind[:-5] if normalize else kind

    if base == "recency":
        assert half_life is not None
        hs = _recency_strength(acc, home_id, avg, ordinal, half_life)
        as_ = _recency_strength(acc, away_id, avg, ordinal, half_life)
        if not (hs.is_reliable and as_.is_reliable):
            return None
        lam_h, lam_a = expected_goals(hs, as_, avg)
    else:  # "adjusted" — opponent-adjusted, recency-weighted iff half_life set
        strengths = _adjusted_strengths(acc, avg, config["iterations"], ordinal, half_life)
        if home_id not in strengths or away_id not in strengths:
            return None
        lam_h, lam_a = expected_goals(strengths[home_id], strengths[away_id], avg)

    if normalize:
        cl_h, cl_a = expected_goals(ch, ca, avg)
        total = lam_h + lam_a
        if total <= 0:
            return None
        scale = (cl_h + cl_a) / total
        lam_h, lam_a = lam_h * scale, lam_a * scale
    return lam_h, lam_a


def _walk(
    names: dict[int, str], rows: list[tuple], since: date, cutoff: date, config: dict,
) -> list[Scored]:
    by_comp: dict[int, list[tuple]] = defaultdict(list)
    for r in rows:
        by_comp[int(r[0])].append(r)

    out: list[Scored] = []
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
                and mdate >= cutoff
            ):
                avg = acc.averages()
                ch = _control_strength(acc, home_id, avg)
                ca = _control_strength(acc, away_id, avg)
                if ch.is_reliable and ca.is_reliable and avg.is_reliable:
                    cand_lams = _candidate_lambdas(
                        acc, avg, ch, ca, home_id, away_id, ordinal, config
                    )
                    if cand_lams is not None:
                        champ = _market_probs(ch, ca, avg, code)
                        cand = _market_probs_from_lambda(cand_lams[0], cand_lams[1], code)
                        won = {d.key: int(d.holds(hg, ag)) for d in SUPPORTED}
                        out.append(Scored(name, str(season or "?"), champ, cand, won))
            acc.record(home_id, away_id, hg, ag, ordinal)
    return out


def _brier(rows: list[Scored], key: tuple[str, str], model: str) -> float:
    pick = (lambda r: r.champ) if model == "champ" else (lambda r: r.cand)
    return sum((pick(r)[key] - r.won[key]) ** 2 for r in rows) / len(rows)


def _ece(rows: list[Scored], key: tuple[str, str], model: str, bins: int = 10) -> float:
    pick = (lambda r: r.champ) if model == "champ" else (lambda r: r.cand)
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for r in rows:
        p = pick(r)[key]
        buckets[min(bins - 1, int(p * bins))].append((p, r.won[key]))
    n = sum(len(b) for b in buckets)
    ece = 0.0
    for b in buckets:
        if not b:
            continue
        mp = sum(p for p, _ in b) / len(b)
        my = sum(y for _, y in b) / len(b)
        ece += (len(b) / n) * abs(mp - my)
    return ece


def _paired_ci(rows: list[Scored], key: tuple[str, str]) -> tuple[float, float, float]:
    deltas = [
        (r.cand[key] - r.won[key]) ** 2 - (r.champ[key] - r.won[key]) ** 2 for r in rows
    ]
    rng = random.Random(20260920)
    size = len(deltas)
    means = []
    for _ in range(BOOTSTRAP):
        means.append(sum(deltas[rng.randrange(size)] for _ in range(size)) / size)
    means.sort()
    mean = sum(deltas) / size
    return mean, means[int(0.025 * BOOTSTRAP)], means[int(0.975 * BOOTSTRAP)]


def _competition_breadth(rows: list[Scored], key: tuple[str, str]) -> tuple[int, int]:
    by_comp: dict[str, list[Scored]] = defaultdict(list)
    for r in rows:
        by_comp[r.competition].append(r)
    better = 0
    for fs in by_comp.values():
        c = sum((f.champ[key] - f.won[key]) ** 2 for f in fs) / len(fs)
        d = sum((f.cand[key] - f.won[key]) ** 2 for f in fs) / len(fs)
        if d < c:
            better += 1
    return better, len(by_comp)


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


def _verdict(lo: float, hi: float) -> str:
    if hi < 0:
        return "improved"
    if lo > 0:
        return "REGRESSED"
    return "no change"


def _service_report(rows: list[Scored], service, defn: MarketDefinition) -> dict:
    key = defn.key
    thr = service.min_probability
    q_champ = [r for r in rows if r.champ[key] >= thr]
    q_cand = [r for r in rows if r.cand[key] >= thr]
    champ_set = {id(r) for r in q_champ}
    cand_set = {id(r) for r in q_cand}
    gained = [r for r in q_cand if id(r) not in champ_set]
    lost = [r for r in q_champ if id(r) not in cand_set]

    def hit_rate(rs: list[Scored]) -> float | None:
        return (sum(r.won[key] for r in rs) / len(rs)) if rs else None

    return {
        "q_champ": len(q_champ),
        "q_cand": len(q_cand),
        "gained": len(gained),
        "lost": len(lost),
        "hit_champ": hit_rate(q_champ),
        "hit_cand": hit_rate(q_cand),
        "hit_gained": hit_rate(gained),
        "hit_lost": hit_rate(lost),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-days", type=int, default=TEST_DAYS)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--candidate", choices=sorted(CANDIDATES), default="stack")
    args = parser.parse_args()
    config = CANDIDATES[args.candidate]
    candidate_label = config["label"]

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        names, rows = await _load(session)
        if not rows:
            print("No historical matches.")
            await database.disconnect()
            return 1
        max_date = max(r[4] for r in rows)
        cutoff = max_date - timedelta(days=args.test_days)
        since = cutoff - timedelta(days=args.window_days)
        scored = _walk(names, rows, since, cutoff, config)
        if not scored:
            print("No settled fixtures in the audit window.")
            await database.disconnect()
            return 1

    await database.disconnect()

    print(RULE)
    print(f"FULL MARKET IMPACT AUDIT — champion {CHAMPION} vs candidate {candidate_label}")
    print(f"grid {grid_version()} · settled fixtures {len(scored):,} · window from {cutoff}")
    print(RULE)

    # Per-market table.
    print("\n  MARKET CALIBRATION (binary Brier; Δ<0 => candidate better):")
    print(f"    {'market / outcome':<40} {'n':>7} {'champ':>8} {'cand':>8} {'Δ':>9}  verdict")
    regressions: list[str] = []
    improvements = 0
    by_market: dict[str, list[MarketDefinition]] = defaultdict(list)
    for d in SUPPORTED:
        by_market[d.market].append(d)
    for _market, defs in by_market.items():
        for d in defs:
            key = d.key
            bc = _brier(scored, key, "champ")
            bd = _brier(scored, key, "cand")
            delta, lo, hi = _paired_ci(scored, key)
            verdict = _verdict(lo, hi)
            if verdict == "REGRESSED":
                regressions.append(
                    f"{d.market} / {d.outcome} (Δ {delta:+.5f}, CI [{lo:+.5f},{hi:+.5f}])"
                )
            elif verdict == "improved":
                improvements += 1
            label = f"{d.market}/{d.outcome}"
            print(
                f"    {label[:39]:<40} {len(scored):>7,} {bc:>8.4f} {bd:>8.4f} "
                f"{delta:>+9.5f}  {verdict}"
            )

    unchanged = len(SUPPORTED) - improvements - len(regressions)
    print(
        f"\n  markets improved (CI<0): {improvements} · "
        f"regressed (CI>0): {len(regressions)} · unchanged: {unchanged}"
    )
    if regressions:
        print("  REGRESSIONS (surfaced, not hidden by aggregate gains):")
        for r in regressions:
            print(f"    ✗ {r}")

    # ECE for the headline result markets.
    print("\n  CALIBRATION (ECE) on key markets — champ -> cand:")
    for market_key in (("1X2", "Home"), ("1X2", "Draw"), ("1X2", "Away"),
                        ("Goals", "Over 2.5"), ("Both teams to score", "Yes")):
        ec = _ece(scored, market_key, "champ")
        ed = _ece(scored, market_key, "cand")
        print(f"    {market_key[0]}/{market_key[1]:<12} {ec:.4f} -> {ed:.4f}")

    # Per-service qualification & selection changes.
    by_key = {d.key: d for d in SUPPORTED}
    print("\n  SERVICE QUALIFICATION & SELECTION CHANGES (threshold on model probability):")
    print(f"    {'service':<26} {'thr':>4} {'q_ch':>5} {'q_cd':>5} {'+':>4} {'-':>4}"
          f"  hit_ch  hit_cd  hit_gain  hit_lost")
    for service in SERVICES:
        defn = by_key.get((service.market, service.outcome))
        if defn is None:
            continue
        s = _service_report(scored, service, defn)

        def fmt(x: float | None) -> str:
            return f"{x:.3f}" if x is not None else "  -  "

        print(
            f"    {service.key:<26} {service.min_probability:>4.2f} "
            f"{s['q_champ']:>5,} {s['q_cand']:>5,} {s['gained']:>4,} {s['lost']:>4,}"
            f"  {fmt(s['hit_champ'])}  {fmt(s['hit_cand'])}  "
            f"{fmt(s['hit_gained'])}  {fmt(s['hit_lost'])}"
        )

    print(RULE)
    if regressions:
        print(f"  AUDIT: {improvements} markets improved, {len(regressions)} REGRESSED. "
              "Weigh the regressions before any promotion.")
    else:
        print(f"  AUDIT: {improvements} markets improved, none regressed with CI excluding 0.")
    print("  Read-only. No table written. Production untouched. Evidence for the promotion gate.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
