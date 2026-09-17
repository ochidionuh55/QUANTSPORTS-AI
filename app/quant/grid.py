"""The one place a football scoreline distribution is built.

**Why this module exists.** Four production paths each constructed their own
grid directly from the independent Poisson: the live match analysis, the
Best of the Day forecast rebuild, the market-history reconstruction and the
first-half markets. Nothing was wrong with any of them in isolation, and that
was precisely the hazard — a correction applied to one would silently not
apply to the others, so one fixture could carry a corrected probability on the
Telegram board and an uncorrected one on the website. A model whose output
depends on which screen asked is not one model.

Every production path now routes through :func:`build_grid`. There is exactly
one switch, in one place.

**The QUANTSPORT rule this protects.** One fixture produces one joint score
distribution, and every supported market is derived from that distribution.
Home win, draw, both-teams-to-score, totals, double chance and the combination
markets are all sums over the same grid. None of them is computed by a
separate model, so none of them can disagree with another about the same
match.

**What the correction does.** Poisson treats the two sides' goals as
independent. That holds well enough at higher scorelines and fails at low
ones: 0-0 and 1-1 occur more often than independence implies, 1-0 and 0-1 less
often. The consequence is a systematic under-prediction of draws.

Dixon and Coles (1997) correct exactly those four scorelines. The
implementation already existed in :mod:`app.quant.dixon_coles`, tested and used
by the backtest harness; it was simply never wired into anything a user could
see.

**On by default, but reversible without a deploy.** This changes every
published probability, so it carries an environment switch. Turning it off
restores the previous engine exactly.

**This is not a claim of edge.** The correction aims to make the draw
probability less wrong. It says nothing about whether the resulting number
beats a bookmaker's price, and the value-detection gate is untouched by it.
Whether it earns its place in production is decided by the before/after
validation in ``docs/VALIDATION.md``, not by the fact that theory says it
should help.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Final

from app.quant.dixon_coles import DEFAULT_RHO
from app.quant.dixon_coles import score_matrix as corrected_matrix
from app.quant.poisson import MAX_GOALS, MatchProbabilities
from app.quant.poisson import score_matrix as independent_matrix

Scoreline = tuple[int, int]
Grid = dict[Scoreline, Decimal]

CORRECTION_ENV: Final[str] = "QUANT_DIXON_COLES_ENABLED"
"""Environment switch for the correction.

Present because this alters every published probability. A change with that
reach needs an off switch that does not require a code change to reach.
"""

RHO_ENV: Final[str] = "QUANT_DIXON_COLES_RHO"
"""Override for the correlation parameter, for experiments."""

MODEL_NAME_INDEPENDENT: Final[str] = "poisson"
MODEL_NAME_CORRECTED: Final[str] = "poisson_dc"
"""Component name recorded against each prediction.

Distinct names because a track record spanning a model change is
uninterpretable without knowing which side of the change a forecast came from.
"""

GRID_VERSION_INDEPENDENT: Final[str] = "grid-v1-poisson"
GRID_VERSION_CORRECTED: Final[str] = "grid-v2-poisson-dc"
"""Version stamped on predictions, so Track Record can separate generations.

Published selections are never rewritten when the model changes. A selection
carries the version that produced it, permanently.
"""


def _truthy(value: str | None, default: bool) -> bool:
    """Read a boolean environment variable."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def correction_enabled() -> bool:
    """Whether the Dixon-Coles correction is applied.

    **Defaults to off, on the evidence.** The correction does fix the draw
    bias it was raised for — 88,808 walk-forward forecasts move it from -2.0%
    to +0.8% — but on the same sample the Brier improvement is
    indistinguishable from chance, log loss is worse, and it helps in only 21
    of 38 competitions. See ``docs/VALIDATION.md``.

    It stays off until a per-league rho is fitted and re-audited. Turning it on
    is one environment variable; turning it on without that work would be
    shipping a change because the theory is appealing, which is the thing the
    validation exists to prevent.
    """
    return _truthy(os.environ.get(CORRECTION_ENV), default=False)


def active_rho() -> float:
    """Return the correlation parameter in force.

    Falls back to the published default when the override is absent or
    unparseable, rather than raising: a malformed environment variable must
    not take the scanner down mid-run.
    """
    raw = os.environ.get(RHO_ENV)
    if raw is None:
        return DEFAULT_RHO
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_RHO


def model_name() -> str:
    """Return the component name for predictions built here."""
    return MODEL_NAME_CORRECTED if correction_enabled() else MODEL_NAME_INDEPENDENT


def grid_version() -> str:
    """Return the version string for predictions built here."""
    return GRID_VERSION_CORRECTED if correction_enabled() else GRID_VERSION_INDEPENDENT


def build_grid(
    lambda_home: float,
    lambda_away: float,
    max_goals: int = MAX_GOALS,
    rho: float | None = None,
    corrected: bool | None = None,
) -> Grid:
    """Return the probability of every scoreline.

    Args:
        lambda_home: Expected home goals.
        lambda_away: Expected away goals.
        max_goals: Largest scoreline on each axis.
        rho: Correlation parameter; the configured value when omitted.
        corrected: Force the correction on or off, overriding configuration.
            Used by the validation harness to score both engines on identical
            fixtures, and by tests. Production callers leave it unset.

    Returns:
        A distribution over scorelines summing to approximately one.
    """
    use_correction = correction_enabled() if corrected is None else corrected
    if not use_correction:
        return independent_matrix(lambda_home, lambda_away, max_goals=max_goals)
    return corrected_matrix(
        lambda_home,
        lambda_away,
        rho=active_rho() if rho is None else rho,
        max_goals=max_goals,
    )


def build_match_probabilities(
    lambda_home: float,
    lambda_away: float,
    totals: tuple[float, ...] = (0.5, 1.5, 2.5, 3.5),
    max_goals: int = MAX_GOALS,
    rho: float | None = None,
    corrected: bool | None = None,
) -> MatchProbabilities:
    """Derive the headline markets from one grid.

    Returns the same type the uncorrected model returns, so callers are
    interchangeable and the two engines can be compared directly on identical
    inputs.

    Every figure here is a sum over the same distribution. There is no separate
    calculation for the result markets, the totals or both-teams-to-score, so
    they cannot drift apart.
    """
    grid = build_grid(
        lambda_home,
        lambda_away,
        max_goals=max_goals,
        rho=rho,
        corrected=corrected,
    )

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


__all__ = [
    "CORRECTION_ENV",
    "GRID_VERSION_CORRECTED",
    "GRID_VERSION_INDEPENDENT",
    "MODEL_NAME_CORRECTED",
    "MODEL_NAME_INDEPENDENT",
    "RHO_ENV",
    "Grid",
    "Scoreline",
    "active_rho",
    "build_grid",
    "build_match_probabilities",
    "correction_enabled",
    "grid_version",
    "model_name",
]
