#!/usr/bin/env python3
"""ACTUAL INCUMBENT vs V3 — the promotion comparison against the real baseline.

EXP-009 was proved against the *research* V2 grid. Production does not serve that
grid — it serves a Poisson + Elo + Form **blend** (``model_probabilities``), and
the Best-of/services grid is that Poisson grid **tilted** to the blend. So before
promoting, this compares the frozen pure V3 against the model users *actually*
receive, reconstructed with production's own Elo/Form/Poisson/tilt code (not a
simplified V2 stand-in).

Three paths, kept separate:

1. **Published probability path** — when odds exist the published 1X2 is the
   bookmaker's de-margined price (reliability 0, no promoted model). It is
   model-agnostic, so promoting V3 does not change it; historical odds are not
   stored here, so it is reported as unchanged-by-construction, not scored.
2. **Model-only path** — the blend (Poisson+Elo+Form) vs pure V3 1X2. Scored.
3. **Canonical grid used by Best-of/services** — incumbent's *tilted* grid vs
   V3's grid, every registered market. Scored (full market audit).

Metrics: paired Brier, log loss, ECE, competition breadth (path 2); per-market
binary Brier + bootstrap CI with regressions surfaced (path 3). V3 is frozen
(hl365 · oppadj it2 · totals-preserved) — **no tuning from this comparison**.
Same fixtures, same chronological/OOS methodology, walk-forward, past data only.

Run::

    railway ssh "python scripts/incumbent_vs_v3.py"                    # newest-year test
    railway ssh "python scripts/incumbent_vs_v3.py --test-end 2023-09-04"  # untouched OOS
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.competitions import code_for_name
from app.core.config import get_settings
from app.infrastructure.database import Database
from app.quant.elo import EloEngine
from app.quant.form import MatchOutcome, summarise
from app.quant.form import predict as form_predict
from app.quant.grid import build_grid, build_match_probabilities, grid_version
from app.quant.markets import MARKETS
from app.quant.poisson import LeagueAverages, TeamStrength, expected_goals, team_strength
from app.services.selections import _tilted

RULE = "=" * 92
HALF_LIFE = 365.0
STACK_ITERATIONS = 2
HISTORY_WINDOW_DAYS = 365 * 3
RECENT_LIMIT = 60
MIN_MATCHES_FOR_ANALYSIS = 8
MIN_LEAGUE_RESULTS = 20
RATIO_FLOOR, RATIO_CEIL = 0.2, 5.0
BOOTSTRAP = 400
EPS = 1e-12
TEST_DAYS = 365
DEFAULT_WINDOW_DAYS = 1095
OUTCOMES = ("home", "draw", "away")
SUPPORTED = tuple(d for d in MARKETS if d.supported)
Probs = tuple[float, float, float]


@dataclass
class Row:
    competition: str
    season: str
    y: tuple[int, int, int]
    incumbent: Probs
    v3: Probs
    inc_markets: dict[tuple[str, str], float]
    v3_markets: dict[tuple[str, str], float]
    won: dict[tuple[str, str], int]


# ---- V3 (frozen EXP-009) machinery, per competition -----------------------
@dataclass
class CompAcc:
    matches: list[tuple[int, int, int, int, int]] = field(default_factory=list)
    home: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    away: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    dated: dict[int, list[tuple[int, int, int]]] = field(default_factory=dict)
    results: list[tuple[int, int]] = field(default_factory=list)
    sum_home: int = 0
    sum_away: int = 0

    def played(self, t: int) -> int:
        return len(self.home.get(t, [])) + len(self.away.get(t, []))

    def averages(self) -> LeagueAverages:
        n = len(self.results)
        return LeagueAverages(self.sum_home / n, self.sum_away / n, n)

    def record(self, h: int, a: int, hg: int, ag: int, o: int) -> None:
        self.matches.append((h, a, hg, ag, o))
        self.home.setdefault(h, []).append((hg, ag))
        self.away.setdefault(a, []).append((ag, hg))
        self.dated.setdefault(h, []).append((hg, ag, o))
        self.dated.setdefault(a, []).append((ag, hg, o))
        self.results.append((hg, ag))
        self.sum_home += hg
        self.sum_away += ag


def _clamp(x: float) -> float:
    return max(RATIO_FLOOR, min(RATIO_CEIL, x))


def _control_strength(acc: CompAcc, t: int, avg: LeagueAverages) -> TeamStrength:
    return team_strength(t, acc.home.get(t, []), acc.away.get(t, []), avg)


def _adjusted_it2(acc: CompAcc, avg: LeagueAverages, ref: int) -> dict[int, TeamStrength]:
    def w(o: int) -> float:
        return float(0.5 ** ((ref - o) / HALF_LIFE))

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
    for _it in range(STACK_ITERATIONS):
        da: dict[int, float] = defaultdict(float)
        dd: dict[int, float] = defaultdict(float)
        for h, a, _hg, _ag, o in acc.matches:
            ww = w(o)
            da[h] += ww * avg.home_goals * defence[a]
            dd[h] += ww * avg.away_goals * attack[a]
            da[a] += ww * avg.away_goals * defence[h]
            dd[a] += ww * avg.home_goals * attack[h]
        attack = {t: _clamp(wscored[t] / da[t]) if da[t] > 0 else 1.0 for t in teams}
        defence = {t: _clamp(wconc[t] / dd[t]) if dd[t] > 0 else 1.0 for t in teams}
    return {t: TeamStrength(t, attack[t], defence[t], counts[t]) for t in teams}


def _grid_from_lambda(lam_h: float, lam_a: float, code: str | None):
    return build_grid(lam_h, lam_a, corrected=True, competition=code)


def _markets_from_grid(grid) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for d in SUPPORTED:
        out[d.key] = float(sum((p for (h, a), p in grid.items() if d.holds(h, a)), Decimal(0)))
    return out


def _1x2_from_grid(grid) -> Probs:
    ph = pd = pa = 0.0
    for (h, a), p in grid.items():
        fp = float(p)
        if h > a:
            ph += fp
        elif h == a:
            pd += fp
        else:
            pa += fp
    t = ph + pd + pa
    return (ph / t, pd / t, pa / t)


# ---- incumbent (production) team history, cross-competition ---------------
@dataclass
class TeamState:
    recent: deque[tuple[int, bool, int, int]] = field(  # (ordinal, at_home, scored, conceded)
        default_factory=lambda: deque(maxlen=RECENT_LIMIT)
    )


def _incumbent_1x2(
    home_id: int, away_id: int, states: dict[int, TeamState], elo: EloEngine,
    avg: LeagueAverages, code: str | None, moment: datetime, cutoff_ord: int,
) -> tuple[Probs, dict[tuple[str, str], float]] | None:
    """Reconstruct production's model-only blend + tilted services grid."""
    hs_state = states.get(home_id)
    as_state = states.get(away_id)
    if hs_state is None or as_state is None:
        return None
    h_recent = [m for m in hs_state.recent if m[0] >= cutoff_ord]
    a_recent = [m for m in as_state.recent if m[0] >= cutoff_ord]
    if len(h_recent) < MIN_MATCHES_FOR_ANALYSIS or len(a_recent) < MIN_MATCHES_FOR_ANALYSIS:
        return None

    # Poisson component.
    h_home = [(s, c) for o, at, s, c in h_recent if at]
    h_away = [(s, c) for o, at, s, c in h_recent if not at]
    a_home = [(s, c) for o, at, s, c in a_recent if at]
    a_away = [(s, c) for o, at, s, c in a_recent if not at]
    hs = team_strength(home_id, h_home, h_away, avg)
    as_ = team_strength(away_id, a_home, a_away, avg)
    if not (hs.is_reliable and as_.is_reliable and avg.is_reliable):
        return None
    lam_h, lam_a = expected_goals(hs, as_, avg)
    poisson = build_match_probabilities(lam_h, lam_a, competition=code)
    components: list[tuple[float, float, float]] = [
        (float(poisson.home_win), float(poisson.draw), float(poisson.away_win))
    ]

    # Elo component.
    if elo.is_reliable(home_id, away_id):
        e = elo.predict(home_id, away_id)
        components.append((float(e[0]), float(e[1]), float(e[2])))

    # Form component.
    def outcomes(recent: list[tuple[int, bool, int, int]]) -> list[MatchOutcome]:
        return [
            MatchOutcome(
                played_at=datetime.fromordinal(o).replace(tzinfo=UTC),
                scored=s, conceded=c, at_home=at,
            )
            for o, at, s, c in recent
        ]

    hf = summarise(home_id, outcomes(h_recent), before=moment)
    af = summarise(away_id, outcomes(a_recent), before=moment)
    if hf.is_reliable and af.is_reliable:
        f = form_predict(hf, af)
        components.append((float(f[0]), float(f[1]), float(f[2])))

    # Equal-weight blend -> model_probabilities (path 2).
    n = len(components)
    blend = tuple(sum(c[i] for c in components) / n for i in range(3))
    tot = sum(blend)
    model = {"home": blend[0] / tot, "draw": blend[1] / tot, "away": blend[2] / tot}

    # Services grid (path 3): global-rho grid tilted to the blend.
    tilted = _tilted(build_grid(lam_h, lam_a), model)
    return (model["home"], model["draw"], model["away"]), _markets_from_grid(tilted)


