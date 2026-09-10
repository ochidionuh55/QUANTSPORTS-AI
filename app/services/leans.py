"""Statistical leans.

Identifies the strongest statistical conclusion available for a fixture across
every derived market, and ranks leans against each other so a daily card can be
sorted by how much the analysis actually supports.

**This is not a value bet and must never be presented as one.** A lean says
"this is the most confident thing our models can say about this match". It says
nothing about whether the price is generous, because our models have not
demonstrated an edge over market prices and we do not pretend otherwise.

**Ranking by probability alone would be useless.** "Over 0.5 goals at 94%"
would top every list, every day, and tell nobody anything. A lean is scored on
five components instead:

* **Confidence** — how far the probability sits from the market's own view of
  the same outcome, or from a coin flip where no market exists. A 94% Over 0.5
  is near-certain and unremarkable; a 62% draw is a genuine statement.
* **Coverage** — fully modelled fixtures score above partially modelled ones,
  because fewer components means a thinner basis.
* **Data quality** — how much history sits behind both sides.
* **Stability** — whether the independent models agree. Poisson saying 55% and
  Elo saying 30% is not a conclusion, it is a disagreement.
* **Market agreement** — whether the market broadly concurs. Agreement is
  scored *positively* here, which is the opposite of value betting and is
  deliberate: without a demonstrated edge, disagreeing with the market is more
  likely to be our error than our insight.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

# How often each outcome happens across all 113,029 matches on record.
#
# Measured, not assumed. A single baseline per market was the flaw in the first
# version: "Goals" was scored against 0.5, but Over 1.5 lands 74% of the time,
# so an 89% Over 1.5 looked like a dramatic departure when it is a fairly
# ordinary match. Every lean in the first day's top five was a goals line as a
# result.
#
# Recompute with: python scripts/base_rates.py
BASE_RATES: Final[dict[str, float]] = {
    "Home": 0.4375,
    "Draw": 0.2620,
    "Away": 0.3004,
    "Home win": 0.4375,
    "Away win": 0.3004,
    "1X (home or draw)": 0.6996,
    "12 (home or away)": 0.7380,
    "X2 (draw or away)": 0.5625,
    "Over 0.5": 0.9242,
    "Under 0.5": 0.0758,
    "Over 1.5": 0.7441,
    "Under 1.5": 0.2559,
    "Over 2.5": 0.5005,
    "Under 2.5": 0.4995,
    "Over 3.5": 0.2795,
    "Under 3.5": 0.7205,
    "Yes": 0.5226,
    "No": 0.4774,
}

MARKET_BASELINES: Final[dict[str, float]] = {
    "1X2": 1 / 3,
    "Double chance": 2 / 3,
    "Result": 1 / 3,
    "Goals": 0.5,
    "Both teams to score": 0.5,
}
"""Fallback when an outcome has no measured rate, so a new market still
ranks rather than being silently dropped."""

MAX_BITS: Final[float] = 0.75
"""Information gain treated as a maximally interesting lean.

A 62% draw against a 26% base rate carries about 0.41 bits; anything past
0.75 is close to certainty and rare enough that capping loses nothing.
"""

MAX_PER_MARKET: Final[int] = 2
"""Cap on how many leans from one market appear in a ranked list.

