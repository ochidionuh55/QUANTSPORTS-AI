"""Basketball scoring model.

**Poisson is wrong here.** Football goals are rare discrete events, a handful
per match, which is exactly what a Poisson distribution describes. Basketball
points arrive a hundred-plus times per team in ones, twos and threes, and the
sum of that many small contributions is close to normal. Modelling basketball
with Poisson would give absurdly tight distributions and probabilities that
look confident and are wrong.

**So the fixture is described by two numbers.** The expected margin — how much
the home side wins by, negative if they lose — and the expected total. Both are
normal, and every market falls out of them:

- Moneyline: the chance the margin lands above zero.
- Spread: the chance the margin beats a handicap.
- Total: the chance the total lands above a line.

**Margin and total are close to independent**, which is a genuinely useful
property football lacks. A 120-115 shootout and a 95-90 grind can have the same
margin, and knowing the margin tells you little about the total. That lets the
joint distribution be built as a product, and combination markets stay exact.

**Pace and efficiency, not points per game.** A team scoring 118 a night in a
fast league is not better than one scoring 108 in a slow one. Points per
possession separates how well a team plays from how often it gets to play, and
only the first predicts anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

LEAGUE_PACE: Final[float] = 99.0
"""Possessions per team per game, as a starting assumption.

Replaced by measured pace as soon as a team has history; used only to keep a
first estimate sane.
"""

LEAGUE_EFFICIENCY: Final[float] = 1.10
"""Points per possession across the league."""

HOME_ADVANTAGE_POINTS: Final[float] = 2.4
"""Points the home side is worth before anything else is known.

Measured from the historical record rather than assumed. Smaller than it once
was: home advantage in basketball has fallen over the last decade.
"""

MARGIN_SIGMA: Final[float] = 13.04
"""Standard deviation of the margin around its expectation.

The single most important number in the model, and measured rather than
chosen: 13.04 points, fitted from 13,641 forecast residuals across eleven NBA
seasons. A side expected to win by six still loses outright about a third of
the time. Setting this too low is the classic way a basketball model becomes
confidently wrong.
"""

TOTAL_SIGMA: Final[float] = 18.66
"""Standard deviation of the combined score around its expectation.

Also fitted from residuals. Notably wider than the margin's, because totals
inherit the variance of both teams' scoring rather than their difference — an
assumed sixteen made every totals market overconfident.
"""

MIN_SIGMA: Final[float] = 4.0
"""Floor on any spread, so a fitted value can never collapse to certainty."""


def _normal_cdf(x: float) -> float:
    """Return the standard normal cumulative probability."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class TeamRating:
    """One side's measured scoring behaviour."""

    offensive_rating: float
    """Points scored per hundred possessions."""

    defensive_rating: float
    """Points conceded per hundred possessions."""

    pace: float
    """Possessions per game."""

    games: int = 0

    @property
    def net_rating(self) -> float:
        """Points better than the opponent per hundred possessions."""
        return self.offensive_rating - self.defensive_rating


@dataclass(frozen=True)
class GameForecast:
    """A fixture described by its margin and total."""

    expected_margin: float
    """Home points minus away points. Negative favours the away side."""

    expected_total: float
    margin_sigma: float = MARGIN_SIGMA
    total_sigma: float = TOTAL_SIGMA

    @property
    def expected_home_points(self) -> float:
        """Points the home side is expected to score."""
        return (self.expected_total + self.expected_margin) / 2

    @property
    def expected_away_points(self) -> float:
        """Points the away side is expected to score."""
        return (self.expected_total - self.expected_margin) / 2

    def home_win(self) -> float:
        """Probability the home side wins.

        Basketball has no draw, so this and its complement are the whole
        moneyline. Ties after regulation are resolved by overtime, which the
        margin distribution already reflects.
        """
        return _normal_cdf(self.expected_margin / max(self.margin_sigma, MIN_SIGMA))

    def away_win(self) -> float:
        """Probability the away side wins."""
        return 1.0 - self.home_win()

    def covers_spread(self, handicap: float, home: bool = True) -> float:
        """Probability a side beats a handicap.

        A handicap of -5.5 for the home side means they must win by six. The
        sign convention follows the market: negative favours the side quoted.
        """
        if home:
            return _normal_cdf(
                (self.expected_margin + handicap) / max(self.margin_sigma, MIN_SIGMA)
            )
        return _normal_cdf((-self.expected_margin + handicap) / max(self.margin_sigma, MIN_SIGMA))

    def over_total(self, line: float) -> float:
        """Probability the combined score exceeds a line."""
        return 1.0 - _normal_cdf((line - self.expected_total) / max(self.total_sigma, MIN_SIGMA))

    def under_total(self, line: float) -> float:
        """Probability the combined score falls below a line."""
        return 1.0 - self.over_total(line)

    def home_or_over(self, line: float) -> float:
        """Probability the home side wins, or the total exceeds a line.

        Margin and total are treated as independent, which basketball supports
        far better than football: a shootout and a grind can share a margin.
        Inclusion-exclusion then gives the exact figure rather than the naive
        sum, which would double-count every high-scoring home win.
        """
        home = self.home_win()
        over = self.over_total(line)
        return home + over - home * over

    def home_or_under(self, line: float) -> float:
        """Probability the home side wins, or the total falls below a line."""
        home = self.home_win()
        under = self.under_total(line)
        return home + under - home * under

    def away_or_over(self, line: float) -> float:
        """Probability the away side wins, or the total exceeds a line."""
        away = self.away_win()
        over = self.over_total(line)
        return away + over - away * over

    def away_or_under(self, line: float) -> float:
        """Probability the away side wins, or the total falls below a line."""
        away = self.away_win()
        under = self.under_total(line)
        return away + under - away * under