# ---- metrics --------------------------------------------------------------
def _brier_row(p: Probs, y: tuple[int, int, int]) -> float:
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y, strict=True))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _brier(rows: list[Row], pick) -> float:
    return _mean([_brier_row(pick(r), r.y) for r in rows])


def _log_loss(rows: list[Row], pick) -> float:
    total = 0.0
    for r in rows:
        for p, y in zip(pick(r), r.y, strict=True):
            if y:
                total -= math.log(max(p, EPS))
    return total / len(rows) if rows else 0.0


def _ece(rows: list[Row], pick, bins: int = 10) -> float:
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for r in rows:
        for p, y in zip(pick(r), r.y, strict=True):
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


def _paired_ci_1x2(rows: list[Row], a, b) -> tuple[float, float, float]:
    deltas = [_brier_row(a(r), r.y) - _brier_row(b(r), r.y) for r in rows]
    rng = random.Random(20260920)
    size = len(deltas)
    means = [_mean([deltas[rng.randrange(size)] for _ in range(size)]) for _ in range(BOOTSTRAP)]
    means.sort()
    return _mean(deltas), means[int(0.025 * BOOTSTRAP)], means[int(0.975 * BOOTSTRAP)]


def _paired_ci_market(rows: list[Row], key: tuple[str, str]) -> tuple[float, float, float]:
    deltas = [
        (r.v3_markets[key] - r.won[key]) ** 2 - (r.inc_markets[key] - r.won[key]) ** 2
        for r in rows
    ]
    rng = random.Random(20260920)
    size = len(deltas)
    means = [_mean([deltas[rng.randrange(size)] for _ in range(size)]) for _ in range(BOOTSTRAP)]
    means.sort()
    return _mean(deltas), means[int(0.025 * BOOTSTRAP)], means[int(0.975 * BOOTSTRAP)]


