"""Odds pricing model for mock providers.

Odds are derived from underlying fair probabilities, not from a flat offset
applied to another provider's prices. The distinction matters: a fixed shift
makes one provider uniformly cheaper than the other, so *every* outcome looks
like value against it and a broken value scanner passes its tests.

::

    fair probabilities ─┬─► reference margin model ─► reference odds
                        └─► tradeable margin model ─► tradeable odds

Both providers price the same underlying truth. They differ in how much margin
they take and how they distribute it across outcomes, which is how real books
differ.

**Overround.** A book prices so that implied probabilities sum to more than 1.
The excess is its margin. A sharp reference book runs perhaps 2-3%; a soft
tradeable book 6-10%. Because the reference takes less, its odds are usually
*higher* than the tradeable book's — the opposite of what a naive "reference is
cheaper" shift produces.

**Tilt.** Margin is not spread evenly. Books shade prices toward the side that
attracts money, and lengthen the other. Tilt is what makes a genuine value
opportunity possible: an outcome the tradeable book has lengthened past its
fair price is positive expected value, even though the book's overall margin
is positive.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

TWO_PLACES: Final[Decimal] = Decimal("0.01")

REFERENCE_OVERROUND: Final[Decimal] = Decimal("1.025")
"""Sharp book: 2.5% margin."""

TRADEABLE_OVERROUND: Final[Decimal] = Decimal("1.075")
"""Soft book: 7.5% margin."""


@dataclass(frozen=True)
class OutcomePricing:
    """One outcome's fair probability and the tilt applied by a book.

    Attributes:
        label: Outcome name.
        fair_probability: True probability. Sums to 1 across a market.
        tilt: Multiplier on this outcome's share of the margin. Below 1.0
            lengthens the price (better for the bettor); above 1.0 shortens it.
    """

    label: str
    fair_probability: Decimal
    tilt: Decimal = Decimal("1.0")


def _quantize(value: Decimal) -> Decimal:
    """Round to two decimal places, as books quote."""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def price_market(
    outcomes: tuple[OutcomePricing, ...],
    overround: Decimal,
    apply_tilt: bool = True,
) -> dict[str, Decimal]:
    """Convert fair probabilities into quoted decimal odds.

    Each outcome's quoted probability is its fair probability scaled by the
    overround and by its tilt, then renormalised so the book's total margin is
    exactly ``overround`` regardless of how the tilt is distributed. Without
    that renormalisation the tilt would change the margin as well as its
    distribution, and the two effects could not be reasoned about separately.

    Args:
        outcomes: Fair probabilities and tilts for one market.
        overround: Total implied probability the book quotes to, e.g. 1.075.
        apply_tilt: When False, margin is spread proportionally — the classic
            "no value anywhere" book.

    Returns:
        Mapping of outcome label to quoted decimal odds.

    Raises:
        ValueError: If probabilities are not a valid distribution.
    """
    if not outcomes:
        raise ValueError("Cannot price an empty market.")

    total = sum(o.fair_probability for o in outcomes)
    if abs(total - Decimal("1")) > Decimal("0.0001"):
        raise ValueError(f"Fair probabilities must sum to 1.0, got {total}.")

    weighted = {
        o.label: o.fair_probability * (o.tilt if apply_tilt else Decimal("1")) for o in outcomes
    }
    weighted_total = sum(weighted.values())

    quoted: dict[str, Decimal] = {}
    for label, value in weighted.items():
        implied = (value / weighted_total) * overround
        quoted[label] = _quantize(Decimal("1") / implied)
    return quoted


def implied_probability(odds: Decimal) -> Decimal:
    """Return the raw implied probability of decimal odds, margin included."""
    return Decimal("1") / odds


def remove_margin_proportional(
    odds_by_label: dict[str, Decimal],
) -> dict[str, Decimal]:
    """Strip margin by proportional scaling.

    The simplest method and a biased one: it over-prices longshots and
    under-prices favourites, because a book's margin is not spread evenly.
    Retained as the baseline the Shin and power methods will be compared
    against in Phase 7.
    """
    raw = {label: implied_probability(odds) for label, odds in odds_by_label.items()}
    total = sum(raw.values())
    return {label: value / total for label, value in raw.items()}


def expected_value(probability: Decimal, odds: Decimal) -> Decimal:
    """Return ``(probability * odds) - 1``."""
    return (probability * odds) - Decimal("1")


def stable_fraction(seed: str) -> Decimal:
    """Return a repeatable value in ``[0, 1)`` derived from a string.

    A hash rather than ``random``, so the value depends only on its input and
    is identical across processes, platforms and Python versions.
    """
    digest = hashlib.sha256(seed.encode()).hexdigest()
    return Decimal(int(digest[:8], 16)) / Decimal(0xFFFFFFFF)