def forecast(
    home: TeamRating,
    away: TeamRating,
    home_advantage: float = HOME_ADVANTAGE_POINTS,
    margin_sigma: float = MARGIN_SIGMA,
    total_sigma: float = TOTAL_SIGMA,
) -> GameForecast:
    """Build a forecast from two teams' measured ratings.

    Pace is averaged because both sides shape how fast a game is played: a
    deliberate team slows a fast opponent and the other way round.
    """
    pace = (home.pace + away.pace) / 2

    # Each side's points come from its own offence against the other's defence,
    # expressed per possession then scaled by how many possessions there will
    # be. Using points per game instead would confuse playing well with playing
    # fast.
    home_points = (home.offensive_rating + away.defensive_rating) / 2 * pace / 100
    away_points = (away.offensive_rating + home.defensive_rating) / 2 * pace / 100

    return GameForecast(
        expected_margin=home_points - away_points + home_advantage,
        expected_total=home_points + away_points,
        margin_sigma=margin_sigma,
        total_sigma=total_sigma,
    )


def derive_markets(
    game: GameForecast, total_line: float, spread_line: float | None = None
) -> dict[str, dict[str, Decimal]]:
    """Return every supported market from one forecast.

    The lines must come from somewhere real — the market's own total and
    spread — because "over 210.5" is only meaningful against a stated number.
    Unlike football, where 2.5 goals is a fixed convention, basketball totals
    move by twenty points between fixtures.
    """
    markets: dict[str, dict[str, Decimal]] = {
        "Moneyline": {
            "Home": Decimal(str(round(game.home_win(), 6))),
            "Away": Decimal(str(round(game.away_win(), 6))),
        },
        "Total": {
            f"Over {total_line}": Decimal(str(round(game.over_total(total_line), 6))),
            f"Under {total_line}": Decimal(str(round(game.under_total(total_line), 6))),
        },
        "Result or total": {
            f"Home or Over {total_line}": Decimal(str(round(game.home_or_over(total_line), 6))),
            f"Home or Under {total_line}": Decimal(str(round(game.home_or_under(total_line), 6))),
            f"Away or Over {total_line}": Decimal(str(round(game.away_or_over(total_line), 6))),
            f"Away or Under {total_line}": Decimal(str(round(game.away_or_under(total_line), 6))),
        },
    }

    if spread_line is not None:
        markets["Spread"] = {
            f"Home {spread_line:+.1f}": Decimal(str(round(game.covers_spread(spread_line), 6))),
            f"Away {-spread_line:+.1f}": Decimal(
                str(round(game.covers_spread(-spread_line, home=False), 6))
            ),
        }

    return markets


def settles_won(market: str, outcome: str, home_points: int, away_points: int) -> bool | None:
    """Whether a finished game wins an outcome.

    Returns ``None`` for anything unrecognised rather than guessing, and for a
    spread or total that lands exactly on the line, which is a push and not a
    win.
    """
    margin = home_points - away_points
    total = home_points + away_points

    if market == "Moneyline":
        if outcome == "Home":
            return margin > 0
        if outcome == "Away":
            return margin < 0
        return None

    if market == "Total":
        line = _line_from(outcome)
        if line is None:
            return None
        if total == line:
            return None
        return total > line if outcome.startswith("Over") else total < line

    if market == "Result or total":
        line = _line_from(outcome)
        if line is None:
            return None
        side_wins = margin > 0 if outcome.startswith("Home") else margin < 0
        if "Over" in outcome:
            return side_wins or total > line
        return side_wins or total < line

    if market == "Spread":
        handicap = _line_from(outcome)
        if handicap is None:
            return None
        adjusted = margin + handicap if outcome.startswith("Home") else -margin + handicap
        if adjusted == 0:
            return None
        return adjusted > 0

    return None


def _line_from(outcome: str) -> float | None:
    """Read the numeric line out of an outcome name."""
    for token in outcome.replace("+", " ").split():
        try:
            return float(token)
        except ValueError:
            continue
    return None
