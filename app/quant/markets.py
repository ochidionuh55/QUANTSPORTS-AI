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
from enum import Enum
from typing import Final

from app.quant.grid import build_grid

Scoreline = tuple[int, int]
Grid = dict[Scoreline, Decimal]
Predicate = Callable[[int, int], bool]


class SettlementScope(Enum):
    """The period a market settles over."""

    FULL_MATCH = "full_match"
    FIRST_HALF = "first_half"


class MarketError(KeyError):
    """Raised when a market is unknown, unsupported, or incompletely defined.

    Loud by design. A silent ``None`` from a market lookup is how a service
    ends up publishing a selection nothing can settle.
    """


@dataclass(frozen=True)
class MarketDefinition:
    """One market: what it means, how it prices, how it settles.

    **One predicate, two uses.** ``predicate`` is the single statement about a
    scoreline that defines this market. The probability is the sum of grid
    cells where it holds; the settlement is whether it holds for the final
    score. :meth:`probability_holds` and :meth:`settles` both delegate to it,
    so the priced event and the settled event cannot drift apart — not by
    convention, but because there is only one rule to drift from.

    **The stored keys never change.** ``market`` and ``outcome`` are persisted
    on every published selection. Renaming one would orphan the record it was
    published under, so an ambiguous name is corrected in ``display_name``
    instead, which nothing stores.

    **Unsupported markets are declared, not omitted.** A market we cannot map
    to a real bet stays in the registry marked unsupported, carrying the reason.
    Deleting it would lose the fact that we once published it.
    """

    market: str
    """Stored market key. Persisted; never rename."""

    outcome: str
    """Stored outcome key. Persisted; never rename."""

    predicate: Predicate
    """The single statement defining this market, for pricing and settling."""

    display_name: str = ""
    """Unambiguous label shown to people. Defaults to ``outcome``."""

    provider_market: str | None = None
    """The bookmaker market this corresponds to, or ``None`` if unmapped."""

    provider_outcome: str | None = None
    """The bookmaker's name for this outcome."""

    scope: SettlementScope = SettlementScope.FULL_MATCH
    """The period settled over."""

    supported: bool = True
    """Whether this may be published."""

    unsupported_reason: str | None = None
    """Why not, when unsupported. Required whenever ``supported`` is false."""

    combination: bool = False
    """Whether two statements are combined with "or"."""

    def __post_init__(self) -> None:
        """Reject a definition that could not be published or settled safely."""
        if not self.supported and not self.unsupported_reason:
            raise MarketError(
                f"{self.market}/{self.outcome} is unsupported without a stated reason."
            )
        if self.supported and self.provider_market is None:
            raise MarketError(
                f"{self.market}/{self.outcome} is supported but maps to no provider market. "
                "A market nobody can bet must not be published as a selection."
            )

    @property
    def key(self) -> tuple[str, str]:
        """Return the market and outcome as a pair."""
        return (self.market, self.outcome)

    @property
    def label(self) -> str:
        """Return the unambiguous display name."""
        return self.display_name or self.outcome

    def holds(self, home: int, away: int) -> bool:
        """Whether this outcome wins for a scoreline."""
        return self.predicate(home, away)

    def probability_holds(self, home: int, away: int) -> bool:
        """Whether this scoreline contributes to the market's probability."""
        return self.predicate(home, away)

    def settles(self, home: int, away: int) -> bool:
        """Whether this final score wins the market."""
        return self.predicate(home, away)


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


def _home_clean_sheet(home: int, away: int) -> bool:
    """The home side keeps a clean sheet: the away side did not score."""
    return away == 0


def _away_clean_sheet(home: int, away: int) -> bool:
    """The away side keeps a clean sheet: the home side did not score."""
    return home == 0


