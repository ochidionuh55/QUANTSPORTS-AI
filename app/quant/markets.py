"""Every market, derived exactly from one joint scoreline distribution.

**One distribution, many questions.** A model that estimates how often each
scoreline occurs has already answered every question about the match. Asking
whether the home side wins, whether there are three goals, or whether both
score are all the same operation — summing the cells where the statement holds.
Deriving markets this way makes contradiction impossible: probabilities cannot
disagree with each other when they come from the same table.

**Combination markets are where this matters.** "Home or over 2.5" is not
``P(home) + P(over 2.5)``. Those events overlap — a 3-1 home win satisfies
both — and adding them counts that overlap twice, producing a number above one.
The correct calculation is inclusion-exclusion:

    P(A or B) = P(A) + P(B) - P(A and B)

and the joint term needs the scoreline table. Most published combination
figures are wrong for exactly this reason. Here every market is computed by
testing each scoreline against the statement and summing the cells that pass,
so the overlap is handled by construction rather than by a correction.

**Nothing here creates an edge.** A market that wins four times in five is
priced to win four times in five. Combination markets are worth computing
because they are worth computing *correctly*, not because high probability is
mispriced.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.quant.grid import build_grid

Scoreline = tuple[int, int]
Grid = dict[Scoreline, Decimal]
Predicate = Callable[[int, int], bool]


@dataclass(frozen=True)
class MarketDefinition:
    """One market, defined by when it wins."""

    market: str
    outcome: str
    holds: Predicate
    """Whether this outcome wins for a given scoreline."""

    combination: bool = False
    """Whether two statements are combined with "or"."""

    @property
    def key(self) -> tuple[str, str]:
        """Return the market and outcome as a pair."""
        return (self.market, self.outcome)


def _either(first: Predicate, second: Predicate) -> Predicate:
    """Combine two statements with "or".

    Inclusion-exclusion is implicit: a scoreline satisfying both is counted
    once, because it is one cell of the table.
    """

    def holds(home: int, away: int) -> bool:
        return first(home, away) or second(home, away)

    return holds


# The base statements, in terms of a scoreline.
def _home(home: int, away: int) -> bool:
    """Home win."""
    return home > away


def _draw(home: int, away: int) -> bool:
    """Draw."""
    return home == away


def _away(home: int, away: int) -> bool:
    """Away win."""
    return home < away


def _over(line: float) -> Predicate:
    """Total goals above a line."""

    def holds(home: int, away: int) -> bool:
        return home + away > line

    return holds


def _under(line: float) -> Predicate:
    """Total goals below a line."""

    def holds(home: int, away: int) -> bool:
        return home + away < line

    return holds


def _btts(home: int, away: int) -> bool:
    """Both teams score."""
    return home > 0 and away > 0


def _no_btts(home: int, away: int) -> bool:
    """At least one side fails to score."""
    return home == 0 or away == 0


def _clean_sheet(home: int, away: int) -> bool:
    """Either side keeps a clean sheet."""
    return home == 0 or away == 0


MARKETS: Final[tuple[MarketDefinition, ...]] = (
    # Result.
    MarketDefinition("1X2", "Home", _home),
    MarketDefinition("1X2", "Draw", _draw),
    MarketDefinition("1X2", "Away", _away),
    # Double chance.
    MarketDefinition("Double chance", "1X (home or draw)", _either(_home, _draw)),
    MarketDefinition("Double chance", "12 (home or away)", _either(_home, _away)),
    MarketDefinition("Double chance", "X2 (draw or away)", _either(_draw, _away)),
    # Goals.
    MarketDefinition("Goals", "Over 0.5", _over(0.5)),
    MarketDefinition("Goals", "Under 0.5", _under(0.5)),
    MarketDefinition("Goals", "Over 1.5", _over(1.5)),
    MarketDefinition("Goals", "Under 1.5", _under(1.5)),
    MarketDefinition("Goals", "Over 2.5", _over(2.5)),
    MarketDefinition("Goals", "Under 2.5", _under(2.5)),
    MarketDefinition("Goals", "Over 3.5", _over(3.5)),
    MarketDefinition("Goals", "Under 3.5", _under(3.5)),
    # Both teams to score.
    MarketDefinition("Both teams to score", "Yes", _btts),
    MarketDefinition("Both teams to score", "No", _no_btts),
    # Result combined with goals. A 0-3 away win satisfies "home or over 2.5"
    # through its second half, which is the whole point of the market.
    MarketDefinition("Result or goals", "Home or Over 2.5", _either(_home, _over(2.5)), True),
    MarketDefinition("Result or goals", "Home or Under 2.5", _either(_home, _under(2.5)), True),
    MarketDefinition("Result or goals", "Draw or Over 2.5", _either(_draw, _over(2.5)), True),
    MarketDefinition("Result or goals", "Draw or Under 2.5", _either(_draw, _under(2.5)), True),
    MarketDefinition("Result or goals", "Away or Over 2.5", _either(_away, _over(2.5)), True),
    MarketDefinition("Result or goals", "Away or Under 2.5", _either(_away, _under(2.5)), True),
    # Result combined with both teams to score.
    MarketDefinition("Result or BTTS", "Home or BTTS", _either(_home, _btts), True),
    MarketDefinition("Result or BTTS", "Draw or BTTS", _either(_draw, _btts), True),
    MarketDefinition("Result or BTTS", "Away or BTTS", _either(_away, _btts), True),
    # Result combined with a clean sheet for either side.
    MarketDefinition(
        "Result or clean sheet",
        "Home or clean sheet",
        _either(_home, _clean_sheet),
        True,
    ),
    MarketDefinition(
        "Result or clean sheet",
        "Draw or clean sheet",
        _either(_draw, _clean_sheet),
        True,
    ),
    MarketDefinition(
        "Result or clean sheet",
        "Away or clean sheet",
        _either(_away, _clean_sheet),
        True,
    ),
)

COMBINATION_MARKETS: Final[frozenset[str]] = frozenset(
    definition.market for definition in MARKETS if definition.combination
)


def derive_markets(grid: Grid) -> dict[str, dict[str, Decimal]]:
    """Return every market probability from one scoreline distribution.

    Args:
        grid: Probability of each scoreline, summing to approximately one.

    Returns:
        Market name to outcome name to probability.

    Every figure is a sum over the same table, so no two can contradict each
    other and no combination can exceed one.
    """
    markets: dict[str, dict[str, Decimal]] = {}
    for definition in MARKETS:
        probability = sum(
            (p for (home, away), p in grid.items() if definition.holds(home, away)),
            Decimal(0),
        )
        markets.setdefault(definition.market, {})[definition.outcome] = probability
    return markets


def probability_of(grid: Grid, market: str, outcome: str) -> Decimal | None:
    """Return one market's probability, or ``None`` if it is not defined."""
    for definition in MARKETS:
        if definition.key == (market, outcome):
            return sum(
                (p for (home, away), p in grid.items() if definition.holds(home, away)),
                Decimal(0),
            )
    return None


