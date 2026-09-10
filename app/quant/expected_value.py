"""Expected value and edge.

``EV = (probability x odds) - 1``. Positive expected value means the price is
longer than the probability justifies.

The arithmetic is trivial. The hard part is that it is only as good as the
probability fed into it, and a model that is 3% miscalibrated produces 3% of
spurious "edge" on every selection. That is why the thresholds here default
well above zero, and why nothing in this module is exposed to users until the
Phase 8B validation gate passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.quant.probability import ProbabilityError, implied_probability

MIN_EDGE: Final[Decimal] = Decimal("0.03")
"""Default minimum expected value.

Not zero. Most apparent edge at low thresholds is model error rather than
market error, so a threshold at zero selects for miscalibration. The value is
configuration, to be tightened or loosened by what backtesting shows.
"""

MIN_PROBABILITY: Final[Decimal] = Decimal("0.05")
MAX_ODDS: Final[Decimal] = Decimal("15.0")
"""Long prices are where model error is largest in relative terms: an estimate
of 4% versus a true 3% is a 33% error, and produces enormous apparent edge."""


@dataclass(frozen=True)
class ValueAssessment:
    """The result of comparing one model probability against one price."""

    model_probability: Decimal
    market_probability: Decimal
    odds: Decimal
    edge: Decimal
    expected_value: Decimal

    @property
    def has_value(self) -> bool:
        """Whether expected value is positive at all."""
        return self.expected_value > 0

    def passes(
        self,
        min_edge: Decimal = MIN_EDGE,
        min_probability: Decimal = MIN_PROBABILITY,
        max_odds: Decimal = MAX_ODDS,
    ) -> bool:
        """Whether this selection clears the configured filters."""
        return (
            self.expected_value >= min_edge
            and self.model_probability >= min_probability
            and self.odds <= max_odds
        )


def expected_value(probability: Decimal, odds: Decimal) -> Decimal:
    """Return the expected value of a unit stake.

    Raises:
        ProbabilityError: If inputs are outside their valid ranges.
    """
    if not 0 < probability <= 1:
        raise ProbabilityError(f"Probability must be in (0, 1], got {probability}.")
    if odds <= 1:
        raise ProbabilityError(f"Decimal odds must exceed 1.0, got {odds}.")
    return (probability * odds) - Decimal(1)


def edge(model_probability: Decimal, market_probability: Decimal) -> Decimal:
    """Return the difference between model and market probability.

    Distinct from expected value: edge is in probability space and is the more
    natural quantity for calibration work, while expected value is in return
    space and is what determines whether a bet is worth making.
    """
    return model_probability - market_probability


def assess(
    model_probability: Decimal,
    odds: Decimal,
    market_probability: Decimal | None = None,
) -> ValueAssessment:
    """Compare a model probability against a quoted price.

    Args:
        model_probability: The model's estimate.
        odds: The tradeable decimal price.
        market_probability: Fair market probability, if margin has already been
            removed. Defaults to the raw implied probability, which includes
            margin and therefore understates edge.
    """
    market = market_probability if market_probability is not None else implied_probability(odds)
    return ValueAssessment(
        model_probability=model_probability,
        market_probability=market,
        odds=odds,
        edge=edge(model_probability, market),
        expected_value=expected_value(model_probability, odds),
    )


def kelly_fraction(probability: Decimal, odds: Decimal, cap: Decimal = Decimal("0.25")) -> Decimal:
    """Return the Kelly-optimal stake as a fraction of bankroll.

    Full Kelly is optimal only when the probability is exactly right, which it
    never is. Because Kelly is highly sensitive to overestimated probability,
    the result is capped — a fractional stake gives up little growth and
    survives being wrong, which full Kelly does not.

    Returns:
        The capped fraction, or zero when there is no edge.
    """
    value = expected_value(probability, odds)
    if value <= 0:
        return Decimal(0)
    fraction = value / (odds - Decimal(1))
    return min(fraction, cap)
