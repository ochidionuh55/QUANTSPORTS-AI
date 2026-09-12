"""Market derivation tests.

Combination markets are where published football probabilities are most often
wrong, because adding two overlapping events double-counts the overlap. These
tests pin the arithmetic to scorelines a reader can check by hand.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.quant.markets import (
    COMBINATION_MARKETS,
    MARKETS,
    derive_markets,
    describe,
    probability_of,
    settles_won,
)
from app.quant.poisson import score_matrix


@pytest.fixture
def grid() -> dict[tuple[int, int], Decimal]:
    """A typical fixture's scoreline distribution."""
    return score_matrix(1.6, 1.1)


class TestSettlement:
    """When each outcome wins, checked against real scorelines."""

    @pytest.mark.parametrize(
        ("outcome", "score", "expected"),
        [
            # The case that motivates the whole market: an away win still wins
            # "home or over 2.5" through the goals half.
            ("Home or Over 2.5", (0, 3), True),
            ("Home or Over 2.5", (1, 1), False),
            ("Home or Over 2.5", (2, 0), True),
            ("Home or Over 2.5", (3, 1), True),
            ("Home or Over 2.5", (0, 1), False),
            ("Home or Under 2.5", (1, 0), True),
            ("Home or Under 2.5", (0, 1), True),
            ("Home or Under 2.5", (1, 3), False),
            ("Draw or Over 2.5", (1, 1), True),
            ("Draw or Over 2.5", (3, 0), True),
            ("Draw or Over 2.5", (1, 0), False),
            ("Away or Over 2.5", (0, 1), True),
            ("Away or Over 2.5", (2, 1), True),
            ("Away or Over 2.5", (1, 0), False),
            ("Home or BTTS", (2, 1), True),
            ("Home or BTTS", (0, 2), False),
            ("Home or BTTS", (1, 1), True),
            ("Away or clean sheet", (1, 0), True),
            ("Away or clean sheet", (0, 2), True),
            ("Away or clean sheet", (2, 1), False),
        ],
    )
    def test_scoreline_settles_correctly(
        self, outcome: str, score: tuple[int, int], expected: bool
    ) -> None:
        market = next(d.market for d in MARKETS if d.outcome == outcome)
        assert settles_won(market, outcome, *score) is expected

    def test_unknown_market_returns_none(self) -> None:
        """An outcome we never priced must not be silently settled."""
        assert settles_won("Corners", "Over 9.5", 2, 1) is None


class TestCombinationArithmetic:
    """The overlap must be removed, not counted twice."""

    def test_combination_is_below_the_naive_sum(self, grid: dict[tuple[int, int], Decimal]) -> None:
        """Adding the parts double-counts every scoreline satisfying both."""
        markets = derive_markets(grid)
        home = markets["1X2"]["Home"]
        over = markets["Goals"]["Over 2.5"]
        combined = markets["Result or goals"]["Home or Over 2.5"]

        assert combined < home + over
        assert combined > max(home, over)

    def test_inclusion_exclusion_holds_exactly(self, grid: dict[tuple[int, int], Decimal]) -> None:
        """P(A or B) must equal P(A) + P(B) - P(A and B) to the last digit."""
        markets = derive_markets(grid)
        home = markets["1X2"]["Home"]
        over = markets["Goals"]["Over 2.5"]
        joint = sum((p for (h, a), p in grid.items() if h > a and h + a > 2.5), Decimal(0))

        assert markets["Result or goals"]["Home or Over 2.5"] == pytest.approx(
            home + over - joint, abs=1e-9
        )

    def test_no_probability_exceeds_one(self, grid: dict[tuple[int, int], Decimal]) -> None:
        markets = derive_markets(grid)
        for outcomes in markets.values():
            for probability in outcomes.values():
                assert Decimal(0) <= probability <= Decimal(1)

    def test_extreme_scoring_rates_stay_valid(self) -> None:
        """A one-sided fixture must not produce a figure above one."""
        markets = derive_markets(score_matrix(4.0, 0.2))
        assert markets["Result or goals"]["Home or Over 2.5"] <= Decimal(1)


class TestConsistency:
    """Markets from one distribution cannot contradict each other."""

    def test_result_sums_to_one(self, grid: dict[tuple[int, int], Decimal]) -> None:
        markets = derive_markets(grid)
        total = sum(markets["1X2"].values())
        assert total == pytest.approx(Decimal(1), abs=1e-9)

    def test_over_and_under_are_complements(self, grid: dict[tuple[int, int], Decimal]) -> None:
        markets = derive_markets(grid)
        for line in ("0.5", "1.5", "2.5", "3.5"):
            total = markets["Goals"][f"Over {line}"] + markets["Goals"][f"Under {line}"]
            assert total == pytest.approx(Decimal(1), abs=1e-9)

    def test_btts_complements(self, grid: dict[tuple[int, int], Decimal]) -> None:
        markets = derive_markets(grid)
        total = markets["Both teams to score"]["Yes"] + markets["Both teams to score"]["No"]
        assert total == pytest.approx(Decimal(1), abs=1e-9)

    def test_double_chance_matches_the_result_market(
        self, grid: dict[tuple[int, int], Decimal]
    ) -> None:
        """Two derivations of the same statement must agree."""
        markets = derive_markets(grid)
        assert markets["Double chance"]["1X (home or draw)"] == pytest.approx(
            markets["1X2"]["Home"] + markets["1X2"]["Draw"], abs=1e-9
        )

    def test_over_lines_decrease(self, grid: dict[tuple[int, int], Decimal]) -> None:
        """More goals cannot be likelier than fewer."""
        goals = derive_markets(grid)["Goals"]
        assert goals["Over 0.5"] > goals["Over 1.5"] > goals["Over 2.5"] > goals["Over 3.5"]


class TestCoverage:
    """Every listed market must be usable end to end."""

    def test_every_market_can_be_derived(self, grid: dict[tuple[int, int], Decimal]) -> None:
        markets = derive_markets(grid)
        for definition in MARKETS:
            assert definition.outcome in markets[definition.market]

    def test_every_market_can_be_settled(self) -> None:
        for definition in MARKETS:
            assert settles_won(definition.market, definition.outcome, 2, 1) is not None

    def test_probability_of_matches_derive(self, grid: dict[tuple[int, int], Decimal]) -> None:
        markets = derive_markets(grid)
        assert probability_of(grid, "1X2", "Home") == markets["1X2"]["Home"]

    def test_probability_of_unknown_is_none(self, grid: dict[tuple[int, int], Decimal]) -> None:
        assert probability_of(grid, "Corners", "Over 9.5") is None

    def test_combination_markets_are_flagged(self) -> None:
        assert "Result or goals" in COMBINATION_MARKETS
        assert "1X2" not in COMBINATION_MARKETS

    def test_every_combination_is_explained(self) -> None:
        """A user must be able to read when a combination wins."""
        for definition in MARKETS:
            if definition.combination:
                assert describe(definition.market, definition.outcome)
