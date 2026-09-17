"""One place that decides whether a stored fixture may train a model.

**Stored and trainable are different questions.** A fixture that was abandoned
still happened — it was scheduled, it appeared on the card, and a history that
omits it implies a match that never existed. So everything the provider returns
is stored. What changes is whether it may inform a model.

**One policy, not a condition per caller.** Ingestion, model fitting, backtests
and validation all need the same answer, and the way this goes wrong is a
status accepted in one place and rejected in another. That produces a model
trained on rows a backtest excludes, and the discrepancy is invisible because
both look reasonable alone.

**Eligibility is derived, never assigned.** A fixture is trainable because its
status and score say so, not because a competition was trusted or a season was
judged good. Hard-coding "NPFL 2018 is unreliable" would be a hidden opinion in
a table of facts, and would not survive the next season nobody remembered to
check.
"""

from __future__ import annotations

from enum import Enum
from typing import Final


class FixtureStatus(str, Enum):
    """Why a stored fixture is or is not trainable."""

    FINISHED = "FINISHED"
    """Played to a result. Trainable when a score is present."""

    ABANDONED = "ABANDONED"
    """Started and stopped, or awarded off the pitch. Never trainable."""

    NOT_PLAYED = "NOT_PLAYED"
    """Scheduled, postponed or cancelled. No result exists yet or ever."""

    IN_PROGRESS = "IN_PROGRESS"
    """Under way. Not trainable until it finishes."""

    UNKNOWN = "UNKNOWN"
    """A status we do not recognise. Never trainable.

    Deliberately not treated as finished: an unrecognised code is a gap in our
    knowledge, and guessing in the permissive direction would feed unverified
    rows into a model.
    """


FINISHED_CODES: Final[frozenset[str]] = frozenset({"FT", "AET", "PEN"})
"""Provider codes meaning a result stands.

``AET`` and ``PEN`` are included because the ninety-minute score is still a
real football result. What a cup tie's shootout decided is a separate question
from what the teams scored.
"""

ABANDONED_CODES: Final[frozenset[str]] = frozenset(
    {"ABD", "SUSP", "INT", "AWD", "WO"}
)
"""Abandoned, suspended, interrupted, awarded or walkover.

An awarded result is a disciplinary outcome, not a scoring one, so it must
never train a model that estimates goals.
"""

NOT_PLAYED_CODES: Final[frozenset[str]] = frozenset({"NS", "TBD", "PST", "CANC"})

IN_PROGRESS_CODES: Final[frozenset[str]] = frozenset(
    {"1H", "HT", "2H", "ET", "BT", "P", "LIVE"}
)


def classify_status(code: str | None) -> FixtureStatus:
    """Return what a provider status code means for training."""
    normalised = (code or "").strip().upper()
    if normalised in FINISHED_CODES:
        return FixtureStatus.FINISHED
    if normalised in ABANDONED_CODES:
        return FixtureStatus.ABANDONED
    if normalised in NOT_PLAYED_CODES:
        return FixtureStatus.NOT_PLAYED
    if normalised in IN_PROGRESS_CODES:
        return FixtureStatus.IN_PROGRESS
    return FixtureStatus.UNKNOWN


def is_training_eligible(
    code: str | None, home_goals: int | None, away_goals: int | None
) -> bool:
    """Whether this fixture may inform a model.

    Both conditions must hold: the status says a result stands, and both scores
    are present. A finished fixture missing a score is stored and excluded —
    the match happened, we simply cannot say what it was.
    """
    if classify_status(code) is not FixtureStatus.FINISHED:
        return False
    return home_goals is not None and away_goals is not None


def exclusion_reason(
    code: str | None, home_goals: int | None, away_goals: int | None
) -> str | None:
    """Return why a fixture is excluded from training, or ``None``.

    Present so a reconciliation can account for every stored fixture. A row
    missing from training without a stated reason is indistinguishable from a
    row lost by accident.
    """
    status = classify_status(code)
    if status is FixtureStatus.FINISHED:
        if home_goals is None or away_goals is None:
            return "finished without a final score"
        return None
    if status is FixtureStatus.ABANDONED:
        return f"abandoned or awarded ({(code or '').upper()})"
    if status is FixtureStatus.NOT_PLAYED:
        return f"not played ({(code or '').upper()})"
    if status is FixtureStatus.IN_PROGRESS:
        return "in progress"
    return f"unrecognised status ({(code or '').upper() or 'blank'})"


__all__ = [
    "ABANDONED_CODES",
    "FINISHED_CODES",
    "IN_PROGRESS_CODES",
    "NOT_PLAYED_CODES",
    "FixtureStatus",
    "classify_status",
    "exclusion_reason",
    "is_training_eligible",
]