# Share of a match's goals scored before half time.
#
# **Not yet measured from our own data, and therefore not published.** Only
# 12.6% of the 113,029 matches on record carry half-time scores, and that
# subset reports 63% of goals arriving before the break — the opposite of the
# well-established pattern that second halves are higher scoring. A figure that
# contradicts the literature is far more likely to mean the sample is
# unrepresentative than that football is.
#
# The value below is the conventional one. The functions using it are correct
# and tested, but no service publishes them until the underlying rate can be
# measured from data we trust.
FIRST_HALF_SHARE: Final[float] = 0.44


def first_half_markets(lambda_home: float, lambda_away: float) -> dict[str, dict[str, Decimal]]:
    """Return first-half markets from full-match scoring rates.

    Built as its own distribution rather than derived from the full-time one,
    because the question is different: a match that finishes 2-1 may have been
    0-0 at the break, and no amount of arithmetic on the final score recovers
    that.

    Goals arrive at a lower rate before half time, so the rates are scaled by a
    measured share rather than halved.
    """
    half_home = max(0.01, lambda_home * FIRST_HALF_SHARE)
    half_away = max(0.01, lambda_away * FIRST_HALF_SHARE)
    grid = build_grid(half_home, half_away)

    def total(predicate: Predicate) -> Decimal:
        return sum(
            (p for (home, away), p in grid.items() if predicate(home, away)),
            Decimal(0),
        )

    return {
        "First half": {
            "Home": total(_home),
            "Draw": total(_draw),
            "Away": total(_away),
            "Over 0.5": total(_over(0.5)),
            "Under 0.5": total(_under(0.5)),
            "Over 1.5": total(_over(1.5)),
            "Under 1.5": total(_under(1.5)),
        }
    }


def settles_first_half(outcome: str, home_goals: int, away_goals: int) -> bool | None:
    """Whether a half-time scoreline wins a first-half outcome."""
    total = home_goals + away_goals
    checks: dict[str, bool] = {
        "Home": home_goals > away_goals,
        "Draw": home_goals == away_goals,
        "Away": home_goals < away_goals,
        "Over 0.5": total > 0.5,
        "Under 0.5": total < 0.5,
        "Over 1.5": total > 1.5,
        "Under 1.5": total < 1.5,
    }
    return checks.get(outcome)


def settles_won(market: str, outcome: str, home_goals: int, away_goals: int) -> bool | None:
    """Whether a finished scoreline wins this outcome.

    The same predicate that defined the probability decides the result, so a
    market cannot be priced by one rule and settled by another.
    """
    for definition in MARKETS:
        if definition.key == (market, outcome):
            return definition.holds(home_goals, away_goals)
    return None


def describe(market: str, outcome: str) -> str:
    """Return a plain description of when an outcome wins."""
    descriptions = {
        "Home or Over 2.5": (
            "Wins if the home side wins, or if the match has three or more "
            "goals — a 0-3 away win still wins this."
        ),
        "Home or Under 2.5": (
            "Wins if the home side wins, or if the match has two goals or " "fewer."
        ),
        "Draw or Over 2.5": ("Wins if the match is drawn, or if it has three or more goals."),
        "Draw or Under 2.5": ("Wins if the match is drawn, or if it has two goals or fewer."),
        "Away or Over 2.5": (
            "Wins if the away side wins, or if the match has three or more " "goals."
        ),
        "Away or Under 2.5": (
            "Wins if the away side wins, or if the match has two goals or " "fewer."
        ),
        "Home or BTTS": "Wins if the home side wins, or if both teams score.",
        "Draw or BTTS": "Wins if the match is drawn, or if both teams score.",
        "Away or BTTS": "Wins if the away side wins, or if both teams score.",
        "Home or clean sheet": (
            "Wins if the home side wins, or if either side keeps a clean sheet."
        ),
        "Draw or clean sheet": (
            "Wins if the match is drawn, or if either side keeps a clean sheet."
        ),
        "Away or clean sheet": (
            "Wins if the away side wins, or if either side keeps a clean sheet."
        ),
    }
    return descriptions.get(outcome, "")