def _breadth(rows: list[Row], a, b) -> tuple[int, int]:
    by: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by[r.competition].append(r)
    better = sum(1 for rs in by.values() if _brier(rs, a) < _brier(rs, b))
    return better, len(by)


async def _load(session) -> tuple[dict[int, str], list[tuple]]:
    comp_rows = await session.execute(text("SELECT id, canonical_name FROM competitions"))
    names = {int(r[0]): str(r[1]) for r in comp_rows.fetchall()}
    match_rows = await session.execute(
        text(
            "SELECT competition_id, home_team_id, away_team_id, season, match_date, "
            "home_goals, away_goals FROM historical_matches "
            "WHERE competition_id IS NOT NULL ORDER BY match_date, id"
        )
    )
    return names, match_rows.fetchall()


def _walk(names: dict[int, str], rows: list[tuple], test_start: date, test_end: date) -> list[Row]:
    elo = EloEngine()
    states: dict[int, TeamState] = defaultdict(TeamState)
    comp: dict[int, CompAcc] = defaultdict(CompAcc)
    league_win: deque[tuple[int, int, int]] = deque()  # (ordinal, hg, ag)
    lg_home = lg_away = 0
    out: list[Row] = []

    for cid, home_id, away_id, season, mdate, hg, ag in rows:
        cid, home_id, away_id, hg, ag = int(cid), int(home_id), int(away_id), int(hg), int(ag)
        o = mdate.toordinal()
        moment = datetime(mdate.year, mdate.month, mdate.day, tzinfo=UTC)
        cutoff_ord = o - HISTORY_WINDOW_DAYS

        # Expire league window beyond 3y.
        while league_win and league_win[0][0] < cutoff_ord:
            _, eh, ea = league_win.popleft()
            lg_home -= eh
            lg_away -= ea
        avg = (
            LeagueAverages(lg_home / len(league_win), lg_away / len(league_win), len(league_win))
            if len(league_win) >= MIN_LEAGUE_RESULTS
            else None
        )

        acc = comp[cid]
        if (
            avg is not None
            and test_start <= mdate < test_end
            and acc.played(home_id) >= 5
            and acc.played(away_id) >= 5
            and len(acc.results) >= MIN_LEAGUE_RESULTS
        ):
            code = code_for_name(names.get(cid, str(cid)))
            cavg = acc.averages()
            ch = _control_strength(acc, home_id, cavg)
            ca = _control_strength(acc, away_id, cavg)
            inc = _incumbent_1x2(home_id, away_id, states, elo, avg, code, moment, cutoff_ord)
            if inc is not None and ch.is_reliable and ca.is_reliable and cavg.is_reliable:
                strengths = _adjusted_it2(acc, cavg, o)
                if home_id in strengths and away_id in strengths:
                    sl_h, sl_a = expected_goals(strengths[home_id], strengths[away_id], cavg)
                    cl_h, cl_a = expected_goals(ch, ca, cavg)
                    stot = sl_h + sl_a
                    if stot > 0:
                        scale = (cl_h + cl_a) / stot
                        v3_grid = _grid_from_lambda(sl_h * scale, sl_a * scale, code)
                        inc_probs, inc_markets = inc
                        out.append(
                            Row(
                                competition=names.get(cid, str(cid)),
                                season=str(season or "?"),
                                y=(int(hg > ag), int(hg == ag), int(hg < ag)),
                                incumbent=inc_probs,
                                v3=_1x2_from_grid(v3_grid),
                                inc_markets=inc_markets,
                                v3_markets=_markets_from_grid(v3_grid),
                                won={d.key: int(d.holds(hg, ag)) for d in SUPPORTED},
                            )
                        )

        # Update all state with this match (strictly after emitting).
        elo.record(home_id, away_id, hg, ag, moment)
        states[home_id].recent.append((o, True, hg, ag))
        states[away_id].recent.append((o, False, ag, hg))
        acc.record(home_id, away_id, hg, ag, o)
        league_win.append((o, hg, ag))
        lg_home += hg
        lg_away += ag
    return out


