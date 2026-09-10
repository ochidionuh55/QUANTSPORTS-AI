"""The three daily boards.

Three different questions, three different methods, scored separately. One
universal "best game" was the wrong shape for the product — different users
want different things, and pretending a single list serves them all is how a
daily feature becomes noise.

**🎯 Banker Board — what is most likely to happen.**
Ranked by raw probability, restricted to fully modelled fixtures with real
depth behind them. The list a cautious reader wants. It is emphatically *not*
the list with the most value: a 90% outcome priced at 90% is worth nothing to
back, and the board says so.

**🔥 Sharp Board — what is most unusual.**
Ranked by informational content against how often the outcome normally
happens. A 62% draw beats an 89% Over 1.5 here, because draws are rare and
Over 1.5 is not. This is the board that says "look at this one".

**📊 Pattern Board — what the history alone suggests.**
No model at all. Both clubs' counted records over three seasons pointing the
same way: two sides who both go under 2.5 in most of their matches, or both
see goals at both ends. A reader who distrusts every forecast we make can use
this board, which is precisely why it exists.

Each board publishes nothing when nothing qualifies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import StoredAnalysis
from app.services.leans import (
    EXCLUDED_OUTCOMES,
    MARKET_BASELINES,
    Lean,
    LeanBuilder,
    base_rate_for,
)
from app.services.profiles import ProfileService, SplitRecord

logger = get_logger(__name__)

BANKER: Final[str] = "banker"
SHARP: Final[str] = "sharp"
PATTERN: Final[str] = "pattern"

TRACK_LABELS: Final[dict[str, str]] = {
    BANKER: "🎯 Banker Board",
    SHARP: "🔥 Sharp Board",
    PATTERN: "📊 Pattern Board",
}

TRACK_DESCRIPTIONS: Final[dict[str, str]] = {
    BANKER: (
        "The most likely outcomes on the card, from fully modelled fixtures "
        "only. Most likely is not the same as best value — a 90% outcome "
        "priced at 90% is worth nothing to back."
    ),
    SHARP: (
        "The most unusual conclusions on the card — outcomes far likelier here "
        "than they are in a typical match. Ranked by information carried, not "
        "by raw probability."
    ),
    PATTERN: (
        "Driven entirely by counted history, with no model involved. Both "
        "clubs' records over three seasons pointing the same way."
    ),
}

BANKER_MIN_PROBABILITY: Final[float] = 0.68
"""The bar for the Banker Board.

