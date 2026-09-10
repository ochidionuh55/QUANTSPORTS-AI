"""Dixon-Coles correction to the Poisson goal model.

Poisson assumes the two sides score independently. That is close to true at
higher scorelines and demonstrably false at low ones: 0-0 and 1-1 occur more
often than independence predicts, and 1-0 and 0-1 less often. The consequence
is that Poisson systematically under-predicts draws — visibly so, since our own
model produced draw probabilities below 28% where the observed rate is around
25-26% and should be reachable.

Dixon and Coles (1997) correct exactly the four affected scorelines with a
multiplicative factor::

    tau(0,0) = 1 - lambda * mu * rho
    tau(0,1) = 1 + lambda * rho
    tau(1,0) = 1 + mu * rho
    tau(1,1) = 1 - rho
    tau(x,y) = 1          otherwise

A negative ``rho`` raises 0-0 and 1-1 while lowering 1-0 and 0-1, which is the
direction the data shows. Nothing above one goal each is touched.

**This does not replace Poisson.** It is a separate model, versioned
separately, compared on the same backtest harness. Whether it earns its place
is a measurement, not an assumption — and the honest expectation is a small
improvement in draw calibration rather than an edge over the market.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.quant.poisson import MAX_GOALS, MatchProbabilities, poisson_pmf

DEFAULT_RHO: Final[float] = -0.13
"""Typical value for top-division football.

Dixon and Coles reported around -0.13 on English league data. Used when there
is too little history to fit a value, so the correction still applies in the
right direction rather than not at all.
"""

RHO_BOUNDS: Final[tuple[float, float]] = (-0.25, 0.05)
"""Search range when fitting.

Bounded because tau must stay positive: a large negative rho drives
``1 - lambda * mu * rho`` toward zero and the correction stops being a
correction.
"""

MIN_MATCHES_FOR_RHO: Final[int] = 200
"""Below this, a fitted rho is noise and the default is used instead."""


def tau(
    home_goals: int,
    away_goals: int,
    lambda_home: float,
    lambda_away: float,
    rho: float,
) -> float:
    """Return the Dixon-Coles adjustment for one scoreline.

    Returns 1.0 for any scoreline above one goal each, where independence
    holds well enough.

    The result is floored just above zero: an extreme rho combined with high
    rates can drive the factor negative, which would produce a negative
    probability.
    """
    if home_goals == 0 and away_goals == 0:
        return max(1.0 - lambda_home * lambda_away * rho, 1e-6)
    if home_goals == 0 and away_goals == 1:
        return max(1.0 + lambda_home * rho, 1e-6)
    if home_goals == 1 and away_goals == 0:
        return max(1.0 + lambda_away * rho, 1e-6)
    if home_goals == 1 and away_goals == 1:
        return max(1.0 - rho, 1e-6)
    return 1.0


def score_matrix(
    lambda_home: float,
    lambda_away: float,
    rho: float = DEFAULT_RHO,
    max_goals: int = MAX_GOALS,
) -> dict[tuple[int, int], Decimal]:
    """Return the corrected probability of every scoreline.

    Renormalised after correction, so the grid remains a distribution.
    """
    grid: dict[tuple[int, int], float] = {}
    for home in range(max_goals + 1):
        home_probability = poisson_pmf(home, lambda_home)
        for away in range(max_goals + 1):
            base = home_probability * poisson_pmf(away, lambda_away)
            grid[(home, away)] = base * tau(home, away, lambda_home, lambda_away, rho)

    total = sum(grid.values())
    return {k: Decimal(str(v / total)) for k, v in grid.items()}


def match_probabilities(
    lambda_home: float,
    lambda_away: float,
    rho: float = DEFAULT_RHO,
    totals: tuple[float, ...] = (0.5, 1.5, 2.5, 3.5),
    max_goals: int = MAX_GOALS,
) -> MatchProbabilities:
    """Derive every market from corrected scoring rates.

    Returns the same type as the uncorrected model, so the two are
    interchangeable everywhere downstream and can be compared directly.
    """
    grid = score_matrix(lambda_home, lambda_away, rho, max_goals)

    home_win = sum((p for (h, a), p in grid.items() if h > a), Decimal(0))
    draw = sum((p for (h, a), p in grid.items() if h == a), Decimal(0))
    away_win = sum((p for (h, a), p in grid.items() if h < a), Decimal(0))

    over_under: dict[str, Decimal] = {}
    for line in totals:
        over = sum((p for (h, a), p in grid.items() if h + a > line), Decimal(0))
        over_under[f"over_{line}"] = over
        over_under[f"under_{line}"] = Decimal(1) - over

    both_score = sum((p for (h, a), p in grid.items() if h > 0 and a > 0), Decimal(0))

    return MatchProbabilities(
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        home_win=home_win,
        draw=draw,
        away_win=away_win,
        over_under=over_under,
        both_teams_score=both_score,
        scoreline=grid,
    )


@dataclass(frozen=True)
class RhoFit:
    """Result of fitting the correlation parameter."""

    rho: float
    log_likelihood: float
    matches: int
    fitted: bool

    @property
    def improves_on_independence(self) -> bool:
        """Whether the fitted value implies a real correction.

        A rho near zero means the data does not support the correction, and
        applying it anyway would be adding a parameter for nothing.
        """
        return self.fitted and abs(self.rho) > 0.02


def log_likelihood(observations: list[tuple[int, int, float, float]], rho: float) -> float:
    """Return the log likelihood of observed scores under a given rho.

    Args:
        observations: ``(home_goals, away_goals, lambda_home, lambda_away)``.
        rho: Correlation parameter.
    """
    total = 0.0
    for home, away, lambda_home, lambda_away in observations:
        base = poisson_pmf(home, lambda_home) * poisson_pmf(away, lambda_away)
        adjusted = base * tau(home, away, lambda_home, lambda_away, rho)
        total += math.log(max(adjusted, 1e-300))
    return total


def estimate_rho(
    observations: list[tuple[int, int, float, float]],
    bounds: tuple[float, float] = RHO_BOUNDS,
    steps: int = 60,
) -> RhoFit:
    """Fit the correlation parameter by maximum likelihood.

    A grid search rather than an optimiser: the parameter is one-dimensional
    and bounded, the likelihood is smooth, and a grid cannot diverge or find a
    boundary artefact the way a gradient method can on this shape.

    Falls back to the default when there is too little data, so the correction
    still applies in the right direction rather than being fitted to noise.
    """
    if len(observations) < MIN_MATCHES_FOR_RHO:
        return RhoFit(
            rho=DEFAULT_RHO,
            log_likelihood=log_likelihood(observations, DEFAULT_RHO) if observations else 0.0,
            matches=len(observations),
            fitted=False,
        )

    low, high = bounds
    best_rho = DEFAULT_RHO
    best_score = -math.inf

    for index in range(steps + 1):
        candidate = low + (high - low) * index / steps
        score = log_likelihood(observations, candidate)
        if score > best_score:
            best_score = score
            best_rho = candidate

    return RhoFit(
        rho=best_rho,
        log_likelihood=best_score,
        matches=len(observations),
        fitted=True,
    )


MODEL_NAME: Final[str] = "dixon_coles"
MODEL_VERSION: Final[str] = "dixon-coles-v1"