Without it a day where goals lines happen to score well produces five goals
lines, and the screen stops being a summary of the card.
"""

EXCLUDED_OUTCOMES: Final[frozenset[str]] = frozenset({"Over 0.5", "Under 0.5", "Under 3.5"})
"""Outcomes whose probability is nearly fixed regardless of the fixture."""

MIN_PROBABILITY: Final[float] = 0.45
"""Below this, an outcome is not the strongest thing we can say about a match."""

WEIGHTS: Final[dict[str, float]] = {
    "confidence": 0.40,
    "coverage": 0.15,
    "data_quality": 0.15,
    "stability": 0.15,
    "market_agreement": 0.15,
}

COVERAGE_SCORES: Final[dict[str, float]] = {
    "fully_modelled": 1.0,
    "partially_modelled": 0.6,
    "data_only": 0.25,
    "unsupported": 0.0,
}

MATCHES_FOR_FULL_QUALITY: Final[int] = 40


@dataclass(frozen=True)
class Lean:
    """The strongest statistical conclusion for one fixture."""

    fixture_id: str
    home_name: str
    away_name: str
    competition: str | None
    kickoff: object

    market: str
    outcome: str
    probability: float

    score: float
    confidence: float
    coverage_score: float
    data_quality: float
    stability: float
    market_agreement: float
    coverage: str
    reason: str

    @property
    def headline(self) -> str:
        """Short description of the lean."""
        return f"{self.outcome} — {self.probability * 100:.0f}%"


def _confidence(probability: float, baseline: float) -> float:
    """Score how surprising a probability is, given how often it normally happens.

    Kullback-Leibler divergence between the forecast and the base rate — the
    information a forecast carries beyond simply knowing the long-run
    frequency. In bits.

    This is what makes the ranking meaningful. Under the older
    headroom-above-baseline measure, an 89% Over 1.5 outranked a 62% draw. But
    Over 1.5 lands 74% of the time anyway, so 89% tells you little; draws
    happen 26% of the time, so 62% is a genuinely strong statement. Measured in
    bits, the draw carries 0.41 and the goals line 0.10, which is the right way
    round.

    Args:
        probability: The forecast.
        baseline: How often this outcome normally happens.

    Returns:
        Between 0 and 1, capped at :data:`MAX_BITS`.
    """
    # Only departures *above* the base rate are leans. Divergence is symmetric,
    # so a 58% double chance scored well against a 70% base rate — but that is
    # a statement that the outcome is *less* likely than usual, and presenting
    # it as a strong conclusion inverts its meaning.
    if probability <= baseline:
        return 0.0

    probability = min(max(probability, 1e-9), 1 - 1e-9)
    baseline = min(max(baseline, 1e-9), 1 - 1e-9)

    bits = probability * math.log2(probability / baseline) + (1 - probability) * math.log2(
        (1 - probability) / (1 - baseline)
    )
    return min(max(bits, 0.0) / MAX_BITS, 1.0)


def base_rate_for(market: str, outcome: str) -> float:
    """Return how often an outcome happens, falling back to its market."""
    return BASE_RATES.get(outcome, MARKET_BASELINES.get(market, 0.5))


def _stability(component_count: int) -> float:
    """Score model agreement by how many components contributed.

    A proxy: the ensemble already drops components that lack data, so a fixture
    surviving with all three has more independent support than one relying on a
    single model.
    """
    return min(component_count / 3.0, 1.0)


def _data_quality(home_matches: int, away_matches: int) -> float:
    """Score the depth of history behind both sides.

    The weaker side governs: a fixture is only as well understood as its
    lesser-known team.
    """
    weakest = min(home_matches, away_matches)
    return min(weakest / MATCHES_FOR_FULL_QUALITY, 1.0)


def _market_agreement(probability: float, market_probability: float | None) -> float:
    """Score how closely the market concurs.

    Agreement scores high. That is the opposite of value betting and is
    intentional: with no demonstrated edge, a large disagreement is more likely
    to be our error than our insight, and should lower confidence rather than
    raise it.

    Returns a neutral 0.5 when no market price exists, so a fixture is neither
    rewarded nor punished for the absence.
    """
    if market_probability is None:
        return 0.5
    return max(0.0, 1.0 - abs(probability - market_probability) * 2.5)


def _reason(
    outcome: str,
    probability: float,
    market_probability: float | None,
    components: int,
    matches: int,
    baseline: float,
) -> str:
    """Explain the lean in one plain sentence.

    Careful about attribution. With no promoted model the published probability
    *is* the market prior — our models computed a view and were given no weight
    to move it. Writing "our models put this at 77%" would claim credit for a
    number the bookmakers produced.
    """
    lift = probability / baseline if baseline else 1.0
    parts = [
        f"{outcome} at {probability * 100:.0f}%, against {baseline * 100:.0f}% "
        f"in a typical match ({lift:.1f}x)"
    ]

    if market_probability is not None and abs(probability - market_probability) < 0.02:
        parts.append(
            "this reflects market prices, which our models agreed with rather " "than moved"
        )
    elif market_probability is not None:
        gap = probability - market_probability
        direction = "above" if gap > 0 else "below"
        parts.append(
            f"{abs(gap) * 100:.0f} points {direction} the market's "
            f"{market_probability * 100:.0f}%"
        )

    parts.append(f"backed by {components} model(s) and {matches} matches of history")
    return "; ".join(parts) + "."


@dataclass
class LeanBuilder:
    """Builds and ranks leans from stored analyses."""

    min_probability: float = MIN_PROBABILITY
    weights: dict[str, float] = field(default_factory=lambda: dict(WEIGHTS))

    def for_record(self, record: object) -> Lean | None:
        """Return the strongest lean for one stored analysis, if any.

        Returns ``None`` when nothing crosses the threshold, which is the
        honest outcome for an evenly balanced fixture — there is no conclusion
        worth stating.
        """
        markets = getattr(record, "markets", None) or {}
        if not isinstance(markets, dict) or not markets:
            return None

        coverage = str(getattr(record, "coverage", "unsupported"))
        coverage_score = COVERAGE_SCORES.get(coverage, 0.0)
        components = len(getattr(record, "components_used", []) or [])
        home_stats = getattr(record, "home_stats", {}) or {}
        away_stats = getattr(record, "away_stats", {}) or {}
        matches = min(int(home_stats.get("matches", 0)), int(away_stats.get("matches", 0)))
        market_view = getattr(record, "market_probabilities", None) or {}

        best: Lean | None = None
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
                if probability < self.min_probability:
                    continue

                market_probability = _market_for(market_name, outcome, market_view)
                confidence = _confidence(probability, base_rate_for(market_name, outcome))
                if confidence <= 0.0:
                    # No informational content: the outcome is no likelier than
                    # it is in any match. Coverage and data depth alone must not
                    # promote it — a well-evidenced statement of the obvious is
                    # still a statement of the obvious.
                    continue
                stability = _stability(components)
                quality = _data_quality(matches, matches)
                agreement = _market_agreement(probability, market_probability)

                score = (
                    self.weights["confidence"] * confidence
                    + self.weights["coverage"] * coverage_score
                    + self.weights["data_quality"] * quality
                    + self.weights["stability"] * stability
                    + self.weights["market_agreement"] * agreement
                )

                candidate = Lean(
                    fixture_id=str(getattr(record, "provider_event_id", "")),
                    home_name=str(getattr(record, "home_name", "")),
                    away_name=str(getattr(record, "away_name", "")),
                    competition=getattr(record, "competition", None),
                    kickoff=getattr(record, "kickoff", None),
                    market=market_name,
                    outcome=outcome,
                    probability=probability,
                    score=score,
                    confidence=confidence,
                    coverage_score=coverage_score,
                    data_quality=quality,
                    stability=stability,
                    market_agreement=agreement,
                    coverage=coverage,
                    reason=_reason(
                        outcome,
                        probability,
                        market_probability,
                        components,
                        matches,
                        base_rate_for(market_name, outcome),
                    ),
                )
                if best is None or candidate.score > best.score:
                    best = candidate

        return best

    def rank(self, records: list[object], limit: int = 5) -> list[Lean]:
        """Return the strongest leans, spread across markets.

        Strict score order would let one market fill the list on any day its
        numbers happen to run high, which stops the screen describing the card.
        At most :data:`MAX_PER_MARKET` leans come from any one market, unless
        there are too few fixtures to fill the list otherwise.
        """
        leans = [lean for record in records if (lean := self.for_record(record))]
        leans.sort(key=lambda lean: lean.score, reverse=True)

        chosen: list[Lean] = []
        per_market: dict[str, int] = {}
        overflow: list[Lean] = []

        for lean in leans:
            if per_market.get(lean.market, 0) < MAX_PER_MARKET:
                chosen.append(lean)
                per_market[lean.market] = per_market.get(lean.market, 0) + 1
            else:
                overflow.append(lean)
            if len(chosen) >= limit:
                return chosen

        # A thin card is better filled than left short.
        chosen.extend(overflow[: limit - len(chosen)])
        return chosen


def _market_for(market_name: str, outcome: str, market_view: dict[str, object]) -> float | None:
    """Return the market's probability for a 1X2 outcome, if comparable.

    Only 1X2 has a directly comparable market probability stored. Double chance
    could be derived by summing, but the derived markets are not quoted by the
    provider, so claiming a market view for them would be inventing one.
    """
    if market_name != "1X2":
        return None
    key = {"Home": "home", "Draw": "draw", "Away": "away"}.get(outcome)
    if key is None:
        return None
    raw = market_view.get(key)
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def probability_of(lean: Lean) -> Decimal:
    """Return a lean's probability as a Decimal."""
    return Decimal(str(lean.probability))
