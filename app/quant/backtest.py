"""Backtesting harness and the model promotion gate.

This is the phase that decides whether anything the models produce is allowed
in front of a user.

**Chronological splits only.** A random train/test split on time-series data
leaks the future into the past: a model trained on May and tested on March has
seen results that had not happened yet, and will look excellent while being
worthless. Every split here is by date, and the splitter refuses to produce
overlapping windows.

**Walk-forward evaluation.** Rather than one train/test cut, the harness steps
forward through time, refitting on everything available before each window and
predicting the next. That mirrors how the system would actually have run.

**The gate.** A model is promoted only if it beats the market baseline on both
Brier score and log loss on the held-out test period, is calibrated, and has a
sample large enough for the result to mean anything. Failing any of these keeps
``value_detection_enabled`` false — which the application already enforces at
startup.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from app.core.logging import get_logger
from app.quant.metrics import EvaluationResult, evaluate

logger = get_logger(__name__)

MIN_TEST_SAMPLE: Final[int] = 500
"""Minimum held-out forecasts before a result is meaningful.

Below this, the confidence interval on a Brier difference is wider than any
edge worth acting on, and a promotion decision would be noise.
"""

MIN_BRIER_SKILL: Final[float] = 0.005
MAX_CALIBRATION_ERROR: Final[float] = 0.02


class LeakageError(RuntimeError):
    """Raised when an evaluation would use information from the future."""


@dataclass(frozen=True)
class DatedForecast:
    """One forecast, its market counterpart, and what happened.

    Attributes:
        occurred_at: When the event took place. Ordering key for every split.
        model_probability: The model's estimate.
        market_probability: Margin-free market probability for the same event.
        outcome: 1 if the event occurred, 0 otherwise.
        known_at: When the inputs behind the forecast were available. Must
            precede ``occurred_at``, or the forecast used the result.
    """

    occurred_at: datetime
    model_probability: float
    market_probability: float
    outcome: int
    known_at: datetime | None = None
    label: str | None = None


@dataclass(frozen=True)
class Split:
    """One chronological train/test window."""

    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    train_size: int
    test_size: int


@dataclass
class BacktestResult:
    """Outcome of a full walk-forward backtest."""

    splits: list[Split] = field(default_factory=list)
    per_split: list[EvaluationResult] = field(default_factory=list)
    overall: EvaluationResult | None = None

    @property
    def splits_beating_market(self) -> int:
        """How many windows the model won."""
        return sum(1 for result in self.per_split if result.beats_market)

    @property
    def consistency(self) -> float:
        """Fraction of windows beaten.

        A model that wins overall but loses most windows is usually riding one
        lucky period rather than holding an edge.
        """
        if not self.per_split:
            return 0.0
        return self.splits_beating_market / len(self.per_split)


def assert_no_leakage(forecasts: Sequence[DatedForecast]) -> None:
    """Verify no forecast used information from after the event.

    Raises:
        LeakageError: If any forecast's inputs postdate its event.
    """
    for forecast in forecasts:
        if forecast.known_at is None:
            continue
        if forecast.known_at >= forecast.occurred_at:
            raise LeakageError(
                f"Forecast for {forecast.label or forecast.occurred_at} used "
                f"data known at {forecast.known_at}, which is not before the "
                f"event at {forecast.occurred_at}. This is future information."
            )


def chronological_splits(
    forecasts: Sequence[DatedForecast],
    folds: int = 4,
    min_train: int = 100,
) -> list[tuple[list[DatedForecast], list[DatedForecast]]]:
    """Produce expanding-window train/test folds ordered by date.

    Each fold trains on everything before its test window, so the training set
    grows and never contains anything from the future.

    Args:
        forecasts: All forecasts, in any order; sorted internally.
        folds: Number of test windows.
        min_train: Minimum training observations before the first test window.

    Raises:
        LeakageError: If the data cannot be split without overlap.
    """
    if folds < 1:
        raise LeakageError("At least one fold is required.")

    ordered = sorted(forecasts, key=lambda f: f.occurred_at)
    total = len(ordered)
    if total < min_train + folds:
        raise LeakageError(
            f"Need at least {min_train + folds} forecasts for {folds} folds "
            f"with a minimum training set of {min_train}; got {total}."
        )

    window = (total - min_train) // folds
    result: list[tuple[list[DatedForecast], list[DatedForecast]]] = []

    for index in range(folds):
        train_end = min_train + index * window
        test_end = train_end + window if index < folds - 1 else total
        train = ordered[:train_end]
        test = ordered[train_end:test_end]
        if not test:
            continue
        if train[-1].occurred_at > test[0].occurred_at:
            raise LeakageError(
                "Training data postdates test data; the split is not " "chronological."
            )
        result.append((train, test))
    return result


def _score(forecasts: Sequence[DatedForecast], bins: int) -> EvaluationResult:
    """Score a set of forecasts against their market counterparts."""
    return evaluate(
        [f.model_probability for f in forecasts],
        [f.market_probability for f in forecasts],
        [f.outcome for f in forecasts],
        bins=bins,
    )


def walk_forward(
    forecasts: Sequence[DatedForecast],
    folds: int = 4,
    min_train: int = 100,
    bins: int = 10,
    refit: Callable[[Sequence[DatedForecast]], None] | None = None,
) -> BacktestResult:
    """Run an expanding-window backtest.

    Args:
        forecasts: Every forecast with its outcome.
        folds: Number of test windows.
        min_train: Minimum training observations before the first window.
        bins: Calibration bucket count.
        refit: Optional hook called with the training slice before each window,
            for models that need refitting. The harness does not assume a
            model is stateless.

    Returns:
        Per-window and overall results.
    """
    assert_no_leakage(forecasts)
    result = BacktestResult()

    for index, (train, test) in enumerate(chronological_splits(forecasts, folds, min_train)):
        if refit is not None:
            refit(train)

        result.splits.append(
            Split(
                index=index,
                train_start=train[0].occurred_at,
                train_end=train[-1].occurred_at,
                test_start=test[0].occurred_at,
                test_end=test[-1].occurred_at,
                train_size=len(train),
                test_size=len(test),
            )
        )
        result.per_split.append(_score(test, bins))

    tested = [f for _, test in chronological_splits(forecasts, folds, min_train) for f in test]
    if tested:
        result.overall = _score(tested, bins)

    logger.info(
        "backtest.completed",
        folds=len(result.per_split),
        consistency=round(result.consistency, 3),
        overall=result.overall.summary() if result.overall else None,
    )
    return result


class PromotionCriteria(Protocol):
    """A rule deciding whether a model may face users."""

    def evaluate(self, result: BacktestResult) -> tuple[bool, list[str]]:
        """Return ``(passed, reasons)``."""
        ...


@dataclass(frozen=True)
class DefaultPromotionCriteria:
    """The Phase 8B gate.

    Every threshold is configuration, and every failure is reported by name so
    a rejected model produces a diagnosis rather than a verdict.
    """

    min_test_sample: int = MIN_TEST_SAMPLE
    min_brier_skill: float = MIN_BRIER_SKILL
    max_calibration_error: float = MAX_CALIBRATION_ERROR
    min_consistency: float = 0.5

    def evaluate(self, result: BacktestResult) -> tuple[bool, list[str]]:
        """Assess a backtest against the promotion thresholds."""
        failures: list[str] = []

        if result.overall is None:
            return False, ["No backtest results to assess."]

        overall = result.overall

        if overall.sample_size < self.min_test_sample:
            failures.append(
                f"Sample of {overall.sample_size} is below the minimum "
                f"{self.min_test_sample}; a difference this size would be "
                "indistinguishable from noise."
            )

        if overall.brier_skill < self.min_brier_skill:
            failures.append(
                f"Brier skill {overall.brier_skill:+.4f} is below the required "
                f"{self.min_brier_skill:+.4f}. The model does not improve on "
                "the market it would be betting into."
            )

        if overall.log_loss_skill <= 0:
            failures.append(
                f"Log loss skill {overall.log_loss_skill:+.4f} is not positive. "
                "The model makes more confident mistakes than the market."
            )

        # Compared against the noise floor rather than raw, because raw
        # calibration error is inflated by sampling scatter at small samples.
        excess = overall.calibration.excess_calibration_error
        if excess > self.max_calibration_error:
            failures.append(
                f"Calibration error {overall.calibration.expected_calibration_error:.4f} "
                f"exceeds the sampling noise floor "
                f"({overall.calibration.noise_floor:.4f}) by {excess:.4f}, above "
                f"the permitted {self.max_calibration_error:.4f}. Miscalibration "
                "of this size produces apparent edge that is model error rather "
                "than market error."
            )

        if result.consistency < self.min_consistency:
            failures.append(
                f"Model beat the market in only {result.consistency:.0%} of "
                f"windows, below the required {self.min_consistency:.0%}. An "
                "overall win on this few windows is likely one lucky period."
            )

        return not failures, failures


@dataclass(frozen=True)
class PromotionDecision:
    """The outcome of assessing a model for release."""

    model_version: str
    passed: bool
    failures: tuple[str, ...]
    result: BacktestResult

    def report(self) -> str:
        """Return a human-readable decision."""
        header = (
            f"Model '{self.model_version}': " f"{'PROMOTED' if self.passed else 'NOT PROMOTED'}"
        )
        if self.result.overall:
            header += f"\n  {self.result.overall.summary()}"
            header += f"\n  consistency: {self.result.consistency:.0%}"
        if self.failures:
            header += "\n  blocked by:"
            for failure in self.failures:
                header += f"\n    - {failure}"
        return header


def assess_for_promotion(
    model_version: str,
    result: BacktestResult,
    criteria: PromotionCriteria | None = None,
) -> PromotionDecision:
    """Decide whether a model version may be enabled for users.

    A pass here is a precondition, not an authorisation. Enabling the feature
    still requires setting ``FEATURES__PROMOTED_MODEL_VERSION`` explicitly, so
    no automated process can put an unreviewed model in front of users.
    """
    rules = criteria or DefaultPromotionCriteria()
    passed, failures = rules.evaluate(result)

    decision = PromotionDecision(
        model_version=model_version,
        passed=passed,
        failures=tuple(failures),
        result=result,
    )
    logger.info(
        "model.promotion_assessed",
        model_version=model_version,
        passed=passed,
        failure_count=len(failures),
    )
    return decision
