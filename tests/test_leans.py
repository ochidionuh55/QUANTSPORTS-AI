"""Statistical lean tests.

The ranking tests matter most. A lean list that always shows "Over 0.5 at 94%"
is worse than no list: it looks like insight, informs nobody, and teaches users
to ignore the feature.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.leans import (
    EXCLUDED_OUTCOMES,
    MIN_PROBABILITY,
    LeanBuilder,
    _confidence,
    _market_agreement,
)

NOW = datetime(2026, 3, 1, 15, tzinfo=UTC)


def _record(
    fixture_id: str = "1",
    markets: dict | None = None,
    coverage: str = "fully_modelled",
    components: int = 3,
    matches: int = 40,
    market: dict | None = None,
) -> SimpleNamespace:
    """Build a stored-analysis stand-in."""
    return SimpleNamespace(
        provider_event_id=fixture_id,
        home_name="Alpha",
        away_name="Bravo",
        competition="Championship",
        kickoff=NOW,
        coverage=coverage,
        markets=markets
        if markets is not None
        else {
            "1X2": {"Home": "0.52", "Draw": "0.26", "Away": "0.22"},
            "Goals": {
                "Over 0.5": "0.94",
                "Under 0.5": "0.06",
                "Over 2.5": "0.55",
                "Under 2.5": "0.45",
            },
            "Both teams to score": {"Yes": "0.54", "No": "0.46"},
        },
        components_used=["poisson", "elo", "form"][:components],
        home_stats={"matches": matches},
        away_stats={"matches": matches},
        market_probabilities=market
        if market is not None
        else {"home": "0.50", "draw": "0.27", "away": "0.23"},
    )


class TestRankingIsNotJustProbability:
    """The defect that would make the feature useless."""

    def test_near_certain_outcomes_are_excluded(self) -> None:
        """Over 0.5 is above 90% in almost every fixture."""
        lean = LeanBuilder().for_record(_record())
        assert lean is not None
        assert lean.outcome not in EXCLUDED_OUTCOMES

    def test_highest_probability_does_not_automatically_win(self) -> None:
        """A 94% near-certainty must not outrank a meaningful 62%."""
        lean = LeanBuilder().for_record(_record())
        assert lean is not None
        assert lean.probability < 0.94

    def test_balanced_fixture_produces_no_lean(self) -> None:
        """An even match has no conclusion worth stating."""
        record = _record(markets={"1X2": {"Home": "0.35", "Draw": "0.30", "Away": "0.35"}})
        assert LeanBuilder().for_record(record) is None

    def test_threshold_is_respected(self) -> None:
        record = _record(
            markets={
                "1X2": {
                    "Home": str(MIN_PROBABILITY - 0.01),
                    "Draw": "0.30",
                    "Away": "0.26",
                }
            }
        )
        assert LeanBuilder().for_record(record) is None


class TestScoreComponents:
    """Each component must behave sensibly on its own."""

    def test_confidence_rises_with_departure_from_baseline(self) -> None:
        assert _confidence(0.60, 1 / 3) > _confidence(0.40, 1 / 3)

    def test_high_baseline_markets_do_not_dominate(self) -> None:
        """Double chance starts at 2/3 before anyone looks at the fixture.

        Without normalising by headroom it would win every ranking simply
        because two outcomes are likelier than one.
        """
        double_chance = _confidence(0.75, 2 / 3)
        home_win = _confidence(0.60, 1 / 3)
        assert home_win > double_chance

    def test_baseline_probability_scores_zero(self) -> None:
        """An outcome at its base rate says nothing about the fixture."""
        assert _confidence(1 / 3, 1 / 3) == pytest.approx(0.0, abs=1e-6)

    def test_a_forecast_near_the_base_rate_scores_near_zero(self) -> None:
        """Information is measured in both directions, but a small departure
        carries little either way."""
        assert _confidence(0.30, 1 / 3) < 0.02

    def test_market_agreement_rewards_agreement(self) -> None:
        """Deliberately the opposite of value betting."""
        assert _market_agreement(0.50, 0.50) > _market_agreement(0.50, 0.30)

    def test_missing_market_is_neutral(self) -> None:
        assert _market_agreement(0.6, None) == 0.5

    def test_full_coverage_outscores_partial(self) -> None:
        builder = LeanBuilder()
        full = builder.for_record(_record(coverage="fully_modelled"))
        partial = builder.for_record(_record(coverage="partially_modelled", components=1))
        assert full is not None and partial is not None
        assert full.score > partial.score

    def test_deeper_history_outscores_thin(self) -> None:
        builder = LeanBuilder()
        deep = builder.for_record(_record(matches=60))
        thin = builder.for_record(_record(matches=8))
        assert deep is not None and thin is not None
        assert deep.score > thin.score

    def test_model_agreement_outscores_a_lone_model(self) -> None:
        builder = LeanBuilder()
        agreed = builder.for_record(_record(components=3))
        alone = builder.for_record(_record(components=1))
        assert agreed is not None and alone is not None
        assert agreed.stability > alone.stability


class TestRanking:
    """Ordering across a card."""

    def test_returns_strongest_first(self) -> None:
        weak = _record("weak", coverage="partially_modelled", components=1, matches=9)
        strong = _record("strong")
        ranked = LeanBuilder().rank([weak, strong])
        assert ranked[0].fixture_id == "strong"

    def test_limit_is_respected(self) -> None:
        records = [_record(str(i)) for i in range(10)]
        assert len(LeanBuilder().rank(records, limit=3)) == 3

    def test_fixtures_without_analysis_are_skipped(self) -> None:
        assert LeanBuilder().rank([_record("x", markets={})]) == []

    def test_empty_card_returns_nothing(self) -> None:
        assert LeanBuilder().rank([]) == []


class TestExplanation:
    """A lean must justify itself in plain language."""

    def test_reason_states_probability_and_basis(self) -> None:
        lean = LeanBuilder().for_record(_record())
        assert lean is not None
        assert "%" in lean.reason
        assert "history" in lean.reason

    def test_reason_mentions_the_market_when_known(self) -> None:
        lean = LeanBuilder().for_record(_record())
        assert lean is not None
        assert "market" in lean.reason.lower()

    def test_headline_is_short(self) -> None:
        lean = LeanBuilder().for_record(_record())
        assert lean is not None
        assert len(lean.headline) < 40


class TestHonesty:
    """The feature must not become a betting tip in disguise."""

    def test_no_value_language_in_output(self) -> None:
        lean = LeanBuilder().for_record(_record())
        assert lean is not None
        forbidden = ("value", "edge", "bet", "tip", "guarantee", "sure")
        assert not any(word in lean.reason.lower() for word in forbidden)

    def test_notice_disclaims_betting_advice(self) -> None:
        from app.bot.formatting import LEAN_NOTICE

        lowered = LEAN_NOTICE.lower()
        assert "not betting advice" in lowered
        assert "have not demonstrated an edge" in lowered

    @pytest.mark.parametrize("outcome", sorted(EXCLUDED_OUTCOMES))
    def test_trivial_outcomes_never_appear(self, outcome: str) -> None:
        record = _record(markets={"Goals": {outcome: "0.96", "Other": "0.04"}})
        assert LeanBuilder().for_record(record) is None


class TestBaseRates:
    """Leans are scored against how often an outcome actually happens.

    Regression: a single baseline per market meant "Goals" was scored against
    0.5, but Over 1.5 lands 74% of the time. An 89% Over 1.5 outranked a 62%
    draw, and every lean in the first day's top five was a goals line.
    """

    def test_measured_rates_cover_every_market_outcome(self) -> None:
        from app.services.leans import BASE_RATES

        for definition in ("Home", "Draw", "Away", "Over 1.5", "Under 2.5", "Yes"):
            assert definition in BASE_RATES

    def test_over_one_five_is_not_a_coin_flip(self) -> None:
        from app.services.leans import BASE_RATES

        assert BASE_RATES["Over 1.5"] > 0.70

    def test_complementary_outcomes_sum_to_one(self) -> None:
        from app.services.leans import BASE_RATES

        for line in ("0.5", "1.5", "2.5", "3.5"):
            total = BASE_RATES[f"Over {line}"] + BASE_RATES[f"Under {line}"]
            assert abs(total - 1.0) < 0.001
        assert abs(BASE_RATES["Yes"] + BASE_RATES["No"] - 1.0) < 0.001

    def test_a_strong_draw_outranks_a_routine_goals_line(self) -> None:
        """The defect that made the feature useless, stated as a test."""
        from app.services.leans import _confidence, base_rate_for

        draw = _confidence(0.62, base_rate_for("1X2", "Draw"))
        goals = _confidence(0.89, base_rate_for("Goals", "Over 1.5"))
        assert draw > goals

    def test_a_forecast_at_the_base_rate_carries_no_information(self) -> None:
        from app.services.leans import _confidence, base_rate_for

        rate = base_rate_for("Goals", "Over 1.5")
        assert _confidence(rate, rate) == pytest.approx(0.0, abs=1e-6)

    def test_unknown_outcome_falls_back_to_its_market(self) -> None:
        """A new market must still rank rather than being dropped."""
        from app.services.leans import base_rate_for

        assert base_rate_for("Goals", "Over 4.5") == 0.5


class TestRankingDiversity:
    """One market must not fill the whole list."""

    def test_at_most_two_leans_per_market(self) -> None:
        from app.services.leans import MAX_PER_MARKET, LeanBuilder

        # Each fixture yields one lean, so variety has to come from the card.
        # Three markets at a cap of two covers a list of five.
        markets = [
            {"Goals": {"Under 2.5": "0.88"}},
            {"1X2": {"Home": "0.66", "Draw": "0.20", "Away": "0.14"}},
            {"Both teams to score": {"Yes": "0.80", "No": "0.20"}},
        ]
        records = [_record(str(index), markets=markets[index % len(markets)]) for index in range(9)]
        ranked = LeanBuilder().rank(records, limit=5)

        from collections import Counter

        counts = Counter(lean.market for lean in ranked)
        assert len(ranked) == 5
        assert all(count <= MAX_PER_MARKET for count in counts.values()), counts

    def test_a_homogeneous_card_is_still_filled(self) -> None:
        """When every fixture supports the same market, an empty screen is
        worse than a repetitive one."""
        from app.services.leans import LeanBuilder

        records = [
            _record(str(index), markets={"Goals": {"Under 2.5": "0.88"}}) for index in range(6)
        ]
        assert len(LeanBuilder().rank(records, limit=4)) == 4

    def test_a_thin_card_is_still_filled(self) -> None:
        """Diversity must not leave the screen short when options are few."""
        from app.services.leans import LeanBuilder

        records = [_record(str(i)) for i in range(4)]
        assert len(LeanBuilder().rank(records, limit=3)) == 3


class TestDirectionality:
    """A lean must mean the outcome is *more* likely than usual.

    Regression: divergence is symmetric, so a 58% double chance scored well
    against a 70% base rate and was published as a strong conclusion — when it
    is a statement that the outcome is *less* likely than usual. The list
    inverted its own meaning.
    """

    def test_below_base_rate_scores_zero(self) -> None:
        from app.services.leans import _confidence, base_rate_for

        rate = base_rate_for("Double chance", "1X (home or draw)")
        assert _confidence(0.58, rate) == 0.0

    def test_above_base_rate_still_scores(self) -> None:
        from app.services.leans import _confidence, base_rate_for

        rate = base_rate_for("Double chance", "1X (home or draw)")
        assert _confidence(0.85, rate) > 0.0

    def test_a_below_average_outcome_is_not_offered_as_a_lean(self) -> None:
        record = _record(markets={"Double chance": {"1X (home or draw)": "0.58"}})
        assert LeanBuilder().for_record(record) is None


class TestAttribution:
    """Credit must go where the number came from.

    With no promoted model the published probability *is* the market prior; the
    models computed a view and were given no weight to move it. Claiming "our
    models put this at 77%" would take credit for a bookmaker's number.
    """

    def test_agreement_with_the_market_is_stated_plainly(self) -> None:
        lean = LeanBuilder().for_record(
            _record(
                markets={"1X2": {"Home": "0.52", "Draw": "0.26", "Away": "0.22"}},
                market={"home": "0.52", "draw": "0.26", "away": "0.22"},
            )
        )
        assert lean is not None
        assert "market prices" in lean.reason
        assert "our models put" not in lean.reason.lower()

    def test_the_base_rate_is_shown_for_context(self) -> None:
        """A percentage alone means nothing without knowing what is normal."""
        lean = LeanBuilder().for_record(_record())
        assert lean is not None
        assert "in a typical match" in lean.reason
        assert "x)" in lean.reason