def _verdict(lo: float, hi: float) -> str:
    if hi < 0:
        return "V3 better, CI excludes 0"
    if lo > 0:
        return "INCUMBENT better, CI excludes 0"
    return "indistinguishable (CI spans 0)"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-end", default=None)
    parser.add_argument("--test-days", type=int, default=TEST_DAYS)
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
        scored = _walk(names, rows, test_start, test_end)
    await database.disconnect()

    if not scored:
        print("No paired fixtures in the window.")
        return 1

    def inc(r: Row) -> Probs:
        return r.incumbent

    def v3(r: Row) -> Probs:
        return r.v3

    b_inc, b_v3 = _brier(scored, inc), _brier(scored, v3)
    delta, lo, hi = _paired_ci_1x2(scored, v3, inc)
    helped, total = _breadth(scored, v3, inc)

    print(RULE)
    print(f"ACTUAL INCUMBENT vs V3 — PROMOTION COMPARISON  ({grid_version()})")
    era = " (UNTOUCHED pre-EXP-002 OOS)" if args.test_end else ""
    print(f"paired fixtures {len(scored):,} · test [{test_start}, {test_end}){era}")
    print(RULE)
    print("\n  PATH 1 — published probability: bookmaker de-margined price, model-agnostic.")
    print("           Promoting V3 does not change it (reliability 0). Not scored here.")
    print("\n  PATH 2 — model-only 1X2  (incumbent Poisson+Elo+Form blend vs pure V3):")
    print(f"    Brier     incumbent {b_inc:.4f}   V3 {b_v3:.4f}")
    print(f"    paired Δ (V3-inc) {delta:+.5f}  CI [{lo:+.5f}, {hi:+.5f}]  {_verdict(lo, hi)}")
    print(f"    log loss  incumbent {_log_loss(scored, inc):.4f}   V3 {_log_loss(scored, v3):.4f}")
    print(f"    ECE       incumbent {_ece(scored, inc):.4f}   V3 {_ece(scored, v3):.4f}")
    print(f"    competition breadth: V3 beat incumbent in {helped}/{total}")

    print("\n  PATH 3 — canonical markets  (incumbent tilted grid vs V3 grid; Δ<0 => V3 better):")
    print(f"    {'market / outcome':<40} {'inc':>8} {'V3':>8} {'Δ':>9}  verdict")
    improved = regressed = 0
    regressions: list[str] = []
    for d in SUPPORTED:
        key = d.key
        bi = _mean([(r.inc_markets[key] - r.won[key]) ** 2 for r in scored])
        bv = _mean([(r.v3_markets[key] - r.won[key]) ** 2 for r in scored])
        dd, mlo, mhi = _paired_ci_market(scored, key)
        verdict = _verdict(mlo, mhi)
        if mhi < 0:
            improved += 1
        elif mlo > 0:
            regressed += 1
            regressions.append(f"{d.market}/{d.outcome} (Δ {dd:+.5f}, CI [{mlo:+.5f},{mhi:+.5f}])")
        label = f"{d.market}/{d.outcome}"
        print(f"    {label[:39]:<40} {bi:>8.4f} {bv:>8.4f} {dd:>+9.5f}  {verdict}")
    print(f"\n  markets V3-better: {improved} · V3-worse: {regressed} · "
          f"unchanged: {len(SUPPORTED) - improved - regressed}")
    if regressions:
        print("  V3 REGRESSIONS vs the actual incumbent (surfaced, not hidden):")
        for r in regressions:
            print(f"    x {r}")

    print(RULE)
    verdict_1x2 = hi < 0
    if verdict_1x2 and not regressions:
        print("  RESULT: V3 beats the ACTUAL incumbent on the model-only path (CI excludes 0)")
        print("          with no market regression. Clean case — proceed to preflight.")
    elif verdict_1x2:
        print("  RESULT: V3 beats the incumbent on 1X2 but some markets regress — weigh below.")
    else:
        print("  RESULT: V3 does NOT beat the actual incumbent on the model-only path.")
        print("          Do NOT promote — the incumbent blend is competitive. Report and stop.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
