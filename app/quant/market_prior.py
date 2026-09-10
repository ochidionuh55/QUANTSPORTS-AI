"""Market-as-prior combination.

The market is the prior. Models must earn the right to disagree with it.

This is not a stylistic preference. Bookmaker closing prices are among the
best-calibrated forecasts that exist for football: they aggregate injury news,
lineup leaks, weather and money flow, none of which a Poisson-plus-Elo model
sees. A model that averages itself with the market as one vote among several is
mostly replacing good information with worse.

::

    reference odds ─► margin removal ─► market prior
                                            │
    own models ─────────────────────────────┤
                                            ▼
                              log-odds blend with shrinkage
                                            ▼
                                  calibrated posterior
                                            │
                      compared against ─► tradeable odds ─► EV

**Why log-odds.** Averaging probabilities directly is wrong near the extremes:
the midpoint of 1% and 5% is 3%, which understates how large that disagreement
actually is — the model is claiming five times the risk. Log-odds space treats
that as the substantial disagreement it is, and cannot produce a probability
outside ``(0, 1)``.

**Shrinkage.** ``reliability`` is how far the model is trusted to move the
prior, in ``[0, 1]``. It is not a guess: it comes from measured backtest
performance and defaults to zero, meaning a model with no track record returns
the market prior unchanged. A model earns influence by demonstrating skill.

**Renormalisation.** Blending each outcome of a mutually exclusive market
independently does not preserve a sum of one, so the result is renormalised.
This is a real approximation, not a formality, and it is why
:func:`combine_market` exists rather than callers blending outcome by outcome.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

CLAMP: Final[float] = 1e-6
"""Probabilities are clamped away from 0 and 1 before taking log-odds, which
would otherwise be infinite."""

MAX_LOG_ODDS_SHIFT: Final[float] = 1.5
"""Maximum distance the model may move the prior, in log-odds.

Roughly a factor of 4.5 in odds terms. A model proposing a larger move than
that is almost always broken — a mis-parsed fixture, a team resolved to the
wrong club — rather than insightful, and without a cap that error becomes an
enormous apparent edge that passes every filter.
"""


class CombinationError(ValueError):
    """Raised when probabilities cannot be combined."""


def logit(probability: float) -> float:
    """Return the log-odds of a probability."""
    clamped = min(max(probability, CLAMP), 1 - CLAMP)
    return math.log(clamped / (1 - clamped))


def expit(log_odds: float) -> float:
    """Return the probability for a log-odds value."""
    if log_odds >= 0:
        return 1.0 / (1.0 + math.exp(-log_odds))
    exponent = math.exp(log_odds)
    return exponent / (1.0 + exponent)


@dataclass(frozen=True)
class Combination:
    """One outcome's prior, model estimate and resulting posterior."""

    label: str
    prior: Decimal
    model: Decimal
    posterior: Decimal
    shift_log_odds: float
    was_capped: bool

    @property
    def deviation(self) -> Decimal:
        """How far the posterior moved from the prior."""
        return self.posterior - self.prior


def combine_one(
    prior: float, model: float, reliability: float, cap: float = MAX_LOG_ODDS_SHIFT
) -> tuple[float, float, bool]:
    """Blend one model estimate toward its prior in log-odds space.

    Args:
        prior: Market-implied fair probability.
        model: The model's estimate.
        reliability: How far the model is trusted, in ``[0, 1]``. Zero returns
            the prior unchanged.
        cap: Maximum permitted shift in log-odds.

    Returns:
        ``(posterior, shift, was_capped)``.

    Raises:
        CombinationError: If reliability is outside ``[0, 1]``.
    """
    if not 0 <= reliability <= 1:
        raise CombinationError(
            f"Reliability must lie in [0, 1], got {reliability}. It represents "
            "measured skill, not a tuning knob."
        )

    prior_logit = logit(prior)
    model_logit = logit(model)
    shift = reliability * (model_logit - prior_logit)

    was_capped = abs(shift) > cap
    if was_capped:
        shift = cap if shift > 0 else -cap

    return expit(prior_logit + shift), shift, was_capped


def combine_market(
    priors: dict[str, Decimal],
    model: dict[str, Decimal],
    reliability: float,
    cap: float = MAX_LOG_ODDS_SHIFT,
) -> dict[str, Combination]:
    """Blend a complete mutually exclusive market.

    Args:
        priors: Market fair probabilities, summing to one.
        model: Model probabilities for the same outcomes.
        reliability: Measured model skill in ``[0, 1]``.
        cap: Maximum log-odds shift per outcome.

    Returns:
        One :class:`Combination` per outcome, with posteriors summing to one.

    Raises:
        CombinationError: If the two markets disagree on outcomes, or the
            priors are not a distribution.
    """
    if set(priors) != set(model):
        raise CombinationError(
            f"Prior and model must cover the same outcomes. "
            f"Prior has {sorted(priors)}, model has {sorted(model)}."
        )
    if len(priors) < 2:
        raise CombinationError(
            "A market needs at least two outcomes; a single probability cannot " "be renormalised."
        )

    prior_total = sum(priors.values())
    if abs(prior_total - Decimal(1)) > Decimal("0.01"):
        raise CombinationError(
            f"Prior probabilities must sum to 1.0, got {prior_total}. Remove "
            "the bookmaker margin before using prices as a prior."
        )

    raw: dict[str, tuple[float, float, bool]] = {
        label: combine_one(float(priors[label]), float(model[label]), reliability, cap)
        for label in priors
    }

    # Independent per-outcome blending does not preserve the sum, so the
    # posterior is renormalised. The relative ordering and the direction of
    # each shift survive; the absolute magnitudes are scaled slightly.
    total = sum(posterior for posterior, _, _ in raw.values())
    if total <= 0:
        raise CombinationError("Combined probabilities summed to zero.")

    return {
        label: Combination(
            label=label,
            prior=priors[label],
            model=model[label],
            posterior=Decimal(str(posterior / total)),
            shift_log_odds=shift,
            was_capped=capped,
        )
        for label, (posterior, shift, capped) in raw.items()
    }


def reliability_from_skill(brier_skill: float, ceiling: float = 0.6, scale: float = 40.0) -> float:
    """Convert measured backtest skill into a shrinkage weight.

    A model with no demonstrated skill gets zero influence and the system
    returns the market prior. Influence rises with measured skill and is capped
    well below one, because even a genuinely better model should not be trusted
    to override the market entirely on a single fixture.

    Args:
        brier_skill: Fractional improvement over the market baseline.
        ceiling: Maximum influence a model may earn.
        scale: How quickly influence rises with skill. At the default, a 1%
            Brier improvement earns roughly 0.24 influence.

    Returns:
        Reliability in ``[0, ceiling]``.
    """
    if brier_skill <= 0:
        return 0.0
    return min(ceiling, brier_skill * scale)
