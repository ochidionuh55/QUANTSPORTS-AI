"""Scoring and calibration metrics.

These decide whether the model is allowed to face users. Accuracy is not among
them, and deliberately so: a forecaster that says 55% home win on every match
in a league where 45% of home sides win will look "accurate" while being
useless, because it never distinguishes one fixture from another.

What matters instead:

* **Brier score** — mean squared error of probabilities. Proper: it cannot be
  improved by misreporting a belief.
* **Log loss** — punishes confident mistakes far harder. A 1% probability
  assigned to something that happens is close to unrecoverable.
* **Calibration** — of the matches given a 30% chance, did roughly 30% happen?
  A model can discriminate well and still be badly calibrated, and expected
  value calculations depend entirely on calibration.
* **Skill versus the market** — the only comparison that decides anything. A
  Brier score of 0.20 means nothing alone; against a market baseline of 0.19 it
  means the model is worse than the price it is betting into.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

EPSILON: Final[float] = 1e-15
"""Clamp for log loss. A probability of exactly 0 or 1 gives infinite loss,
which would let one overconfident forecast destroy an entire evaluation."""

DEFAULT_BINS: Final[int] = 10


class MetricError(ValueError):
    """Raised when inputs cannot be scored."""


def _validate(probabilities: list[float], outcomes: list[int]) -> None:
    """Check paired inputs.

    Raises:
        MetricError: If lengths differ, inputs are empty, or values are invalid.
    """
    if not probabilities:
        raise MetricError("Cannot score an empty set of forecasts.")
    if len(probabilities) != len(outcomes):
        raise MetricError(
            f"Forecasts and outcomes must align: {len(probabilities)} versus " f"{len(outcomes)}."
        )
    if any(not 0 <= p <= 1 for p in probabilities):
        raise MetricError("Probabilities must lie in [0, 1].")
    if any(o not in (0, 1) for o in outcomes):
        raise MetricError("Outcomes must be 0 or 1.")


def brier_score(probabilities: list[float], outcomes: list[int]) -> float:
    """Return the mean squared error. Lower is better; 0.25 is a coin flip."""
    _validate(probabilities, outcomes)
    return sum((p - o) ** 2 for p, o in zip(probabilities, outcomes, strict=True)) / len(
        probabilities
    )


def log_loss(probabilities: list[float], outcomes: list[int]) -> float:
    """Return the mean negative log likelihood. Lower is better."""
    _validate(probabilities, outcomes)
    total = 0.0
    for p, o in zip(probabilities, outcomes, strict=True):
        clamped = min(max(p, EPSILON), 1 - EPSILON)
        total -= math.log(clamped) if o == 1 else math.log(1 - clamped)
    return total / len(probabilities)


def skill_score(model: float, baseline: float) -> float:
    """Return improvement over a baseline as a fraction.

    Positive means better than the baseline, zero means indistinguishable,
    negative means worse. This is the number that decides promotion.
    """
    if baseline <= 0:
        return 0.0
    return (baseline - model) / baseline


@dataclass(frozen=True)
class CalibrationBin:
    """One bucket of a calibration curve."""

    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_rate: float

    @property
    def gap(self) -> float:
        """Absolute difference between predicted and observed."""
        return abs(self.mean_predicted - self.observed_rate)


@dataclass(frozen=True)
class CalibrationReport:
    """Calibration across all buckets."""

    bins: tuple[CalibrationBin, ...]
    expected_calibration_error: float
    maximum_calibration_error: float
    sample_size: int
    noise_floor: float = 0.0
    """Expected calibration error of a *perfectly* calibrated forecaster at
    this sample size and bin count.

    Calibration error is biased upward: observed rates in each bucket are
    binomial draws, so they scatter around the true rate even when the model is
    exactly right. With 100 observations in a bucket the scatter alone produces
    roughly 0.04 of apparent error. Comparing raw error against a fixed
    threshold therefore measures sample size rather than the model, and would
    reject a perfect forecaster on a small dataset while passing a poor one on
    a large dataset.
    """

    @property
    def excess_calibration_error(self) -> float:
        """Calibration error above what sampling noise alone explains."""
        return max(0.0, self.expected_calibration_error - self.noise_floor)

    def is_well_calibrated(self, threshold: float = 0.02) -> bool:
        """Whether miscalibration exceeds both the threshold and the noise floor.

        Both tests must fail for a model to be judged miscalibrated, so a small
        sample cannot condemn a good model and a large one cannot excuse a bad
        one.
        """
        return self.excess_calibration_error < threshold


def calibration(
    probabilities: list[float], outcomes: list[int], bins: int = DEFAULT_BINS
) -> CalibrationReport:
    """Bucket forecasts and compare predicted against observed frequency.

    Empty buckets are omitted rather than counted as perfectly calibrated,
    which would flatter a model that never makes predictions in that range.
    """
    _validate(probabilities, outcomes)
    if bins < 2:
        raise MetricError("Calibration needs at least two bins.")

    width = 1.0 / bins
    computed: list[CalibrationBin] = []
    weighted_error = 0.0
    worst = 0.0

    for index in range(bins):
        lower = index * width
        upper = lower + width
        members = [
            (p, o)
            for p, o in zip(probabilities, outcomes, strict=True)
            if (lower <= p < upper) or (index == bins - 1 and p == 1.0)
        ]
        if not members:
            continue

        count = len(members)
        mean_predicted = sum(p for p, _ in members) / count
        observed = sum(o for _, o in members) / count
        gap = abs(mean_predicted - observed)

        weighted_error += gap * count
        worst = max(worst, gap)
        computed.append(CalibrationBin(lower, upper, count, mean_predicted, observed))

    return CalibrationReport(
        bins=tuple(computed),
        expected_calibration_error=weighted_error / len(probabilities),
        maximum_calibration_error=worst,
        sample_size=len(probabilities),
        noise_floor=_noise_floor(computed, len(probabilities)),
    )


def _noise_floor(bins: list[CalibrationBin], total: int) -> float:
    """Estimate the calibration error a perfect forecaster would still show.

    Each bucket's observed rate is a binomial proportion with standard error
    ``sqrt(p(1-p)/n)``. The expected absolute deviation of a normal variable is
    ``sigma * sqrt(2/pi)``, so summing that across buckets, weighted by size,
    gives the error attributable to sampling alone.
    """
    if not bins or total == 0:
        return 0.0
    expected_absolute = (2 / math.pi) ** 0.5
    weighted = 0.0
    for bucket in bins:
        p = min(max(bucket.mean_predicted, 1e-9), 1 - 1e-9)
        sigma = (p * (1 - p) / bucket.count) ** 0.5
        weighted += sigma * expected_absolute * bucket.count
    return weighted / total


@dataclass(frozen=True)
class EvaluationResult:
    """A model scored against a market baseline on one dataset."""

    sample_size: int
    model_brier: float
    market_brier: float
    model_log_loss: float
    market_log_loss: float
    calibration: CalibrationReport

    @property
    def brier_skill(self) -> float:
        """Fractional improvement in Brier score over the market."""
        return skill_score(self.model_brier, self.market_brier)

    @property
    def log_loss_skill(self) -> float:
        """Fractional improvement in log loss over the market."""
        return skill_score(self.model_log_loss, self.market_log_loss)

    @property
    def beats_market(self) -> bool:
        """Whether the model improves on the market by both measures.

        Both, not either. Improving one while worsening the other usually means
        the model has traded confident errors for vague hedging, which is not
        an edge.
        """
        return self.brier_skill > 0 and self.log_loss_skill > 0

    def summary(self) -> str:
        """Return a one-line human summary."""
        return (
            f"n={self.sample_size} "
            f"brier={self.model_brier:.5f} (market {self.market_brier:.5f}, "
            f"skill {self.brier_skill:+.2%}) "
            f"logloss={self.model_log_loss:.5f} "
            f"(market {self.market_log_loss:.5f}, skill {self.log_loss_skill:+.2%}) "
            f"ece={self.calibration.expected_calibration_error:.4f}"
        )


def evaluate(
    model_probabilities: list[float],
    market_probabilities: list[float],
    outcomes: list[int],
    bins: int = DEFAULT_BINS,
) -> EvaluationResult:
    """Score a model against the market on the same events.

    Args:
        model_probabilities: The model's forecasts.
        market_probabilities: Margin-free market probabilities for the same
            events, in the same order.
        outcomes: 1 if the event occurred, 0 otherwise.
        bins: Calibration bucket count.
    """
    _validate(model_probabilities, outcomes)
    _validate(market_probabilities, outcomes)

    return EvaluationResult(
        sample_size=len(outcomes),
        model_brier=brier_score(model_probabilities, outcomes),
        market_brier=brier_score(market_probabilities, outcomes),
        model_log_loss=log_loss(model_probabilities, outcomes),
        market_log_loss=log_loss(market_probabilities, outcomes),
        calibration=calibration(model_probabilities, outcomes, bins),
    )
