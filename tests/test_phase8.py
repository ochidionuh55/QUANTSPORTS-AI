"""Phase 8 tests: Elo, form, market-prior combination and the ensemble.

The combination tests carry the most weight. If the market prior is not
genuinely dominant when the model has no track record, the entire governance
argument of this project collapses — an unvalidated model would be moving
prices it has not earned the right to move.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.quant.elo import (
    DEFAULT_RATING,
    MIN_MATCHES_FOR_CONFIDENCE,
    EloEngine,
    actual_score,
    expected_score,
    margin_multiplier,
    outcome_probabilities,
)
from app.quant.ensemble import (
    DEFAULT_WEIGHTS,
    ComponentEstimate,
    EnsembleError,
    blend_components,
    build_ensemble,
)
from app.quant.form import MatchOutcome, summarise
from app.quant.form import predict as form_predict
from app.quant.market_prior import (
    CombinationError,
    combine_market,
    combine_one,
    expit,
    logit,
    reliability_from_skill,
)

D = Decimal
START = datetime(2024, 8, 1, tzinfo=UTC)


def _even_prior() -> dict[str, Decimal]:
    """A balanced 1X2 prior."""
    return {"home": D("0.45"), "draw": D("0.27"), "away": D("0.28")}


class TestEloFundamentals:
    """Rating arithmetic."""

    def test_equal_ratings_favour_home(self) -> None:
        assert expected_score(1500, 1500) > 0.5

    def test_stronger_team_expected_higher(self) -> None:
        assert expected_score(1700, 1500) > expected_score(1500, 1700)

    def test_expectations_are_complementary(self) -> None:
        home = expected_score(1600, 1500, home_advantage=0)
        away = expected_score(1500, 1600, home_advantage=0)
        assert abs(home + away - 1.0) < 1e-9

    def test_actual_score(self) -> None:
        assert actual_score(2, 1) == 1.0
        assert actual_score(1, 1) == 0.5
        assert actual_score(0, 3) == 0.0

    def test_margin_multiplier_grows_sublinearly(self) -> None:
        """A 6-0 must not count six times a 1-0."""
        assert margin_multiplier(1) == 1.0
        assert margin_multiplier(3) > margin_multiplier(1)
        assert margin_multiplier(6) < margin_multiplier(3) * 3

    def test_outcome_probabilities_sum_to_one(self) -> None:
        home, draw, away = outcome_probabilities(1600, 1500)
        assert abs(home + draw + away - D(1)) < D("0.0001")

    def test_draw_is_likelier_between_equals(self) -> None:
        _, close_draw, _ = outcome_probabilities(1500, 1500)
        _, distant_draw, _ = outcome_probabilities(1900, 1300)
        assert close_draw > distant_draw

    def test_draw_rate_is_realistic(self) -> None:
        """Observed top-flight draw rates sit near a quarter."""
        _, draw, _ = outcome_probabilities(1500, 1500)
        assert D("0.20") < draw < D("0.32")


class TestEloEngine:
    """Rating updates and history."""

    def test_winning_raises_rating(self) -> None:
        engine = EloEngine()
        engine.record(1, 2, 3, 0, START)
        assert engine.rating(1).rating > DEFAULT_RATING
        assert engine.rating(2).rating < DEFAULT_RATING

    def test_updates_are_zero_sum(self) -> None:
        """Ratings must stay comparable across seasons."""
        engine = EloEngine()
        home_change, away_change = engine.record(1, 2, 2, 1, START)
        assert abs(home_change + away_change) < 1e-9

    def test_bigger_win_moves_rating_further(self) -> None:
        narrow = EloEngine()
        narrow.record(1, 2, 1, 0, START)
        thrashing = EloEngine()
        thrashing.record(1, 2, 5, 0, START)
        assert thrashing.rating(1).rating > narrow.rating(1).rating

    def test_expected_result_moves_rating_less(self) -> None:
        engine = EloEngine()
        engine.rating(1).rating = 1800
        engine.rating(2).rating = 1300
        favourite_wins, _ = engine.record(1, 2, 2, 0, START)

        upset = EloEngine()
        upset.rating(1).rating = 1300
        upset.rating(2).rating = 1800
        underdog_wins, _ = upset.record(1, 2, 2, 0, START)

        assert underdog_wins > favourite_wins

    def test_provisional_ratings_are_flagged(self) -> None:
        """A promoted side must not be modelled as average."""
        engine = EloEngine()
        engine.record(1, 2, 1, 0, START)
        assert not engine.rating(1).is_established
        assert not engine.is_reliable(1, 2)

    def test_established_after_enough_matches(self) -> None:
        engine = EloEngine()
        for index in range(MIN_MATCHES_FOR_CONFIDENCE):
            engine.record(1, 2, 1, 0, START + timedelta(days=index))
        assert engine.rating(1).is_established

    def test_replay_orders_results_by_date(self) -> None:
        """Out-of-order replay produces ratings that never existed."""
        forward = EloEngine()
        results = [
            (1, 2, 3, 0, START),
            (2, 1, 1, 0, START + timedelta(days=7)),
            (1, 2, 2, 2, START + timedelta(days=14)),
        ]
        forward.replay(results)

        shuffled = EloEngine()
        shuffled.replay(list(reversed(results)))

        assert abs(forward.rating(1).rating - shuffled.rating(1).rating) < 1e-9

    def test_rating_at_excludes_the_future(self) -> None:
        """The mechanism that keeps a backtest honest."""
        engine = EloEngine()
        engine.record(1, 2, 5, 0, START)
        engine.record(1, 2, 5, 0, START + timedelta(days=30))

        early = engine.rating(1).rating_at(START + timedelta(days=1))
        late = engine.rating(1).rating_at(START + timedelta(days=60))

        assert early < late
        assert early != engine.rating(1).rating

    def test_prediction_at_a_past_moment(self) -> None:
        engine = EloEngine()
        for index in range(10):
            engine.record(1, 2, 3, 0, START + timedelta(days=index * 7))

        early = engine.predict(1, 2, moment=START + timedelta(days=1))
        current = engine.predict(1, 2)
        assert current[0] > early[0]

    def test_reliability_respects_the_cutoff(self) -> None:
        engine = EloEngine()
        for index in range(MIN_MATCHES_FOR_CONFIDENCE + 2):
            engine.record(1, 2, 1, 0, START + timedelta(days=index))
        assert engine.is_reliable(1, 2)
        assert not engine.is_reliable(1, 2, moment=START + timedelta(days=2))


class TestForm:
    """Recency weighting and its limits."""

    def _run(self, results: list[tuple[int, int]], home: bool = True) -> list[MatchOutcome]:
        """Build a sequence of match outcomes."""
        return [
            MatchOutcome(
                played_at=START + timedelta(days=index * 7),
                scored=scored,
                conceded=conceded,
                at_home=home,
            )
            for index, (scored, conceded) in enumerate(results)
        ]

    def test_recent_matches_weigh_more(self) -> None:
        """A side that has turned a corner must not look average."""
        improving = summarise(1, self._run([(0, 3)] * 5 + [(3, 0)] * 5))
        declining = summarise(2, self._run([(3, 0)] * 5 + [(0, 3)] * 5))
        assert improving.points_per_match > declining.points_per_match

    def test_empty_history_is_unreliable(self) -> None:
        summary = summarise(1, [])
        assert summary.matches == 0
        assert not summary.is_reliable

    def test_short_history_is_unreliable(self) -> None:
        assert not summarise(1, self._run([(1, 0), (2, 1)])).is_reliable

    def test_home_and_away_are_separable(self) -> None:
        matches = self._run([(3, 0)] * 5, home=True) + self._run([(0, 3)] * 5, home=False)
        at_home = summarise(1, matches, at_home=True)
        away = summarise(1, matches, at_home=False)
        assert at_home.points_per_match > away.points_per_match

    def test_before_cutoff_excludes_later_matches(self) -> None:
        matches = self._run([(1, 0)] * 10)
        limited = summarise(1, matches, before=START + timedelta(days=15))
        assert limited.matches == 3

    def test_derived_rates(self) -> None:
        summary = summarise(1, self._run([(2, 0), (1, 1), (0, 0), (3, 2)]))
        assert 0 <= summary.clean_sheet_rate <= 1
        assert 0 <= summary.both_scored_rate <= 1
        assert 0 <= summary.over_2_5_rate <= 1

    def test_prediction_sums_to_one(self) -> None:
        home = summarise(1, self._run([(2, 0)] * 6))
        away = summarise(2, self._run([(0, 2)] * 6, home=False))
        probabilities = form_predict(home, away)
        assert abs(sum(probabilities) - D(1)) < D("0.0001")

    def test_better_form_predicts_better(self) -> None:
        strong = summarise(1, self._run([(3, 0)] * 8))
        weak = summarise(2, self._run([(0, 3)] * 8, home=False))
        home_win, _, away_win = form_predict(strong, weak)
        assert home_win > away_win


class TestLogOddsCombination:
    """The market-as-prior mechanism."""

    def test_logit_round_trip(self) -> None:
        for probability in (0.01, 0.25, 0.5, 0.75, 0.99):
            assert abs(expit(logit(probability)) - probability) < 1e-9

    def test_logit_handles_extremes(self) -> None:
        assert logit(0.0) < -10
        assert logit(1.0) > 10

    def test_zero_reliability_returns_the_prior(self) -> None:
        """The single most important property in this module.

        A model with no track record must not move the market at all.
        """
        posterior, shift, _ = combine_one(0.45, 0.90, reliability=0.0)
        assert abs(posterior - 0.45) < 1e-9
        assert shift == 0.0

    def test_full_reliability_returns_the_model(self) -> None:
        posterior, _, _ = combine_one(0.45, 0.55, reliability=1.0)
        assert abs(posterior - 0.55) < 1e-6

    def test_partial_reliability_lands_between(self) -> None:
        posterior, _, _ = combine_one(0.40, 0.60, reliability=0.5)
        assert 0.40 < posterior < 0.60

    def test_extreme_disagreement_is_capped(self) -> None:
        """A huge disagreement is usually a data fault, not an insight."""
        posterior, shift, capped = combine_one(0.02, 0.95, reliability=1.0)
        assert capped
        assert abs(shift) <= 1.5 + 1e-9
        assert posterior < 0.95

    def test_reliability_is_validated(self) -> None:
        for bad in (-0.1, 1.5):
            with pytest.raises(CombinationError, match="measured skill"):
                combine_one(0.5, 0.6, reliability=bad)

    def test_log_odds_respects_relative_disagreement(self) -> None:
        """Averaging probabilities understates disagreement at the extremes.

        1% against 5% is a claim of five times the risk; a linear midpoint of
        3% would hide that.
        """
        posterior, _, _ = combine_one(0.01, 0.05, reliability=0.5)
        assert posterior < 0.03


class TestMarketCombination:
    """Whole-market blending."""

    def test_posterior_sums_to_one(self) -> None:
        model = {"home": D("0.55"), "draw": D("0.22"), "away": D("0.23")}
        result = combine_market(_even_prior(), model, reliability=0.4)
        assert abs(sum(c.posterior for c in result.values()) - D(1)) < D("0.0001")

    def test_zero_reliability_preserves_the_prior(self) -> None:
        model = {"home": D("0.80"), "draw": D("0.10"), "away": D("0.10")}
        result = combine_market(_even_prior(), model, reliability=0.0)
        for label, combination in result.items():
            assert abs(combination.posterior - _even_prior()[label]) < D("0.001")

    def test_posterior_moves_toward_the_model(self) -> None:
        model = {"home": D("0.60"), "draw": D("0.22"), "away": D("0.18")}
        result = combine_market(_even_prior(), model, reliability=0.5)
        assert result["home"].posterior > _even_prior()["home"]
        assert result["away"].posterior < _even_prior()["away"]

    def test_mismatched_outcomes_rejected(self) -> None:
        with pytest.raises(CombinationError, match="same outcomes"):
            combine_market(
                _even_prior(),
                {"home": D("0.5"), "draw": D("0.5")},
                reliability=0.5,
            )

    def test_unnormalised_prior_rejected(self) -> None:
        """Raw odds still carry margin and are not a prior."""
        with pytest.raises(CombinationError, match="must sum to 1.0"):
            combine_market(
                {"home": D("0.50"), "draw": D("0.30"), "away": D("0.30")},
                {"home": D("0.45"), "draw": D("0.27"), "away": D("0.28")},
                reliability=0.5,
            )

    def test_single_outcome_rejected(self) -> None:
        with pytest.raises(CombinationError, match="at least two outcomes"):
            combine_market({"home": D("1.0")}, {"home": D("1.0")}, reliability=0.5)


class TestReliabilityFromSkill:
    """Influence must be earned."""

    def test_no_skill_earns_no_influence(self) -> None:
        assert reliability_from_skill(0.0) == 0.0
        assert reliability_from_skill(-0.05) == 0.0

    def test_influence_rises_with_skill(self) -> None:
        assert reliability_from_skill(0.02) > reliability_from_skill(0.005)

    def test_influence_is_capped(self) -> None:
        """Even a strong model should not fully override the market."""
        assert reliability_from_skill(10.0) == 0.6


class TestEnsemble:
    """Component blending and unavailable-data handling."""

    def _component(self, name: str, home: str, draw: str, away: str, reliable: bool = True):
        """Build a component estimate."""
        return ComponentEstimate(
            name=name,
            probabilities={"home": D(home), "draw": D(draw), "away": D(away)},
            reliable=reliable,
        )

    def test_blends_all_components(self) -> None:
        components = [
            self._component("poisson", "0.50", "0.25", "0.25"),
            self._component("elo", "0.40", "0.30", "0.30"),
            self._component("form", "0.60", "0.20", "0.20"),
        ]
        probabilities, used, dropped, _ = blend_components(components)
        assert abs(sum(probabilities.values()) - D(1)) < D("0.0001")
        assert set(used) == {"poisson", "elo", "form"}
        assert dropped == ()

    def test_unreliable_components_are_dropped_not_defaulted(self) -> None:
        """A missing component must not contribute a neutral guess."""
        components = [
            self._component("poisson", "0.50", "0.25", "0.25"),
            self._component("elo", "0.40", "0.30", "0.30", reliable=False),
        ]
        _, used, dropped, effective = blend_components(components)
        assert used == ("poisson",)
        assert dropped == ("elo",)
        assert effective["poisson"] == 1.0

    def test_no_reliable_component_refuses_to_predict(self) -> None:
        """The honest answer to a fixture with no data is silence."""
        components = [
            self._component("poisson", "0.5", "0.25", "0.25", reliable=False),
            self._component("elo", "0.4", "0.3", "0.3", reliable=False),
        ]
        with pytest.raises(EnsembleError, match="unmodellable"):
            blend_components(components)

    def test_malformed_component_rejected(self) -> None:
        bad = ComponentEstimate(name="poisson", probabilities={"home": D("0.9"), "draw": D("0.5")})
        with pytest.raises(EnsembleError, match="missing outcomes"):
            blend_components([bad])

    def test_non_distribution_rejected(self) -> None:
        bad = self._component("poisson", "0.9", "0.9", "0.9")
        with pytest.raises(EnsembleError, match="sum to"):
            blend_components([bad])

    def test_weights_are_renormalised_when_dropping(self) -> None:
        components = [
            self._component("poisson", "0.50", "0.25", "0.25"),
            self._component("elo", "0.40", "0.30", "0.30"),
            self._component("form", "0.60", "0.20", "0.20", reliable=False),
        ]
        _, _, _, effective = blend_components(components)
        assert abs(sum(effective.values()) - 1.0) < 1e-9

    def test_full_ensemble_with_no_track_record_returns_the_prior(self) -> None:
        """The governance property, end to end."""
        components = [self._component("poisson", "0.70", "0.15", "0.15")]
        result = build_ensemble(components, _even_prior(), reliability=0.0)

        assert result.is_usable
        for label, prior in _even_prior().items():
            assert abs(result.posterior[label] - prior) < D("0.001")

    def test_full_ensemble_with_skill_moves_the_prior(self) -> None:
        components = [self._component("poisson", "0.60", "0.22", "0.18")]
        result = build_ensemble(components, _even_prior(), reliability=0.5)

        assert result.posterior["home"] > _even_prior()["home"]
        assert abs(sum(result.posterior.values()) - D(1)) < D("0.0001")

    def test_capping_is_reported(self) -> None:
        """A capped shift signals a probable data fault."""
        components = [self._component("poisson", "0.97", "0.02", "0.01")]
        result = build_ensemble(
            components,
            {"home": D("0.10"), "draw": D("0.30"), "away": D("0.60")},
            reliability=1.0,
        )
        assert result.any_capped

    def test_summary_is_readable(self) -> None:
        components = [self._component("poisson", "0.50", "0.25", "0.25")]
        summary = build_ensemble(components, _even_prior(), reliability=0.3).summary()
        assert "poisson" in summary
        assert "reliability" in summary

    def test_default_weights_sum_to_one(self) -> None:
        assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9
