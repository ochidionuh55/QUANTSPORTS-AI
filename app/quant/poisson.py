"""Poisson goal model.

Football scorelines are approximately Poisson-distributed around each side's
expected goals, so estimating two rates — home and away — yields a full
distribution over scorelines, and from that every derived market: 1X2, totals,
both-teams-to-score, correct score.

Rates come from attack and defence strengths relative to the league average::

    lambda_home = league_home_avg x home_attack x away_defence
    lambda_away = league_away_avg x away_attack x home_defence

**Known limitations, stated rather than discovered later.** Poisson assumes
goals arrive independently at a constant rate, which is false: teams change
behaviour when leading, and low scores are correlated between sides. This
under-predicts draws and 0-0 in particular. The Dixon-Coles correction exists
precisely to fix that and arrives in Phase 8.

This model is not a prediction of results. It is an estimator whose calibration
must be measured before anything it produces is shown to a user.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

MAX_GOALS: Final[int] = 10
"""Scoreline grid limit.

Ten goals per side covers essentially all probability mass; the residual is
renormalised away. Extending the grid changes results in the fifth decimal
place and costs quadratic time.
"""

MIN_MATCHES_FOR_STRENGTH: Final[int] = 5
"""Below this, a team's strength estimate is not trustworthy.

A side with two matches can look twice as good as the league. Callers must
check ``is_reliable`` and treat an unreliable estimate as unmodellable rather
than shrinking silently toward the mean, which would hide the data gap.
"""

DEFAULT_HOME_ADVANTAGE: Final[float] = 1.0
"""Home advantage is carried by the separate home and away league averages
rather than a multiplier, so this stays neutral by default."""


@dataclass(frozen=True)
class TeamStrength:
    """A team's scoring and conceding rates relative to the league.

    Attributes:
        attack: Goals scored relative to league average. Above 1.0 is better.
        defence: Goals conceded relative to league average. Below 1.0 is better.
        matches_played: Sample size behind the estimate.
    """

    team_id: int
    attack: float
    defence: float
    matches_played: int

    @property
    def is_reliable(self) -> bool:
        """Whether the sample is large enough to use."""
        return self.matches_played >= MIN_MATCHES_FOR_STRENGTH


@dataclass(frozen=True)
class LeagueAverages:
    """Baseline scoring rates for a competition and season."""

    home_goals: float
    away_goals: float
    matches: int

    @property
    def is_reliable(self) -> bool:
        """Whether enough matches back the baseline."""
        return self.matches >= 20


@dataclass(frozen=True)
class MatchProbabilities:
    """A full distribution over outcomes for one fixture."""

    lambda_home: float
    lambda_away: float
    home_win: Decimal
    draw: Decimal
    away_win: Decimal
    over_under: dict[str, Decimal]
    both_teams_score: Decimal
    scoreline: dict[tuple[int, int], Decimal]

    @property
    def most_likely_score(self) -> tuple[int, int]:
        """The single most probable scoreline."""
        return max(self.scoreline.items(), key=lambda kv: kv[1])[0]


def poisson_pmf(k: int, rate: float) -> float:
    """Return the Poisson probability of exactly ``k`` events."""
    if rate <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-rate) * (rate**k) / math.factorial(k)


def league_averages(
    results: list[tuple[int, int]],
) -> LeagueAverages:
    """Estimate baseline home and away scoring rates.

    Args:
        results: ``(home_goals, away_goals)`` for every match in the league.

    Raises:
        ValueError: If no results are supplied.
    """
    if not results:
        raise ValueError("Cannot estimate league averages from no matches.")
    count = len(results)
    return LeagueAverages(
        home_goals=sum(h for h, _ in results) / count,
        away_goals=sum(a for _, a in results) / count,
        matches=count,
    )


def team_strength(
    team_id: int,
    home_matches: list[tuple[int, int]],
    away_matches: list[tuple[int, int]],
    averages: LeagueAverages,
) -> TeamStrength:
    """Estimate a team's attack and defence strength.

    Home and away performance are kept separate when computing the ratios,
    because a side's home record is not evidence about its away scoring rate at
    the same baseline.

    Args:
        team_id: Canonical team id.
        home_matches: ``(scored, conceded)`` from this team's home fixtures.
        away_matches: ``(scored, conceded)`` from this team's away fixtures.
        averages: League baselines.
    """
    played = len(home_matches) + len(away_matches)
    if played == 0:
        return TeamStrength(team_id, attack=1.0, defence=1.0, matches_played=0)

    scored = sum(s for s, _ in home_matches) + sum(s for s, _ in away_matches)
    conceded = sum(c for _, c in home_matches) + sum(c for _, c in away_matches)

    baseline = (averages.home_goals + averages.away_goals) / 2
    if baseline <= 0:
        return TeamStrength(team_id, 1.0, 1.0, played)

    expected = baseline * played
    return TeamStrength(
        team_id=team_id,
        attack=(scored / expected) if expected else 1.0,
        defence=(conceded / expected) if expected else 1.0,
        matches_played=played,
    )


def expected_goals(
    home: TeamStrength,
    away: TeamStrength,
    averages: LeagueAverages,
    home_advantage: float = DEFAULT_HOME_ADVANTAGE,
) -> tuple[float, float]:
    """Return expected goals for each side.

    Rates are floored just above zero: a rate of exactly zero would make a
    clean sheet certain, which no estimate justifies.
    """
    lambda_home = max(averages.home_goals * home.attack * away.defence * home_advantage, 0.01)
    lambda_away = max(averages.away_goals * away.attack * home.defence, 0.01)
    return lambda_home, lambda_away


def score_matrix(
    lambda_home: float, lambda_away: float, max_goals: int = MAX_GOALS
) -> dict[tuple[int, int], Decimal]:
    """Return the probability of every scoreline up to ``max_goals``.

    Independence between the two sides is assumed here. That assumption is
    known to be wrong for low scores and is corrected by Dixon-Coles later.
    """
    grid: dict[tuple[int, int], float] = {}
    for home_goals in range(max_goals + 1):
        home_probability = poisson_pmf(home_goals, lambda_home)
        for away_goals in range(max_goals + 1):
            grid[(home_goals, away_goals)] = home_probability * poisson_pmf(away_goals, lambda_away)

    # Renormalise so the truncated grid still sums to one.
    total = sum(grid.values())
    return {k: Decimal(str(v / total)) for k, v in grid.items()}


def match_probabilities(
    lambda_home: float,
    lambda_away: float,
    totals: tuple[float, ...] = (0.5, 1.5, 2.5, 3.5),
    max_goals: int = MAX_GOALS,
) -> MatchProbabilities:
    """Derive every market probability from two scoring rates.

    Args:
        lambda_home: Expected home goals.
        lambda_away: Expected away goals.
        totals: Over/under lines to compute.
        max_goals: Scoreline grid limit.
    """
    grid = score_matrix(lambda_home, lambda_away, max_goals)

    home_win = sum((p for (h, a), p in grid.items() if h > a), Decimal(0))
    draw = sum((p for (h, a), p in grid.items() if h == a), Decimal(0))
    away_win = sum((p for (h, a), p in grid.items() if h < a), Decimal(0))

    over_under: dict[str, Decimal] = {}
    for line in totals:
        over = sum((p for (h, a), p in grid.items() if h + a > line), Decimal(0))
        over_under[f"over_{line}"] = over
        over_under[f"under_{line}"] = Decimal(1) - over

    both_score = sum((p for (h, a), p in grid.items() if h > 0 and a > 0), Decimal(0))

    return MatchProbabilities(
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        home_win=home_win,
        draw=draw,
        away_win=away_win,
        over_under=over_under,
        both_teams_score=both_score,
        scoreline=grid,
    )
