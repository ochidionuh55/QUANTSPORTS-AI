"""Elo ratings.

Each team carries a rating; the difference between two ratings maps to a win
expectation, and every result moves both ratings by an amount proportional to
how surprising it was. Ratings need no league table, handle teams changing
strength mid-season, and degrade gracefully when data is thin.

**Football needs three adaptations over chess Elo.**

Draws are common — roughly a quarter of matches — but chess Elo produces a
two-way expectation. This implementation scores a draw as 0.5 and derives the
draw probability separately from rating proximity, rather than pretending the
result is binary.

Margin of victory matters. A 5-0 win is stronger evidence than 1-0, so the
update is scaled by goal difference. Without it, ratings converge slowly and
underweight dominant sides.

Home advantage is worth roughly 60-70 rating points in top-division football
and is applied to the expectation, not the rating itself — it is a property of
the fixture, not the team.

**Ratings are replayed chronologically, never fitted.** ``rating_at`` returns
what a team's rating was on a given date, using only matches before it. That is
what makes a backtest honest: a prediction for March uses the March rating, not
the end-of-season one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Final

DEFAULT_RATING: Final[float] = 1500.0
DEFAULT_K: Final[float] = 20.0
"""Update size. Higher reacts faster but tracks noise; 20 is the usual
compromise for a ~38-match season."""

HOME_ADVANTAGE_POINTS: Final[float] = 65.0
DRAW_SPREAD: Final[float] = 260.0
"""Controls how quickly draw probability falls as ratings diverge. Tuned so an
evenly matched fixture gives roughly the observed 25% draw rate."""

MIN_MATCHES_FOR_CONFIDENCE: Final[int] = 8
"""Below this a rating is still mostly its starting value.

Callers must check ``is_established``. Treating a provisional rating as real is
how a newly promoted side gets modelled as average.
"""


@dataclass
class Rating:
    """A team's current rating and the history behind it."""

    team_id: int
    rating: float = DEFAULT_RATING
    matches_played: int = 0
    history: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def is_established(self) -> bool:
        """Whether enough matches back this rating to trust it."""
        return self.matches_played >= MIN_MATCHES_FOR_CONFIDENCE

    def rating_at(self, moment: datetime) -> float:
        """Return the rating as it stood before ``moment``.

        Used by backtesting so a historical prediction cannot see ratings that
        only exist because of later results.
        """
        applicable = [r for when, r in self.history if when < moment]
        return applicable[-1] if applicable else DEFAULT_RATING

    def matches_before(self, moment: datetime) -> int:
        """Return how many matches had been played before ``moment``."""
        return sum(1 for when, _ in self.history if when < moment)


def expected_score(
    home_rating: float, away_rating: float, home_advantage: float = HOME_ADVANTAGE_POINTS
) -> float:
    """Return the home side's expected score in ``[0, 1]``.

    A draw counts as half, so this is not the win probability. Use
    :func:`outcome_probabilities` for that.
    """
    difference = (home_rating + home_advantage) - away_rating
    return 1.0 / (1.0 + 10 ** (-difference / 400.0))


def outcome_probabilities(
    home_rating: float,
    away_rating: float,
    home_advantage: float = HOME_ADVANTAGE_POINTS,
    draw_spread: float = DRAW_SPREAD,
) -> tuple[Decimal, Decimal, Decimal]:
    """Split an expected score into home, draw and away probabilities.

    The draw probability is highest when ratings are close and decays as they
    diverge, which matches observed football. The remaining mass is divided
    between the sides in proportion to the expected score.

    Returns:
        ``(home_win, draw, away_win)``, summing to one.
    """
    expectation = expected_score(home_rating, away_rating, home_advantage)
    difference = abs((home_rating + home_advantage) - away_rating)

    # Peak draw rate for an even fixture, decaying with rating gap.
    draw = 0.28 * math.exp(-((difference / draw_spread) ** 2))
    draw = min(max(draw, 0.05), 0.34)

    remaining = 1.0 - draw
    # Re-centre the expected score onto the non-draw mass. Without this, a
    # 0.5 expectation with a draw carved out would not split evenly.
    home = remaining * expectation
    away = remaining * (1.0 - expectation)

    total = home + draw + away
    return (
        Decimal(str(home / total)),
        Decimal(str(draw / total)),
        Decimal(str(away / total)),
    )


