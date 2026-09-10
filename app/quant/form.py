"""Recent form.

Elo is slow by design: it treats a result from thirty matches ago almost as
seriously as last week's. Form is the opposite — a short-window view that
reacts quickly to a side hitting a run or losing a key player.

**Recency weighting.** Every match is weighted by exponential decay, so last
week's result counts more than one from two months ago. Treating all matches
equally, as the original blueprint's flat "last 5" would, throws away the
information that a team's recent trajectory carries.

**Home and away are kept separate.** A side's home record is weak evidence
about its away scoring rate. Merging them produces an average that describes
neither.

Form is a weak signal on its own and is not intended to stand alone. It exists
to contribute to the ensemble, where its weight is set by measured performance
rather than assumption.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

DEFAULT_WINDOW: Final[int] = 10
DEFAULT_DECAY: Final[float] = 0.85
"""Weight multiplier per match into the past.

At 0.85, a match ten games back carries about 20% of the most recent one's
weight. Lower reacts faster and is noisier.
"""

MIN_MATCHES: Final[int] = 4
"""Below this, form is noise rather than signal."""


@dataclass(frozen=True)
class MatchOutcome:
    """One completed match from a single team's perspective."""

    played_at: datetime
    scored: int
    conceded: int
    at_home: bool

    @property
    def points(self) -> int:
        """League points earned: 3, 1 or 0."""
        if self.scored > self.conceded:
            return 3
        return 1 if self.scored == self.conceded else 0

    @property
    def clean_sheet(self) -> bool:
        """Whether the side conceded nothing."""
        return self.conceded == 0

    @property
    def both_scored(self) -> bool:
        """Whether both sides scored."""
        return self.scored > 0 and self.conceded > 0


@dataclass(frozen=True)
class FormSummary:
    """A team's weighted recent record."""

    team_id: int
    matches: int
    points_per_match: float
    goals_scored_per_match: float
    goals_conceded_per_match: float
    clean_sheet_rate: float
    both_scored_rate: float
    over_2_5_rate: float

    @property
    def is_reliable(self) -> bool:
        """Whether enough matches back the estimate."""
        return self.matches >= MIN_MATCHES

    @property
    def strength(self) -> float:
        """A single number in roughly ``[0, 1]`` summarising form.

        Points per match scaled to its maximum of three. Crude on purpose: a
        composite weighting goals and clean sheets would need weights nobody
        has measured yet, and inventing them would be exactly the kind of
        unvalidated number this project avoids.
        """
        return self.points_per_match / 3.0


def _weights(count: int, decay: float) -> list[float]:
    """Return decay weights, most recent first."""
    return [decay**index for index in range(count)]


def summarise(
    team_id: int,
    matches: list[MatchOutcome],
    window: int = DEFAULT_WINDOW,
    decay: float = DEFAULT_DECAY,
    at_home: bool | None = None,
    before: datetime | None = None,
) -> FormSummary:
    """Summarise a team's recent form.

    Args:
        team_id: Canonical team id.
        matches: The team's matches, any order.
        window: Maximum matches to consider.
        decay: Weight multiplier per match into the past.
        at_home: Restrict to home or away matches when given.
        before: Only use matches before this moment. Required for backtesting,
            where including the match being predicted would leak its result.

    Returns:
        A weighted summary. Check ``is_reliable`` before using it.
    """
    candidates = matches
    if before is not None:
        candidates = [m for m in candidates if m.played_at < before]
    if at_home is not None:
        candidates = [m for m in candidates if m.at_home is at_home]

    recent = sorted(candidates, key=lambda m: m.played_at, reverse=True)[:window]
    if not recent:
        return FormSummary(team_id, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    weights = _weights(len(recent), decay)
    total = sum(weights)

    def weighted(values: list[float]) -> float:
        return sum(v * w for v, w in zip(values, weights, strict=True)) / total

    return FormSummary(
        team_id=team_id,
        matches=len(recent),
        points_per_match=weighted([float(m.points) for m in recent]),
        goals_scored_per_match=weighted([float(m.scored) for m in recent]),
        goals_conceded_per_match=weighted([float(m.conceded) for m in recent]),
        clean_sheet_rate=weighted([1.0 if m.clean_sheet else 0.0 for m in recent]),
        both_scored_rate=weighted([1.0 if m.both_scored else 0.0 for m in recent]),
        over_2_5_rate=weighted([1.0 if m.scored + m.conceded > 2.5 else 0.0 for m in recent]),
    )


def predict(
    home: FormSummary, away: FormSummary, home_bonus: float = 0.10
) -> tuple[Decimal, Decimal, Decimal]:
    """Return crude 1X2 probabilities from two form summaries.

    Deliberately simple. Form's job in the ensemble is to register that a side
    is currently over- or under-performing its long-run rating; asking it to
    produce a precise distribution would be asking more than the signal
    supports.

    Returns:
        ``(home_win, draw, away_win)``, summing to one.
    """
    home_strength = max(home.strength + home_bonus, 0.01)
    away_strength = max(away.strength, 0.01)

    # Draw share shrinks as the strength gap widens.
    gap = abs(home_strength - away_strength)
    draw = max(0.30 - gap * 0.45, 0.12)

    remaining = 1.0 - draw
    total_strength = home_strength + away_strength
    home_win = remaining * (home_strength / total_strength)
    away_win = remaining * (away_strength / total_strength)

    total = home_win + draw + away_win
    return (
        Decimal(str(home_win / total)),
        Decimal(str(draw / total)),
        Decimal(str(away_win / total)),
    )