High enough that the list means something. Below this it is just the card
sorted by probability, which the Markets explorer already does better.
"""

BANKER_MIN_MATCHES: Final[int] = 25
PATTERN_MIN_RATE: Final[float] = 0.65
"""How often both clubs must have shown a pattern for it to count."""

PATTERN_MIN_MATCHES: Final[int] = 20
DAILY_PER_TRACK: Final[int] = 3


@dataclass(frozen=True)
class BoardEntry:
    """One selection on a board."""

    track: str
    fixture_id: str
    home_name: str
    away_name: str
    competition: str | None
    kickoff: datetime
    market: str
    outcome: str
    probability: float
    base_rate: float
    score: float
    coverage: str
    rationale: str
    factors: dict[str, object]


class BoardService:
    """Builds the three daily boards."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build(
        self,
        records: list[StoredAnalysis],
        per_track: int = DAILY_PER_TRACK,
        now: datetime | None = None,
    ) -> dict[str, list[BoardEntry]]:
        """Return every board, keyed by track."""
        moment = now or datetime.now(UTC)
        upcoming = [r for r in records if _kickoff(r) > moment]
        return {
            BANKER: self._banker(upcoming, per_track),
            SHARP: self._sharp(upcoming, per_track),
            PATTERN: await self._pattern(upcoming, per_track),
        }

    def _banker(self, records: list[StoredAnalysis], limit: int) -> list[BoardEntry]:
        """Most likely outcomes, from well-evidenced fixtures only."""
        entries: list[BoardEntry] = []

        for record in records:
            if record.coverage != "fully_modelled":
                continue
            if _sample(record) < BANKER_MIN_MATCHES:
                continue

            markets = record.markets or {}
            if not isinstance(markets, dict):
                continue

            for market_name, outcomes in markets.items():
                if market_name not in MARKET_BASELINES or not isinstance(outcomes, dict):
                    continue
                for outcome, raw in outcomes.items():
                    if outcome in EXCLUDED_OUTCOMES:
                        continue
                    try:
                        probability = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if probability < BANKER_MIN_PROBABILITY:
                        continue

                    base = base_rate_for(market_name, outcome)
                    entries.append(
                        BoardEntry(
                            track=BANKER,
                            fixture_id=record.provider_event_id,
                            home_name=record.home_name,
                            away_name=record.away_name,
                            competition=record.competition,
                            kickoff=_kickoff(record),
                            market=market_name,
                            outcome=outcome,
                            probability=probability,
                            base_rate=base,
                            score=probability,
                            coverage=record.coverage,
                            rationale=(
                                f"{outcome} at {probability * 100:.0f}%, the "
                                f"highest-probability qualifying outcome on this "
                                f"fixture. Normally {base * 100:.0f}%. Fully "
                                f"modelled with {_sample(record)} matches behind "
                                "each side."
                            ),
                            factors={
                                "probability": round(probability, 4),
                                "base_rate": round(base, 4),
                                "sample": _sample(record),
                            },
                        )
                    )

        entries.sort(key=lambda entry: entry.probability, reverse=True)
        return _one_per_fixture(entries, limit)

    def _sharp(self, records: list[StoredAnalysis], limit: int) -> list[BoardEntry]:
        """Most informative departures from the base rate."""
        leans = LeanBuilder().rank(list(records), limit=limit * 3)
        return [
            BoardEntry(
                track=SHARP,
                fixture_id=lean.fixture_id,
                home_name=lean.home_name,
                away_name=lean.away_name,
                competition=lean.competition,
                kickoff=lean.kickoff,  # type: ignore[arg-type]
                market=lean.market,
                outcome=lean.outcome,
                probability=lean.probability,
                base_rate=base_rate_for(lean.market, lean.outcome),
                score=lean.score,
                coverage=lean.coverage,
                rationale=lean.reason,
                factors=_lean_factors(lean),
            )
            for lean in leans[:limit]
        ]

    async def _pattern(self, records: list[StoredAnalysis], limit: int) -> list[BoardEntry]:
        """Fixtures where both clubs' counted records agree.

        No model, no forecast. Two sides who each go under 2.5 in most of their
        matches make a low-scoring fixture likely for reasons a reader can
        verify by counting.
        """
        service = ProfileService(self._session)
        entries: list[BoardEntry] = []

        for record in records:
            home_stats = record.home_stats or {}
            away_stats = record.away_stats or {}
            if not (home_stats.get("resolved") and away_stats.get("resolved")):
                continue

            home_id = home_stats.get("team_id")
            away_id = away_stats.get("team_id")
            if not isinstance(home_id, int) or not isinstance(away_id, int):
                continue

            home = await service.team_profile(home_id)
            away = await service.team_profile(away_id)
            if home is None or away is None:
                continue
            if min(home.overall.played, away.overall.played) < PATTERN_MIN_MATCHES:
                continue

            for market, outcome, extract in _PATTERNS:
                home_rate = extract(home.overall)
                away_rate = extract(away.overall)
                if home_rate is None or away_rate is None:
                    continue
                if min(home_rate, away_rate) < PATTERN_MIN_RATE:
                    continue

                combined = (home_rate + away_rate) / 2
                entries.append(
                    BoardEntry(
                        track=PATTERN,
                        fixture_id=record.provider_event_id,
                        home_name=record.home_name,
                        away_name=record.away_name,
                        competition=record.competition,
                        kickoff=_kickoff(record),
                        market=market,
                        outcome=outcome,
                        probability=combined,
                        base_rate=base_rate_for(market, outcome),
                        score=combined,
                        coverage=record.coverage,
                        rationale=(
                            f"{record.home_name} have seen {outcome.lower()} in "
                            f"{home_rate * 100:.0f}% of {home.overall.played} "
                            f"matches; {record.away_name} in "
                            f"{away_rate * 100:.0f}% of {away.overall.played}. "
                            "Counted from the record — no model involved."
                        ),
                        factors={
                            "home_rate": round(home_rate, 4),
                            "away_rate": round(away_rate, 4),
                            "home_matches": home.overall.played,
                            "away_matches": away.overall.played,
                        },
                    )
                )

        entries.sort(key=lambda entry: entry.score, reverse=True)
        return _one_per_fixture(entries, limit)


def _kickoff(record: StoredAnalysis) -> datetime:
    """Return a timezone-aware kickoff."""
    kickoff = record.kickoff
    return kickoff if kickoff.tzinfo else kickoff.replace(tzinfo=UTC)


def _sample(record: StoredAnalysis) -> int:
    """Return the smaller of the two sides' match counts."""
    stats = (record.home_stats or {}, record.away_stats or {})
    counts: list[int] = []
    for entry in stats:
        raw = entry.get("matches", 0) if isinstance(entry, dict) else 0
        try:
            counts.append(int(str(raw)))
        except (TypeError, ValueError):
            counts.append(0)
    return min(counts) if counts else 0


def _one_per_fixture(entries: list[BoardEntry], limit: int) -> list[BoardEntry]:
    """Keep the strongest entry per fixture.

    A board listing the same match three times under different markets is one
    idea padded out, not three selections.
    """
    seen: set[str] = set()
    chosen: list[BoardEntry] = []
    for entry in entries:
        if entry.fixture_id in seen:
            continue
        seen.add(entry.fixture_id)
        chosen.append(entry)
        if len(chosen) >= limit:
            break
    return chosen


def _lean_factors(lean: Lean) -> dict[str, object]:
    """Return a lean's score components for the "why" screen."""
    return {
        "informational_content": round(lean.confidence, 4),
        "coverage": round(lean.coverage_score, 4),
        "data_depth": round(lean.data_quality, 4),
        "model_agreement": round(lean.stability, 4),
        "market_agreement": round(lean.market_agreement, 4),
        "overall": round(lean.score, 4),
    }


# Historical patterns worth surfacing, as (market, outcome, how to measure).
_PATTERNS: Final[tuple[tuple[str, str, Callable[[SplitRecord], float | None]], ...]] = (
    ("Goals", "Under 2.5", lambda r: r.rate(r.played - r.over_2_5)),
    ("Goals", "Over 2.5", lambda r: r.rate(r.over_2_5)),
    ("Goals", "Over 1.5", lambda r: r.rate(r.over_1_5)),
    ("Both teams to score", "Yes", lambda r: r.rate(r.both_scored)),
    ("Both teams to score", "No", lambda r: r.rate(r.played - r.both_scored)),
)