def margin_multiplier(goal_difference: int) -> float:
    """Scale the update by margin of victory.

    Logarithmic rather than linear: the difference between 1-0 and 3-0 is far
    more informative than between 6-0 and 8-0, and a linear scale would let one
    thrashing dominate a season of ratings.
    """
    margin = abs(goal_difference)
    if margin <= 1:
        return 1.0
    # math.pow rather than ** so the return type is a concrete float.
    return 1.0 + 0.5 * math.pow(margin - 1, 0.75)


def actual_score(home_goals: int, away_goals: int) -> float:
    """Return the home side's actual score: 1, 0.5 or 0."""
    if home_goals > away_goals:
        return 1.0
    if home_goals < away_goals:
        return 0.0
    return 0.5


class EloEngine:
    """Maintains ratings by replaying results in chronological order."""

    def __init__(
        self,
        k_factor: float = DEFAULT_K,
        home_advantage: float = HOME_ADVANTAGE_POINTS,
        initial_rating: float = DEFAULT_RATING,
    ) -> None:
        self._k = k_factor
        self._home_advantage = home_advantage
        self._initial = initial_rating
        self._ratings: dict[int, Rating] = {}

    def rating(self, team_id: int) -> Rating:
        """Return a team's rating, creating a provisional one if unseen."""
        if team_id not in self._ratings:
            self._ratings[team_id] = Rating(team_id=team_id, rating=self._initial)
        return self._ratings[team_id]

    @property
    def teams(self) -> tuple[int, ...]:
        """Return every rated team id."""
        return tuple(self._ratings)

    def record(
        self,
        home_team_id: int,
        away_team_id: int,
        home_goals: int,
        away_goals: int,
        played_at: datetime,
    ) -> tuple[float, float]:
        """Apply one result and return the rating change for each side.

        Zero-sum: whatever one side gains, the other loses, so the population
        mean stays fixed and ratings remain comparable across seasons.

        Returns:
            ``(home_change, away_change)``.
        """
        home = self.rating(home_team_id)
        away = self.rating(away_team_id)

        expectation = expected_score(home.rating, away.rating, self._home_advantage)
        actual = actual_score(home_goals, away_goals)
        multiplier = margin_multiplier(home_goals - away_goals)

        change = self._k * multiplier * (actual - expectation)

        home.rating += change
        away.rating -= change
        home.matches_played += 1
        away.matches_played += 1
        home.history.append((played_at, home.rating))
        away.history.append((played_at, away.rating))

        return change, -change

    def replay(self, results: list[tuple[int, int, int, int, datetime]]) -> None:
        """Replay results in date order.

        Sorted internally rather than trusting the caller: applying results out
        of order produces ratings that could not have existed, and the error is
        invisible in the output.
        """
        for home_id, away_id, home_goals, away_goals, played_at in sorted(
            results, key=lambda r: r[4]
        ):
            self.record(home_id, away_id, home_goals, away_goals, played_at)

    def predict(
        self, home_team_id: int, away_team_id: int, moment: datetime | None = None
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Return 1X2 probabilities for a fixture.

        Args:
            home_team_id: Home side.
            away_team_id: Away side.
            moment: If given, uses ratings as they stood before this date, so a
                backtest cannot see the future.
        """
        home = self.rating(home_team_id)
        away = self.rating(away_team_id)

        home_rating = home.rating_at(moment) if moment else home.rating
        away_rating = away.rating_at(moment) if moment else away.rating

        return outcome_probabilities(home_rating, away_rating, self._home_advantage)

    def is_reliable(
        self, home_team_id: int, away_team_id: int, moment: datetime | None = None
    ) -> bool:
        """Whether both sides have enough history for the estimate to mean anything."""
        home = self.rating(home_team_id)
        away = self.rating(away_team_id)
        if moment is None:
            return home.is_established and away.is_established
        return (
            home.matches_before(moment) >= MIN_MATCHES_FOR_CONFIDENCE
            and away.matches_before(moment) >= MIN_MATCHES_FOR_CONFIDENCE
        )
