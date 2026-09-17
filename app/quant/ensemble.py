"""Ensemble model.

Combines the independent estimators into one model probability, then blends
that toward the market prior according to measured reliability.

::

    Poisson ─┐
    Elo ─────┼─► weighted model estimate ─► market-prior blend ─► posterior
    Form ────┘

**Weights are configuration, not truth.** The defaults below are starting
points, not findings. They are recorded on every prediction via
``model_versions`` so a result can be traced to the weights that produced it,
and Phase 8B is where backtesting decides what they should actually be.

**Unavailable components are dropped, not defaulted.** If a team has no
historical coverage, Poisson does not contribute a neutral 1/3 — it contributes
nothing and the remaining weights are renormalised. Substituting a placeholder
would let a fixture with no data look identically confident to one with a
decade of it.

**A fixture with no reliable component is not predicted at all.** It is marked
unmodellable and skipped, because the honest answer is that we do not know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from app.quant.market_prior import Combination, combine_market

DEFAULT_WEIGHTS: Final[dict[str, float]] = {
    "poisson": 0.50,
    "elo": 0.35,
    "form": 0.15,
}
"""Starting weights, to be replaced by whatever backtesting shows.

Poisson leads because it models the goal process directly and yields every
derived market rather than 1X2 alone. Form is smallest because it is the
noisiest signal and overlaps heavily with recent Elo movement.
"""

OUTCOMES: Final[tuple[str, ...]] = ("home", "draw", "away")


class EnsembleError(ValueError):
    """Raised when an ensemble cannot be formed."""


@dataclass
class ComponentEstimate:
    """One model's contribution to a fixture.

    Attributes:
        reliable: Whether this component had enough data. An unreliable
            component is excluded entirely rather than down-weighted, so a
            prediction is never part guess.
    """

    name: str
    probabilities: dict[str, Decimal]
    reliable: bool = True

    def validate(self) -> None:
        """Check the estimate is a distribution over the expected outcomes.

        Raises:
            EnsembleError: If outcomes are missing or probabilities do not sum
                to one.
        """
        missing = set(OUTCOMES) - set(self.probabilities)
        if missing:
            raise EnsembleError(f"Component '{self.name}' is missing outcomes: {sorted(missing)}.")
        total = sum(self.probabilities[o] for o in OUTCOMES)
        if abs(total - Decimal(1)) > Decimal("0.01"):
            raise EnsembleError(f"Component '{self.name}' probabilities sum to {total}, not 1.0.")


@dataclass
class EnsembleResult:
    """A fixture's blended model estimate and final posterior."""

    model_probabilities: dict[str, Decimal]
    posterior: dict[str, Decimal] = field(default_factory=dict)
    combinations: dict[str, Combination] = field(default_factory=dict)
    components_used: tuple[str, ...] = ()
    components_dropped: tuple[str, ...] = ()
    effective_weights: dict[str, float] = field(default_factory=dict)
    reliability: float = 0.0

    @property
    def is_usable(self) -> bool:
        """Whether at least one component contributed."""
        return bool(self.components_used)

    @property
    def any_capped(self) -> bool:
        """Whether any outcome hit the maximum permitted deviation.

        A capped shift is a warning sign: it usually means a data fault rather
        than a genuine disagreement with the market.
        """
        return any(c.was_capped for c in self.combinations.values())

    def summary(self) -> str:
        """Return a one-line human summary."""
        used = ", ".join(self.components_used) or "none"
        dropped = ", ".join(self.components_dropped) or "none"
        return (
            f"used=[{used}] dropped=[{dropped}] "
            f"reliability={self.reliability:.3f} "
            f"posterior={{"
            + ", ".join(f"{k}={float(v):.3f}" for k, v in self.posterior.items())
            + "}"
        )


def blend_components(
    components: list[ComponentEstimate],
    weights: dict[str, float] | None = None,
) -> tuple[dict[str, Decimal], tuple[str, ...], tuple[str, ...], dict[str, float]]:
    """Combine component estimates into one model probability.

    Args:
        components: Estimates from each model.
        weights: Component weights. Defaults to :data:`DEFAULT_WEIGHTS`.

    Returns:
        ``(probabilities, used, dropped, effective_weights)``.

    Raises:
        EnsembleError: If no component is reliable.
    """
    configured = weights or DEFAULT_WEIGHTS

    usable = [c for c in components if c.reliable]
    dropped = tuple(c.name for c in components if not c.reliable)

    if not usable:
        raise EnsembleError(
            "No reliable component. This fixture cannot be modelled and must "
            "be reported as unmodellable rather than predicted."
        )

    for component in usable:
        component.validate()

    # Renormalise over whichever components survived, so dropping one does not
    # quietly shrink the total weight and flatten the distribution.
    # An unrecognised component name resolves to a weight of zero and is
    # silently dropped while appearing in ``components_used``. That is how
    # renaming the Poisson component to "poisson_dc" handed its entire 0.50
    # share to Elo and Form with the whole suite still passing. Unknown names
    # now fail loudly.
    unknown = [c.name for c in usable if c.name not in configured]
    if unknown:
        raise EnsembleError(
            f"Components {sorted(unknown)} have no configured weight. "
            f"Known components: {sorted(configured)}. A component with no "
            "weight contributes nothing while still being reported as used."
        )

    total_weight = sum(configured.get(c.name, 0.0) for c in usable)
    if total_weight <= 0:
        raise EnsembleError(f"Components {[c.name for c in usable]} carry no configured weight.")

    effective = {c.name: configured.get(c.name, 0.0) / total_weight for c in usable}

    blended: dict[str, Decimal] = {}
    for outcome in OUTCOMES:
        blended[outcome] = sum(
            (c.probabilities[outcome] * Decimal(str(effective[c.name])) for c in usable),
            Decimal(0),
        )

    total = sum(blended.values())
    normalised = {k: v / total for k, v in blended.items()}
    return normalised, tuple(c.name for c in usable), dropped, effective


def build_ensemble(
    components: list[ComponentEstimate],
    market_prior: dict[str, Decimal],
    reliability: float,
    weights: dict[str, float] | None = None,
) -> EnsembleResult:
    """Blend components, then shrink the result toward the market prior.

    Args:
        components: Estimates from each independent model.
        market_prior: Margin-free market probabilities for the same outcomes.
        reliability: Measured model skill in ``[0, 1]``, from backtesting. Zero
            returns the market prior unchanged.
        weights: Component weights.

    Returns:
        The ensemble result, including the posterior actually used.

    Raises:
        EnsembleError: If no component is reliable or inputs are malformed.
    """
    model_probabilities, used, dropped, effective = blend_components(components, weights)

    combinations = combine_market(market_prior, model_probabilities, reliability)

    return EnsembleResult(
        model_probabilities=model_probabilities,
        posterior={label: c.posterior for label, c in combinations.items()},
        combinations=combinations,
        components_used=used,
        components_dropped=dropped,
        effective_weights=effective,
        reliability=reliability,
    )
