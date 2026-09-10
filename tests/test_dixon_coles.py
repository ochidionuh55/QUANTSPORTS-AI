"""Dixon-Coles tests.

The correction has one job: raise the probability of 0-0 and 1-1 while lowering
1-0 and 0-1, leaving everything else untouched. If it moves any other scoreline
it is not Dixon-Coles, and if it fails to raise draws it is not doing anything.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from app.quant.dixon_coles import (
    DEFAULT_RHO,
    MIN_MATCHES_FOR_RHO,
    estimate_rho,
    log_likelihood,
    score_matrix,
    tau,
)
from app.quant.dixon_coles import (
    match_probabilities as dixon_coles,
)
from app.quant.poisson import match_probabilities as poisson


class TestTau:
    """Only the four low scorelines are corrected."""

    def test_higher_scores_are_untouched(self) -> None:
        for home, away in ((2, 1), (0, 2), (3, 3), (1, 4)):
            assert tau(home, away, 1.5, 1.2, DEFAULT_RHO) == 1.0

    def test_negative_rho_raises_the_drawn_scorelines(self) -> None:
        assert tau(0, 0, 1.5, 1.2, DEFAULT_RHO) > 1.0
        assert tau(1, 1, 1.5, 1.2, DEFAULT_RHO) > 1.0

    def test_negative_rho_lowers_the_one_goal_wins(self) -> None:
        assert tau(1, 0, 1.5, 1.2, DEFAULT_RHO) < 1.0
        assert tau(0, 1, 1.5, 1.2, DEFAULT_RHO) < 1.0

    def test_zero_rho_is_the_identity(self) -> None:
        """With no correlation, the model must reduce to plain Poisson."""
        for home, away in ((0, 0), (1, 0), (0, 1), (1, 1), (2, 2)):
            assert tau(home, away, 1.5, 1.2, 0.0) == 1.0

    def test_factor_never_goes_negative(self) -> None:
        """A negative factor would produce a negative probability."""
        assert tau(0, 0, 4.0, 4.0, -0.25) > 0
        assert tau(1, 1, 1.0, 1.0, 0.99) > 0


class TestDistribution:
    """The corrected grid remains a valid distribution."""

    def test_scoreline_probabilities_sum_to_one(self) -> None:
        grid = score_matrix(1.6, 1.3)
        assert abs(sum(grid.values()) - Decimal(1)) < Decimal("0.0001")

    def test_no_negative_probabilities(self) -> None:
        assert all(p >= 0 for p in score_matrix(2.5, 2.2, rho=-0.25).values())

    def test_outcome_probabilities_sum_to_one(self) -> None:
        result = dixon_coles(1.6, 1.1)
        total = result.home_win + result.draw + result.away_win
        assert abs(total - Decimal(1)) < Decimal("0.0001")

    def test_over_under_stay_complementary(self) -> None:
        result = dixon_coles(1.5, 1.4)
        for line in ("0.5", "1.5", "2.5", "3.5"):
            over = result.over_under[f"over_{line}"]
            under = result.over_under[f"under_{line}"]
            assert abs(over + under - Decimal(1)) < Decimal("0.0001")

    def test_zero_rho_matches_poisson(self) -> None:
        """The correction must be a strict generalisation."""
        corrected = dixon_coles(1.7, 1.2, rho=0.0)
        plain = poisson(1.7, 1.2)
        assert abs(corrected.draw - plain.draw) < Decimal("0.0001")
        assert abs(corrected.home_win - plain.home_win) < Decimal("0.0001")


class TestDrawCorrection:
    """The reason the model exists."""

    @pytest.mark.parametrize(
        ("lambda_home", "lambda_away"),
        [(1.5, 1.2), (2.1, 0.9), (1.1, 1.1), (2.4, 2.0)],
    )
    def test_draws_increase(self, lambda_home: float, lambda_away: float) -> None:
        assert dixon_coles(lambda_home, lambda_away).draw > poisson(lambda_home, lambda_away).draw

    def test_low_scoring_fixtures_move_most(self) -> None:
        """Independence fails hardest where goals are scarce."""
        tight = dixon_coles(1.1, 1.1).draw - poisson(1.1, 1.1).draw
        loose = dixon_coles(2.4, 2.0).draw - poisson(2.4, 2.0).draw
        assert tight > loose

    def test_nil_nil_and_one_one_rise(self) -> None:
        corrected = score_matrix(1.5, 1.2)
        plain = poisson(1.5, 1.2).scoreline
        assert corrected[(0, 0)] > plain[(0, 0)]
        assert corrected[(1, 1)] > plain[(1, 1)]

    def test_single_goal_wins_fall(self) -> None:
        corrected = score_matrix(1.5, 1.2)
        plain = poisson(1.5, 1.2).scoreline
        assert corrected[(1, 0)] < plain[(1, 0)]
        assert corrected[(0, 1)] < plain[(0, 1)]

    def test_two_one_is_unchanged(self) -> None:
        """Nothing above one goal each should move."""
        corrected = score_matrix(1.5, 1.2)
        plain = poisson(1.5, 1.2).scoreline
        assert abs(corrected[(2, 1)] - plain[(2, 1)]) < Decimal("0.002")


class TestRhoFitting:
    """The parameter is measured, not assumed."""

    def test_small_samples_fall_back_to_the_default(self) -> None:
        fit = estimate_rho([(1, 0, 1.4, 1.1)] * 10)
        assert not fit.fitted
        assert fit.rho == DEFAULT_RHO

    def test_fits_when_given_enough_data(self) -> None:
        observations = [(1, 1, 1.4, 1.1)] * (MIN_MATCHES_FOR_RHO + 50)
        fit = estimate_rho(observations)
        assert fit.fitted
        assert fit.matches == len(observations)

    def test_recovers_a_correlation_present_in_the_data(self) -> None:
        """Draw-heavy data should fit a negative rho."""
        observations = (
            [(0, 0, 1.3, 1.2)] * 150 + [(1, 1, 1.3, 1.2)] * 150 + [(2, 1, 1.3, 1.2)] * 100
        )
        assert estimate_rho(observations).rho < 0

    def test_fitted_value_maximises_the_likelihood(self) -> None:
        observations = [(0, 0, 1.3, 1.2)] * 300 + [(2, 1, 1.3, 1.2)] * 200
        fit = estimate_rho(observations)
        for candidate in (-0.2, -0.1, 0.0):
            assert fit.log_likelihood >= log_likelihood(observations, candidate)

    def test_near_zero_fit_is_reported_as_unsupported(self) -> None:
        """Applying a correction the data does not support adds a parameter
        for nothing."""
        from app.quant.dixon_coles import RhoFit

        assert not RhoFit(-0.005, 0.0, 1000, True).improves_on_independence
        assert RhoFit(-0.13, 0.0, 1000, True).improves_on_independence

    def test_likelihood_is_finite_for_impossible_scores(self) -> None:
        """One extreme observation must not produce negative infinity."""
        assert math.isfinite(log_likelihood([(9, 9, 1.2, 1.1)], DEFAULT_RHO))


class TestHarnessIntegration:
    """Selectable in the backtest harness, like-for-like."""

    def test_goal_model_is_selectable(self) -> None:
        import inspect

        from app.quant.backtest_runner import components_and_goals

        parameters = inspect.signature(components_and_goals).parameters
        assert "goal_model" in parameters
        assert "rho" in parameters

    def test_component_name_is_unchanged(self) -> None:
        """Ensemble weights configured for Poisson must apply unchanged, or
        the comparison is not like-for-like."""
        import inspect

        from app.quant import backtest_runner

        source = inspect.getsource(backtest_runner.components_and_goals)
        assert 'ComponentEstimate(\n                    "poisson"' in source