MARKETS: Final[tuple[MarketDefinition, ...]] = (
    # Result.
    MarketDefinition("1X2", "Home", _home, "Home win", "1X2", "1"),
    MarketDefinition("1X2", "Draw", _draw, "Draw", "1X2", "X"),
    MarketDefinition("1X2", "Away", _away, "Away win", "1X2", "2"),
    # Double chance.
    MarketDefinition(
        "Double chance", "1X (home or draw)", _either(_home, _draw),
        "Home win or draw", "Double chance", "1X",
    ),
    MarketDefinition(
        "Double chance", "12 (home or away)", _either(_home, _away),
        "Home win or away win", "Double chance", "12",
    ),
    MarketDefinition(
        "Double chance", "X2 (draw or away)", _either(_draw, _away),
        "Draw or away win", "Double chance", "X2",
    ),
    # Goals. Half lines throughout, so no scoreline can push.
    MarketDefinition("Goals", "Over 0.5", _over(0.5), "Over 0.5 goals", "Over/Under", "Over 0.5"),
    MarketDefinition(
        "Goals", "Under 0.5", _under(0.5), "Under 0.5 goals", "Over/Under", "Under 0.5",
    ),
    MarketDefinition("Goals", "Over 1.5", _over(1.5), "Over 1.5 goals", "Over/Under", "Over 1.5"),
    MarketDefinition(
        "Goals", "Under 1.5", _under(1.5), "Under 1.5 goals", "Over/Under", "Under 1.5",
    ),
    MarketDefinition("Goals", "Over 2.5", _over(2.5), "Over 2.5 goals", "Over/Under", "Over 2.5"),
    MarketDefinition(
        "Goals", "Under 2.5", _under(2.5), "Under 2.5 goals", "Over/Under", "Under 2.5",
    ),
    MarketDefinition("Goals", "Over 3.5", _over(3.5), "Over 3.5 goals", "Over/Under", "Over 3.5"),
    MarketDefinition(
        "Goals", "Under 3.5", _under(3.5), "Under 3.5 goals", "Over/Under", "Under 3.5",
    ),
    # Both teams to score.
    MarketDefinition(
        "Both teams to score", "Yes", _btts, "Both teams to score", "Both teams to score", "Yes",
    ),
    MarketDefinition(
        "Both teams to score", "No", _no_btts,
        "Not both teams to score", "Both teams to score", "No",
    ),
    # Result combined with goals. A 0-3 away win satisfies "home or over 2.5"
    # through its second half, which is the whole point of the market.
    MarketDefinition(
        "Result or goals", "Home or Over 2.5", _either(_home, _over(2.5)),
        "Home win or over 2.5 goals", "Result/Total Goals", "Home or Over 2.5", combination=True,
    ),
    MarketDefinition(
        "Result or goals", "Home or Under 2.5", _either(_home, _under(2.5)),
        "Home win or under 2.5 goals", "Result/Total Goals", "Home or Under 2.5", combination=True,
    ),
    MarketDefinition(
        "Result or goals", "Draw or Over 2.5", _either(_draw, _over(2.5)),
        "Draw or over 2.5 goals", "Result/Total Goals", "Draw or Over 2.5", combination=True,
    ),
    MarketDefinition(
        "Result or goals", "Draw or Under 2.5", _either(_draw, _under(2.5)),
        "Draw or under 2.5 goals", "Result/Total Goals", "Draw or Under 2.5", combination=True,
    ),
    MarketDefinition(
        "Result or goals", "Away or Over 2.5", _either(_away, _over(2.5)),
        "Away win or over 2.5 goals", "Result/Total Goals", "Away or Over 2.5", combination=True,
    ),
    MarketDefinition(
        "Result or goals", "Away or Under 2.5", _either(_away, _under(2.5)),
        "Away win or under 2.5 goals", "Result/Total Goals", "Away or Under 2.5", combination=True,
    ),
    # Result combined with both teams to score.
    MarketDefinition(
        "Result or BTTS", "Home or BTTS", _either(_home, _btts),
        "Home win or both teams to score", "Result/Both Teams Score", "Home or BTTS",
        combination=True,
    ),
    MarketDefinition(
        "Result or BTTS", "Draw or BTTS", _either(_draw, _btts),
        "Draw or both teams to score", "Result/Both Teams Score", "Draw or BTTS",
        combination=True,
    ),
    MarketDefinition(
        "Result or BTTS", "Away or BTTS", _either(_away, _btts),
        "Away win or both teams to score", "Result/Both Teams Score", "Away or BTTS",
        combination=True,
    ),
    # Result combined with that side's own clean sheet.
    #
    # **Corrected.** These previously used a predicate meaning "either side
    # keeps a clean sheet", which made a 0-2 home defeat win "Home or clean
    # sheet" — because the *away* team kept the sheet. No bookmaker settles it
    # that way, so a selection could show WON here while the same bet lost
    # everywhere else. The predicate is now the home side's own clean sheet.
    MarketDefinition(
        market="Result or clean sheet",
        outcome="Home or clean sheet",
        predicate=_either(_home, _home_clean_sheet),
        display_name="Home win or home clean sheet",
        provider_market="Result or clean sheet",
        provider_outcome="Home win or home clean sheet",
        combination=True,
    ),
    MarketDefinition(
        market="Result or clean sheet",
        outcome="Away or clean sheet",
        predicate=_either(_away, _away_clean_sheet),
        display_name="Away win or away clean sheet",
        provider_market="Result or clean sheet",
        provider_outcome="Away win or away clean sheet",
        combination=True,
    ),
    # Retired: no verified provider semantics.
    #
    # "Draw" names no team, so "draw or clean sheet" has no team whose sheet is
    # meant, and no bookmaker offers a market by this name to check against.
    # Under the old either-side predicate it settled on any 0-x or x-0 score,
    # which is not a bet anyone could place. Kept in the registry rather than
    # deleted so selections already published under it remain resolvable, and
    # marked unsupported so none can be published again.
    MarketDefinition(
        market="Result or clean sheet",
        outcome="Draw or clean sheet",
        predicate=_either(_draw, _either(_home_clean_sheet, _away_clean_sheet)),
        display_name="Draw or clean sheet (retired)",
        provider_market=None,
        supported=False,
        unsupported_reason=(
            "No verified bookmaker equivalent. 'Draw' names no team, so the "
            "clean sheet has no owner, and the market cannot be settled "
            "against a real bet."
        ),
        combination=True,
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


BY_KEY: Final[dict[tuple[str, str], MarketDefinition]] = {d.key: d for d in MARKETS}
"""Every market by stored key, supported or not.

Includes retired markets so a selection published under one stays resolvable.
Publication is gated by :func:`require_publishable`, not by absence here.
"""

PUBLISHABLE: Final[tuple[MarketDefinition, ...]] = tuple(d for d in MARKETS if d.supported)
"""Markets a service may publish."""


def definition_for(market: str, outcome: str) -> MarketDefinition:
    """Return one market's definition, or raise.

    Raises rather than returning ``None`` because every silent failure this
    codebase has produced began with a lookup that returned nothing and a
    caller that carried on.
    """
    found = BY_KEY.get((market, outcome))
    if found is None:
        raise MarketError(f"Unknown market: {market!r} / {outcome!r}.")
    return found


def require_publishable(market: str, outcome: str) -> MarketDefinition:
    """Return a market's definition only if it may be published.

    Called before a selection is written. A market with no settlement rule, no
    probability rule or no provider mapping cannot be published, because a
    selection nobody can settle or place is not a forecast.
    """
    definition = definition_for(market, outcome)
    if not definition.supported:
        raise MarketError(
            f"{market!r} / {outcome!r} is not publishable: "
            f"{definition.unsupported_reason}"
        )
    return definition


def settles_won(market: str, outcome: str, home_goals: int, away_goals: int) -> bool | None:
    """Whether a finished scoreline wins this outcome.

    The same predicate that defined the probability decides the result, so a
    market cannot be priced by one rule and settled by another.

    Returns ``None`` for an unknown market. Retired markets settle normally:
    a selection already published must remain resolvable under the rule it was
    published with.
    """
    found = BY_KEY.get((market, outcome))
    if found is None:
        return None
    return found.settles(home_goals, away_goals)


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
