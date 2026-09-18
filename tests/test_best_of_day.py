"""Best of the Day engine tests.

The ranking decides what a paying user sees first, so it has to be
deterministic, refuse to promote fixtures we know little about, and be willing
to return nothing at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.quant.poisson import score_matrix
from app.services.best_of_day import (
    MIN_SAMPLE,
    SERVICES,
    SERVICES_BY_KEY,
    BestOfDayEngine,
    ModelForecast,
    score_selection,
    settles,
)

NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)


def _forecast(
    fixture_id: str = "1",
    lambda_home: float = 1.9,
    lambda_away: float = 0.8,
    sample: int = 80,
    components: tuple[str, ...] = ("poisson", "elo", "form"),
    views: tuple[dict[str, Decimal], ...] | None = None,
    coverage: str = "fully_modelled",
) -> ModelForecast:
    """Build a forecast for the engine to rank."""
    if views is None:
        views = tuple(
            {"home": Decimal("0.6"), "draw": Decimal("0.25"), "away": Decimal("0.15")}
            for _ in components
        )
    return ModelForecast(
        fixture_id=fixture_id,
        home_name=f"Alpha{fixture_id}",
        away_name=f"Bravo{fixture_id}",
        competition="Championship",
        kickoff=NOW,
        grid=score_matrix(lambda_home, lambda_away),
        components=components,
        component_results=views,
        home_matches=sample,
        away_matches=sample,
        coverage=coverage,
    )


class TestEligibility:
    """What the engine refuses to consider."""

    def test_thin_history_is_excluded(self) -> None:
        """Small samples produce extreme estimates, which a probability
        ranking would then promote."""
        engine = BestOfDayEngine()
        ranked = engine.rank([_forecast(sample=MIN_SAMPLE - 1)])

        assert all(not entries for entries in ranked.values())

    def test_sufficient_history_is_considered(self) -> None:
        engine = BestOfDayEngine()
        ranked = engine.rank([_forecast(sample=MIN_SAMPLE + 50)])

        assert any(entries for entries in ranked.values())

    def test_nothing_qualifies_returns_empty(self) -> None:
        """An even fixture must produce no strong selection anywhere."""
        engine = BestOfDayEngine()
        ranked = engine.rank([_forecast(lambda_home=1.0, lambda_away=1.0)])

        assert ranked["home"] == []
        assert ranked["away"] == []

    def test_engine_never_invents_a_selection(self) -> None:
        """An empty card must produce empty services, not a fallback."""
        engine = BestOfDayEngine()
        best = engine.best([])

        assert set(best) == {service.key for service in SERVICES}
        assert all(selection is None for selection in best.values())


class TestRanking:
    """How fixtures are ordered once they qualify."""

    def test_evidence_outranks_a_thinner_higher_probability(self) -> None:
        """A confident number from little history is weaker evidence, and the
        ranking must say so rather than rewarding the extreme estimate."""
        thin = _forecast("thin", lambda_home=2.6, lambda_away=0.5, sample=MIN_SAMPLE + 2)
        deep = _forecast("deep", lambda_home=2.0, lambda_away=0.7, sample=200)

        ranked = BestOfDayEngine().rank([thin, deep])
        assert ranked["home"][0].forecast.fixture_id == "deep"

    def test_disagreement_is_penalised(self) -> None:
        """Two models reaching a number by different routes is stronger
        evidence than one model contradicting another."""
        agreeing = _forecast(
            "agree",
            views=(
                {"home": Decimal("0.60"), "draw": Decimal("0.25"), "away": Decimal("0.15")},
                {"home": Decimal("0.62"), "draw": Decimal("0.24"), "away": Decimal("0.14")},
            ),
        )
        arguing = _forecast(
            "argue",
            views=(
                {"home": Decimal("0.80"), "draw": Decimal("0.12"), "away": Decimal("0.08")},
                {"home": Decimal("0.35"), "draw": Decimal("0.30"), "away": Decimal("0.35")},
            ),
        )

        ranked = BestOfDayEngine().rank([agreeing, arguing])
        assert ranked["home"][0].forecast.fixture_id == "agree"

    def test_partial_coverage_is_penalised(self) -> None:
        full = _forecast("full", coverage="fully_modelled")
        partial = _forecast("partial", coverage="partially_modelled")

        ranked = BestOfDayEngine().rank([full, partial])
        assert ranked["home"][0].forecast.fixture_id == "full"

    def test_ranking_is_deterministic(self) -> None:
        """The same fixtures must always produce the same order."""
        forecasts = [_forecast(str(i), lambda_home=1.8 + i * 0.1) for i in range(4)]
        engine = BestOfDayEngine()

        first = [s.forecast.fixture_id for s in engine.rank(forecasts)["home"]]
        second = [s.forecast.fixture_id for s in engine.rank(list(reversed(forecasts)))["home"]]
        assert first == second

    def test_limit_is_respected(self) -> None:
        forecasts = [_forecast(str(i)) for i in range(8)]
        ranked = BestOfDayEngine().rank(forecasts, limit=3)

        assert len(ranked["home"]) == 3


class TestScoring:
    """The score must be explainable, not a black box."""

    def test_factors_are_recorded(self) -> None:
        service = SERVICES_BY_KEY["home"]
        _, factors = score_selection(_forecast(), service, 0.7)

        for key in ("probability", "margin", "agreement", "evidence", "coverage"):
            assert key in factors

    def test_score_stays_within_bounds(self) -> None:
        service = SERVICES_BY_KEY["home"]
        score, _ = score_selection(_forecast(), service, 0.95)

        assert 0.0 <= score <= 1.0

    def test_reason_cites_numbers(self) -> None:
        """ "Why this?" must be traceable, not narrative."""
        ranked = BestOfDayEngine().rank([_forecast()])
        selection = ranked["home"][0]

        assert "%" in selection.reason
        assert "matches of history" in selection.reason


class TestSettlement:
    """Services settle by the same rule that priced them."""

    @pytest.mark.parametrize(
        ("key", "score", "expected"),
        [
            ("home", (2, 0), True),
            ("home", (0, 2), False),
            ("away", (0, 2), True),
            ("draw", (1, 1), True),
            ("over_15", (1, 1), True),
            ("over_15", (1, 0), False),
            ("under_25", (1, 1), True),
            ("under_25", (2, 1), False),
            ("btts", (1, 1), True),
            ("no_btts", (2, 0), True),
            # The combination that motivated the whole build.
            ("home_or_over", (0, 3), True),
            ("home_or_over", (2, 0), True),
            ("home_or_over", (1, 1), False),
            ("away_or_btts", (1, 1), True),
            # Corrected. This previously asserted the bug: a 0-2 home defeat
            # was expected to WIN "Best Home or Clean Sheet" because the away
            # side kept a clean sheet. No bookmaker settles it that way.
            ("home_or_cs", (0, 2), True),
            ("home_or_cs", (2, 0), True),
            ("home_or_cs", (0, 0), True),
            ("home_or_cs", (2, 1), True),
            ("home_or_cs", (1, 1), False),
            ("away_or_cs", (0, 2), True),
            ("away_or_cs", (2, 0), True),   # home kept a sheet; ANY settles it
            ("away_or_cs", (0, 0), True),
        ],
    )
    def test_settles_correctly(self, key: str, score: tuple[int, int], expected: bool) -> None:
        assert settles(key, *score) is expected

    def test_unknown_service_is_not_settled(self) -> None:
        assert settles("corners", 2, 1) is None

    def test_every_service_can_settle(self) -> None:
        for service in SERVICES:
            assert settles(service.key, 2, 1) is not None


class TestServiceDefinitions:
    """The catalogue itself."""

    def test_keys_are_unique(self) -> None:
        keys = [service.key for service in SERVICES]
        assert len(keys) == len(set(keys))

    def test_thresholds_are_sane(self) -> None:
        """A bar at zero would qualify everything and mean nothing."""
        for service in SERVICES:
            assert 0.2 < service.min_probability < 0.95

    def test_combination_services_exist(self) -> None:
        assert "home_or_over" in SERVICES_BY_KEY
        assert SERVICES_BY_KEY["home_or_over"].market == "Result or goals"


class TestFixtureIntegrity:
    """One fixture must not appear backing both teams."""

    def test_opposing_legs_conflict(self) -> None:
        from app.services.best_of_day import conflicts

        assert conflicts("home_or_over", "away_or_over")
        assert conflicts("home", "away")
        assert conflicts("home_draw", "away_draw")

    def test_draw_legs_do_not_conflict(self) -> None:
        """ "Draw or over" beside "home or over" does not read as backing both
        sides, so it stays permitted."""
        from app.services.best_of_day import conflicts

        assert not conflicts("home_or_over", "draw_or_over")
        assert not conflicts("draw", "home")

    def test_goal_only_services_never_conflict(self) -> None:
        """Over 1.5 and under 3.5 carry no result leg at all."""
        from app.services.best_of_day import conflicts

        assert not conflicts("over_15", "under_35")
        assert not conflicts("btts", "home_or_over")

    def test_engine_refuses_to_back_both_sides(self) -> None:
        """The complaint this rule exists for: one product appearing to back
        the home team in one service and the away team in another, on the same
        match.

        Draw legs are deliberately still permitted — "draw or over 2.5" beside
        "home or over 2.5" does not read as backing both teams.
        """
        from app.services.best_of_day import RESULT_LEG

        record = _forecast("1", lambda_home=1.9, lambda_away=1.8, sample=200)
        ranked = BestOfDayEngine().rank([record], limit=3)

        legs: set[str] = set()
        for key, selections in ranked.items():
            leg = RESULT_LEG.get(key)
            if leg is not None and selections:
                legs.add(leg)

        assert not {"home", "away"} <= legs

    def test_unrelated_fixtures_are_unaffected(self) -> None:
        """The rule applies per fixture, not across the card."""
        home_side = _forecast("home-strong", lambda_home=2.4, lambda_away=0.7)
        away_side = _forecast("away-strong", lambda_home=0.7, lambda_away=2.4)

        ranked = BestOfDayEngine().rank([home_side, away_side], limit=3)
        assert ranked["home"] or ranked["away"]
