"""V3 — totals-preserving stack model (recency + opponent-adjusted, control total).

This is the single canonical implementation of the model promoted from EXP-009:
recency-weighted (half-life 365d) **and** opponent-adjusted (2 fixed-point
iterations) team strengths for the *ratio* (who wins), with the total-goals
*level* rescaled to the frozen control's equal-weight total (which keeps the
goals markets calibrated). Everything downstream is the canonical Dixon-Coles
grid, exactly as before.

It is deliberately a set of **pure functions over a competition's match pool**,
so the exact same code produces the research evidence, the shadow forecasts and
(once routed) the production forecasts — the "research-code == production-code"
guarantee. Nothing here reads the database or mutates anything.

Frozen lineage (do not tune):
    recency half-life = 365 days
    opponent adjustment = 2 iterations
    total-goals anchor  = equal-weight control total
    grid                = canonical Dixon-Coles grid (per-competition rho)
"""

from __future__ import annotations

from collections import defaultdict

from app.quant.grid import Grid, build_grid, build_match_probabilities
from app.quant.poisson import (
    LeagueAverages,
    MatchProbabilities,
    TeamStrength,
    expected_goals,
    team_strength,
)

VERSION = "model-only-v3-stack-norm"
HALF_LIFE = 365.0
ITERATIONS = 2
MIN_LEAGUE_RESULTS = 20
RATIO_FLOOR, RATIO_CEIL = 0.2, 5.0

# One competition's match: (home_id, away_id, home_goals, away_goals, ordinal).
Match = tuple[int, int, int, int, int]


def _clamp(x: float) -> float:
    return max(RATIO_FLOOR, min(RATIO_CEIL, x))


def league_averages(matches: list[Match]) -> LeagueAverages | None:
    """Per-competition league averages, or None if too few results."""
    n = len(matches)
    if n < MIN_LEAGUE_RESULTS:
        return None
    sum_home = sum(m[2] for m in matches)
    sum_away = sum(m[3] for m in matches)
    return LeagueAverages(sum_home / n, sum_away / n, n)


def control_strength(matches: list[Match], team: int, avg: LeagueAverages) -> TeamStrength:
    """Frozen control: equal-weight attack/defence over the competition history."""
    home = [(hg, ag) for h, a, hg, ag, _ in matches if h == team]
    away = [(ag, hg) for h, a, hg, ag, _ in matches if a == team]
    return team_strength(team, home, away, avg)


def stack_strengths(
    matches: list[Match], avg: LeagueAverages, ref: int
) -> dict[int, TeamStrength]:
    """Recency-weighted, opponent-adjusted strengths — the V3 ratio engine.

    A fixed point over the competition's matches, each weighted by the same
    exponential decay, run for ``ITERATIONS`` passes. Identical to the EXP-009
    research implementation.
    """

    def w(o: int) -> float:
        return float(0.5 ** ((ref - o) / HALF_LIFE))

    wscored: dict[int, float] = defaultdict(float)
    wconc: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    for h, a, hg, ag, o in matches:
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
    for _it in range(ITERATIONS):
        denom_a: dict[int, float] = defaultdict(float)
        denom_d: dict[int, float] = defaultdict(float)
        for h, a, _hg, _ag, o in matches:
            ww = w(o)
            denom_a[h] += ww * avg.home_goals * defence[a]
            denom_d[h] += ww * avg.away_goals * attack[a]
            denom_a[a] += ww * avg.away_goals * defence[h]
            denom_d[a] += ww * avg.home_goals * attack[h]
        attack = {t: _clamp(wscored[t] / denom_a[t]) if denom_a[t] > 0 else 1.0 for t in teams}
        defence = {t: _clamp(wconc[t] / denom_d[t]) if denom_d[t] > 0 else 1.0 for t in teams}
    return {t: TeamStrength(t, attack[t], defence[t], counts[t]) for t in teams}


def v3_expected_goals(
    matches: list[Match], home_id: int, away_id: int, ref: int
) -> tuple[float, float] | None:
    """V3 (lambda_home, lambda_away), or None if the fixture is unmodellable.

    Recency + opponent-adjusted ratio, with the total rescaled to the frozen
    equal-weight control's total (the totals-preserving anchor).
    """
    avg = league_averages(matches)
    if avg is None or not avg.is_reliable:
        return None
    control_h = control_strength(matches, home_id, avg)
    control_a = control_strength(matches, away_id, avg)
    if not (control_h.is_reliable and control_a.is_reliable):
        return None
    strengths = stack_strengths(matches, avg, ref)
    if home_id not in strengths or away_id not in strengths:
        return None
    sl_h, sl_a = expected_goals(strengths[home_id], strengths[away_id], avg)
    stack_total = sl_h + sl_a
    if stack_total <= 0:
        return None
    cl_h, cl_a = expected_goals(control_h, control_a, avg)
    scale = (cl_h + cl_a) / stack_total
    return sl_h * scale, sl_a * scale


def v3_match_probabilities(
    matches: list[Match], home_id: int, away_id: int, ref: int, competition: str | None
) -> MatchProbabilities | None:
    """Full canonical market set from the V3 lambdas, or None if unmodellable."""
    lambdas = v3_expected_goals(matches, home_id, away_id, ref)
    if lambdas is None:
        return None
    return build_match_probabilities(lambdas[0], lambdas[1], competition=competition)


def v3_grid(
    matches: list[Match], home_id: int, away_id: int, ref: int, competition: str | None
) -> Grid | None:
    """The canonical Dixon-Coles grid under V3, or None if unmodellable."""
    lambdas = v3_expected_goals(matches, home_id, away_id, ref)
    if lambdas is None:
        return None
    return build_grid(lambdas[0], lambdas[1], corrected=True, competition=competition)
