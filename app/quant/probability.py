"""Probability conversion and margin removal.

Bookmaker odds do not state probabilities. They state prices that include the
book's margin, so implied probabilities sum to more than one. Recovering a fair
probability means deciding *how* the book distributed that margin, and the
answer is a modelling choice with real consequences — which is why the method
is pluggable and configuration-driven rather than hardcoded.

Three strategies:

* **Proportional** scales every implied probability by the same factor. Simple,
  and biased: it assumes margin is spread evenly, which books do not do. It
  systematically over-prices longshots and under-prices favourites. Kept as the
  baseline that the others are measured against.
* **Power** solves for an exponent such that ``sum(p ** k) == 1``. Removes more
  margin from longshots than favourites, which matches the observed shape of
  the favourite-longshot bias.
* **Shin** models the margin as arising from a proportion of insider money.
  Usually the best-calibrated of the three on football markets.

None is assumed correct. Phase 7B compares them on held-out data, and whichever
wins becomes the configured default — recorded per prediction in
``predictions.margin_method`` so any result stays reproducible.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

TOLERANCE: Final[Decimal] = Decimal("0.0000001")
MAX_ITERATIONS: Final[int] = 200


class MarginMethod(StrEnum):
    """Available margin removal strategies."""

    PROPORTIONAL = "proportional"
    POWER = "power"
    SHIN = "shin"


class ProbabilityError(ValueError):
    """Raised when odds or probabilities are unusable."""


def implied_probability(odds: Decimal) -> Decimal:
    """Return the raw implied probability of decimal odds.

    Includes the book's margin. Never use this as a fair probability.

    Raises:
        ProbabilityError: If odds do not exceed 1.0.
    """
    if odds <= 1:
        raise ProbabilityError(f"Decimal odds must exceed 1.0, got {odds}.")
    return Decimal(1) / odds


def overround(odds: list[Decimal]) -> Decimal:
    """Return the book's total implied probability.

    A value of 1.07 means a 7% margin. Below 1.0 would be an arbitrage and in
    practice means stale or erroneous prices.
    """
    return sum((implied_probability(o) for o in odds), Decimal(0))


class MarginRemovalStrategy(Protocol):
    """Converts quoted odds into fair probabilities summing to one."""

    @property
    def method(self) -> MarginMethod:
        """Which strategy this is."""
        ...

    def remove(self, odds: list[Decimal]) -> list[Decimal]:
        """Return fair probabilities in the same order as ``odds``."""
        ...


def _validate(odds: list[Decimal]) -> list[Decimal]:
    """Check a market's prices and return raw implied probabilities.

    Raises:
        ProbabilityError: If the market is empty or already sums below 1.0.
    """
    if len(odds) < 2:
        raise ProbabilityError(
            "Margin removal needs a complete market of at least two outcomes; "
            "a single price carries no information about the book's margin."
        )
    raw = [implied_probability(o) for o in odds]
    total = sum(raw, Decimal(0))
    if total <= 1:
        raise ProbabilityError(
            f"Implied probabilities sum to {total}, which is at or below 1.0. "
            "This is either an incomplete market or stale prices, and must not "
            "be treated as a fair market."
        )
    return raw


class ProportionalMargin:
    """Scales every implied probability by the same factor."""

    @property
    def method(self) -> MarginMethod:
        """Strategy identifier."""
        return MarginMethod.PROPORTIONAL

    def remove(self, odds: list[Decimal]) -> list[Decimal]:
        """Return fair probabilities by proportional scaling."""
        raw = _validate(odds)
        total = sum(raw, Decimal(0))
        return [p / total for p in raw]


class PowerMargin:
    """Solves for an exponent ``k`` such that fair probabilities sum to one."""

    @property
    def method(self) -> MarginMethod:
        """Strategy identifier."""
        return MarginMethod.POWER

    def remove(self, odds: list[Decimal]) -> list[Decimal]:
        """Return fair probabilities via the power method.

        Bisection rather than Newton's method: the function is monotonic in
        ``k``, bisection cannot diverge, and 200 iterations is microseconds for
        a market of three outcomes.
        """
        raw = _validate(odds)
        low, high = Decimal("0.5"), Decimal("2.0")

        for _ in range(MAX_ITERATIONS):
            mid = (low + high) / 2
            total = sum(p ** float(mid) for p in map(float, raw))
            if abs(total - 1.0) < float(TOLERANCE):
                break
            if total > 1.0:
                low = mid
            else:
                high = mid

        exponent = float((low + high) / 2)
        adjusted = [Decimal(str(float(p) ** exponent)) for p in raw]
        total_adjusted = sum(adjusted, Decimal(0))
        return [p / total_adjusted for p in adjusted]


class ShinMargin:
    """Models margin as arising from a proportion of insider money.

    Shin's insight is that a book widens prices to protect itself against
    better-informed bettors, and does so more on outcomes where that risk is
    concentrated. The parameter ``z`` is that insider proportion, solved
    numerically.
    """

    @property
    def method(self) -> MarginMethod:
        """Strategy identifier."""
        return MarginMethod.SHIN

    def remove(self, odds: list[Decimal]) -> list[Decimal]:
        """Return fair probabilities via Shin's method."""
        raw = [float(p) for p in _validate(odds)]
        total = sum(raw)

        low, high = 0.0, 0.2
        for _ in range(MAX_ITERATIONS):
            z = (low + high) / 2
            probabilities = self._shin_probabilities(raw, total, z)
            current = sum(probabilities)
            if abs(current - 1.0) < float(TOLERANCE):
                break
            if current > 1.0:
                low = z
            else:
                high = z

        z = (low + high) / 2
        adjusted = self._shin_probabilities(raw, total, z)
        adjusted_total = sum(adjusted)
        return [Decimal(str(p / adjusted_total)) for p in adjusted]

    @staticmethod
    def _shin_probabilities(raw: list[float], total: float, z: float) -> list[float]:
        """Apply Shin's transformation for a given insider proportion."""
        if z <= 0:
            return [p / total for p in raw]
        result = []
        for p in raw:
            inner = (z * z) + 4 * (1 - z) * (p * p) / total
            result.append((max(inner, 0.0) ** 0.5 - z) / (2 * (1 - z)))
        return result


_STRATEGIES: Final[dict[MarginMethod, MarginRemovalStrategy]] = {
    MarginMethod.PROPORTIONAL: ProportionalMargin(),
    MarginMethod.POWER: PowerMargin(),
    MarginMethod.SHIN: ShinMargin(),
}


def get_strategy(method: MarginMethod | str) -> MarginRemovalStrategy:
    """Return the strategy for a method.

    Raises:
        ProbabilityError: If the method is unknown.
    """
    try:
        return _STRATEGIES[MarginMethod(method)]
    except (KeyError, ValueError):
        raise ProbabilityError(
            f"Unknown margin method '{method}'. " f"Available: {[m.value for m in MarginMethod]}."
        ) from None


def fair_probabilities(
    odds: list[Decimal], method: MarginMethod | str = MarginMethod.SHIN
) -> list[Decimal]:
    """Convert quoted odds into fair probabilities.

    Args:
        odds: Every price in one complete market.
        method: Margin removal strategy.

    Returns:
        Fair probabilities, in input order, summing to one.
    """
    return get_strategy(method).remove(odds)


def fair_odds(probability: Decimal) -> Decimal:
    """Return the break-even odds for a probability.

    Raises:
        ProbabilityError: If the probability is not strictly between 0 and 1.
    """
    if not 0 < probability < 1:
        raise ProbabilityError(f"Probability must be strictly between 0 and 1, got {probability}.")
    return Decimal(1) / probability
