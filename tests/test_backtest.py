"""Phase 7B tests: metrics, backtesting and the promotion gate.

The gate tests are adversarial by design. Each one constructs a model that
would look good under a naive check and asserts the gate rejects it: a
perfectly calibrated but useless forecaster, a model with a tiny sample, a
model that won overall on one lucky window.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.quant.backtest import (
    BacktestResult,
    DatedForecast,
    DefaultPromotionCriteria,
    LeakageError,
    assert_no_leakage,
    assess_for_promotion,
    chronological_splits,
    walk_forward,
)
from app.quant.metrics import (
    MetricError,
    brier_score,
    calibration,
    evaluate,
    log_loss,
    skill_score,
)

START = datetime(2024, 8, 1, tzinfo=UTC)


def _forecasts(
    count: int,
    model_noise: float = 0.0,
    market_noise: float = 0.0,
    seed: int = 7,
) -> list[DatedForecast]:
    """Build forecasts where the true probability is known.

    Outcomes are drawn from the true probability, so a model given zero noise
    is exactly right and a model given noise is wrong by a known amount. That
    makes the gate's behaviour checkable rather than merely plausible.
    """
    rng = random.Random(seed)
    result: list[DatedForecast] = []
    for index in range(count):
        truth = rng.uniform(0.15, 0.85)
        outcome = 1 if rng.random() < truth else 0
        result.append(
            DatedForecast(
                occurred_at=START + timedelta(hours=index),
                model_probability=min(max(truth + rng.gauss(0, model_noise), 0.01), 0.99),
                market_probability=min(max(truth + rng.gauss(0, market_noise), 0.01), 0.99),
                outcome=outcome,
                known_at=START + timedelta(hours=index) - timedelta(hours=1),
                label=f"event-{index}",
            )
        )
    return result


class TestScoringMetrics:
    """Brier and log loss behave as proper scoring rules."""

    def test_perfect_forecast_scores_zero(self) -> None:
        assert brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == 0.0

    def test_coin_flip_scores_quarter(self) -> None:
        assert brier_score([0.5, 0.5], [1, 0]) == 0.25

    def test_confident_error_is_punished_hardest(self) -> None:
        """The behaviour that makes log loss worth using alongside Brier."""
        cautious = log_loss([0.4], [0])
        reckless = log_loss([0.99], [0])
        assert reckless > cautious * 5

    def test_log_loss_does_not_diverge(self) -> None:
        """One overconfident forecast must not destroy an evaluation."""
        assert log_loss([1.0], [0]) < 40

    def test_mismatched_inputs_rejected(self) -> None:
        with pytest.raises(MetricError, match="align"):
            brier_score([0.5, 0.5], [1])

    def test_empty_inputs_rejected(self) -> None:
        with pytest.raises(MetricError, match="empty"):
            brier_score([], [])

    def test_invalid_probability_rejected(self) -> None:
        with pytest.raises(MetricError, match=r"\[0, 1\]"):
            brier_score([1.5], [1])

    def test_invalid_outcome_rejected(self) -> None:
        with pytest.raises(MetricError, match="0 or 1"):
            brier_score([0.5], [2])

    def test_skill_is_signed(self) -> None:
        assert skill_score(0.18, 0.20) > 0
        assert skill_score(0.22, 0.20) < 0
        assert skill_score(0.20, 0.20) == 0


class TestCalibration:
    """Calibration measures honesty, not sharpness."""

    def test_well_calibrated_forecaster(self) -> None:
        probabilities = [0.1] * 100 + [0.9] * 100
        outcomes = [1] * 10 + [0] * 90 + [1] * 90 + [0] * 10
        report = calibration(probabilities, outcomes)
        assert report.expected_calibration_error < 0.02
        assert report.is_well_calibrated()

    def test_overconfident_forecaster_is_caught(self) -> None:
        probabilities = [0.95] * 100
        outcomes = [1] * 50 + [0] * 50
        report = calibration(probabilities, outcomes)
        assert report.expected_calibration_error > 0.4
        assert not report.is_well_calibrated()

    def test_empty_bins_are_omitted(self) -> None:
        """Counting them as perfect would flatter a narrow forecaster."""
        report = calibration([0.5] * 20, [1] * 10 + [0] * 10)
        assert len(report.bins) == 1

    def test_bins_must_be_meaningful(self) -> None:
        with pytest.raises(MetricError, match="at least two bins"):
            calibration([0.5], [1], bins=1)


class TestEvaluationAgainstMarket:
    """A score means nothing without the market to compare against."""

    def test_better_model_shows_positive_skill(self) -> None:
        forecasts = _forecasts(600, model_noise=0.02, market_noise=0.10)
        result = evaluate(
            [f.model_probability for f in forecasts],
            [f.market_probability for f in forecasts],
            [f.outcome for f in forecasts],
        )
        assert result.brier_skill > 0
        assert result.beats_market

    def test_worse_model_shows_negative_skill(self) -> None:
        forecasts = _forecasts(600, model_noise=0.15, market_noise=0.01)
        result = evaluate(
            [f.model_probability for f in forecasts],
            [f.market_probability for f in forecasts],
            [f.outcome for f in forecasts],
        )
        assert result.brier_skill < 0
        assert not result.beats_market

    def test_both_measures_must_improve(self) -> None:
        """Trading confident errors for vague hedging is not an edge."""
        result = evaluate([0.5] * 100, [0.9] * 50 + [0.1] * 50, [1] * 50 + [0] * 50)
        assert not result.beats_market


class TestLeakagePrevention:
    """The failure that makes a worthless model look excellent."""

    def test_future_information_is_rejected(self) -> None:
        leaky = [
            DatedForecast(
                occurred_at=START,
                model_probability=0.6,
                market_probability=0.5,
                outcome=1,
                known_at=START + timedelta(hours=2),
                label="leaky",
            )
        ]
        with pytest.raises(LeakageError, match="future information"):
            assert_no_leakage(leaky)

    def test_simultaneous_knowledge_is_rejected(self) -> None:
        """Knowing at kickoff is still knowing too much."""
        with pytest.raises(LeakageError):
            assert_no_leakage(
                [
                    DatedForecast(
                        occurred_at=START,
                        model_probability=0.6,
                        market_probability=0.5,
                        outcome=1,
                        known_at=START,
                    )
                ]
            )

    def test_clean_forecasts_pass(self) -> None:
        assert_no_leakage(_forecasts(50))

    def test_splits_are_chronological(self) -> None:
        splits = chronological_splits(_forecasts(500), folds=4, min_train=100)
        for train, test in splits:
            assert max(f.occurred_at for f in train) <= min(f.occurred_at for f in test)

    def test_training_window_expands(self) -> None:
        splits = chronological_splits(_forecasts(500), folds=4, min_train=100)
        sizes = [len(train) for train, _ in splits]
        assert sizes == sorted(sizes)
        assert sizes[0] < sizes[-1]

    def test_unsorted_input_is_still_split_by_time(self) -> None:
        """Ordering must not depend on how the caller happened to pass data."""
        shuffled = _forecasts(500)
        random.Random(1).shuffle(shuffled)
        splits = chronological_splits(shuffled, folds=3, min_train=100)
        for train, test in splits:
            assert max(f.occurred_at for f in train) <= min(f.occurred_at for f in test)

    def test_insufficient_data_is_refused(self) -> None:
        with pytest.raises(LeakageError, match="Need at least"):
            chronological_splits(_forecasts(20), folds=4, min_train=100)


class TestWalkForward:
    """Expanding-window evaluation."""

    def test_produces_a_result_per_window(self) -> None:
        result = walk_forward(_forecasts(600), folds=4, min_train=100)
        assert len(result.per_split) == 4
        assert len(result.splits) == 4
        assert result.overall is not None

    def test_refit_hook_sees_only_past_data(self) -> None:
        """A model that refits must never be handed the future."""
        seen: list[datetime] = []

        def refit(train: object) -> None:
            seen.append(max(f.occurred_at for f in train))  # type: ignore[attr-defined]

        result = walk_forward(_forecasts(600), folds=3, min_train=100, refit=refit)
        for latest_train, split in zip(seen, result.splits, strict=True):
            assert latest_train <= split.test_start

    def test_consistency_is_reported(self) -> None:
        result = walk_forward(_forecasts(800, model_noise=0.02, market_noise=0.12), folds=4)
        assert 0.0 <= result.consistency <= 1.0
        assert result.splits_beating_market >= 0


class TestPromotionGate:
    """Nothing reaches users without clearing every criterion."""

    def test_good_model_is_promoted(self) -> None:
        """A genuinely better model clears the gate given a real sample.

        The sample is deliberately large. The noise-floor correction is a
        normal approximation and slightly under-corrects when buckets are
        small, so a model must be evaluated on enough data for calibration to
        be measurable at all — which is the gate working, not a workaround.
        """
        result = walk_forward(
            _forecasts(2400, model_noise=0.01, market_noise=0.12),
            folds=4,
            min_train=400,
        )
        decision = assess_for_promotion("ensemble-v1", result)
        assert decision.passed, decision.report()

    def test_worse_than_market_is_rejected(self) -> None:
        result = walk_forward(
            _forecasts(1200, model_noise=0.18, market_noise=0.01),
            folds=4,
            min_train=200,
        )
        decision = assess_for_promotion("ensemble-v2", result)
        assert not decision.passed
        assert any("does not improve on the market" in f for f in decision.failures)

    def test_small_sample_is_rejected_even_when_winning(self) -> None:
        """A tiny sample cannot distinguish an edge from luck."""
        result = walk_forward(
            _forecasts(300, model_noise=0.01, market_noise=0.15),
            folds=2,
            min_train=100,
        )
        decision = assess_for_promotion("ensemble-v3", result)
        assert not decision.passed
        assert any("indistinguishable from noise" in f for f in decision.failures)

    def test_miscalibrated_model_is_rejected(self) -> None:
        """Miscalibration manufactures apparent edge out of model error."""
        forecasts = [
            DatedForecast(
                occurred_at=START + timedelta(hours=i),
                model_probability=0.95,
                market_probability=0.5,
                outcome=i % 2,
                label=f"e{i}",
            )
            for i in range(1200)
        ]
        result = walk_forward(forecasts, folds=4, min_train=200)
        decision = assess_for_promotion("overconfident", result)
        assert not decision.passed
        assert any("Calibration error" in f for f in decision.failures)

    def test_empty_backtest_is_rejected(self) -> None:
        decision = assess_for_promotion("nothing", BacktestResult())
        assert not decision.passed

    def test_criteria_are_configurable(self) -> None:
        result = walk_forward(
            _forecasts(2400, model_noise=0.01, market_noise=0.12),
            folds=4,
            min_train=400,
        )
        strict = DefaultPromotionCriteria(min_brier_skill=0.99)
        decision = assess_for_promotion("ensemble-v1", result, criteria=strict)
        assert not decision.passed

    def test_report_names_every_blocker(self) -> None:
        """A rejected model must produce a diagnosis, not just a verdict."""
        result = walk_forward(
            _forecasts(300, model_noise=0.20, market_noise=0.01),
            folds=2,
            min_train=100,
        )
        report = assess_for_promotion("bad", result).report()
        assert "NOT PROMOTED" in report
        assert "blocked by:" in report

    def test_calibration_noise_floor_protects_small_samples(self) -> None:
        """A perfect forecaster must not be condemned by sampling scatter."""
        from app.quant.metrics import calibration

        rng = random.Random(3)
        probabilities: list[float] = []
        outcomes: list[int] = []
        for _ in range(400):
            truth = rng.uniform(0.1, 0.9)
            probabilities.append(truth)
            outcomes.append(1 if rng.random() < truth else 0)

        report = calibration(probabilities, outcomes)
        assert report.noise_floor > 0
        assert report.excess_calibration_error < report.expected_calibration_error

    def test_passing_the_gate_is_not_itself_authorisation(self) -> None:
        """The flag must still be set deliberately.

        The gate is a precondition. Enabling value detection requires naming
        the promoted version in configuration, which the application validates
        at startup, so no automated process can release a model on its own.
        """
        from app.core.config import FeatureFlags

        flags = FeatureFlags(_env_file=None)  # type: ignore[call-arg]
        assert flags.value_detection_enabled is False
        assert flags.promoted_model_version is None
