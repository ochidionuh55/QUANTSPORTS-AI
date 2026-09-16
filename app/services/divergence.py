"""The Outsider Board: where our model rates an underdog far above its price.

**What changed and why.** This started as a general model-market disagreement
report, and it surfaced the wrong thing. A nine-point gap on a side already
priced at 1.45 is a disagreement about a favourite: the market says 69%, we say
78%, and the reader learns nothing they could not have guessed from the price.
Worse, it filled a feature promising unusual conclusions with the most ordinary
picks on the card.

The disagreement that carries information is the one where the market makes
something an outsider and our mathematics does not. That is now the only kind
this module reports: the market must price the outcome *against*, and we must
make it materially likelier than that.

**This is still not a value signal, and the distinction still matters.**
A value signal says the price is wrong and therefore worth taking. Making that
claim requires having shown the models beat closing prices. We tested exactly
that - across nine seasons and 14,097 football forecasts, again with
Dixon-Coles, again on 13,903 NBA games - and measured skill at +0.000%. Where
our models disagreed with the price, the price was right.

So this reports a disagreement about an underdog and says nothing about who is
correct. Filtering to outsiders makes the report more *interesting*; it does
not make it more *right*, and a run of winners at long odds is the variance
those odds describe rather than evidence the filter works. The published record
is what will decide that, over a sample far larger than a good weekend.

**Presented with the market's own number beside ours.** A divergence shown
without the price it diverges from invites the reader to assume we think we are
right. Showing both leaves the judgement where it belongs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from app.core.logging import get_logger
from app.database.models import StoredAnalysis

logger = get_logger(__name__)

BOARD_LABEL: Final[str] = "🎲 Outsider Board"
"""What this is called in the product.

"Divergence" described the mechanism, not the reader's question. "Outsider"
names what is actually on the board - sides the market prices against - and,
unlike "value" or "underpriced", claims nothing about whether the price is
wrong, which we have not shown and cannot say.
"""

BOARD_DESCRIPTION: Final[str] = (
    "Outcomes the market prices as unlikely that our mathematics rates far "
    "higher. These are disagreements, not value calls - the price may well be "
    "right, and long odds are long for a reason."
)

MAX_MARKET_PROBABILITY: Final[float] = 0.45
"""Ceiling on the market's estimate - the constraint that defines this board.

Roughly 2.20 in decimal odds. Above this the market already makes the outcome
likely or near-even, and our agreeing more strongly is not an upset, it is a
rounding difference on a favourite. This single bound is what stops a 1.45
home side appearing on a board whose entire promise is that it will not show
you those.
"""

MIN_RATIO: Final[float] = 1.25
"""How many times likelier we must make it than the market does.

A gap of eight points means something different at 15% than at 40%. Requiring
a relative margin as well as an absolute one keeps the board's entries
comparably surprising across the price range.
"""

MIN_DIVERGENCE: Final[float] = 0.08
"""How far apart the two must be before it is worth reporting.

Eight points. Below that the gap is within the noise of both estimates, and
reporting it would manufacture significance from rounding.
"""

MIN_MODEL_PROBABILITY: Final[float] = 0.25
"""Floor on our own estimate.

A market at 4% and a model at 12% is a large relative disagreement about
something neither expects to happen. Reporting it as a finding would be
technically true and practically useless.
"""

MIN_SAMPLE: Final[int] = 20
"""Matches behind the thinner side.

Disagreement from a thin sample is not our model seeing something the market
missed — it is our model not knowing enough yet.
"""

RESULT_KEYS: Final[dict[str, str]] = {
    "home": "Home",
    "draw": "Draw",
    "away": "Away",
}


@dataclass(frozen=True)
class Divergence:
    """One fixture where our estimate parts company with the price."""

    fixture_id: str
    home_name: str
    away_name: str
    competition: str | None
    kickoff: datetime
    outcome: str
    model_probability: float
    market_probability: float
    coverage: str
    sample: int

    @property
    def gap(self) -> float:
        """How much likelier we think it is than the market does."""
        return self.model_probability - self.market_probability

    @property
    def ratio(self) -> float:
        """How many times likelier we make it than the market does."""
        if self.market_probability <= 0:
            return 0.0
        return self.model_probability / self.market_probability

    @property
    def implied_odds_model(self) -> float:
        """Decimal odds our estimate corresponds to."""
        return 1 / self.model_probability if self.model_probability > 0 else 0.0

    @property
    def implied_odds_market(self) -> float:
        """Decimal odds the market's estimate corresponds to.

        Shown beside ours because a reader thinks in prices. A gap of twelve
        points means little; 3.40 against 2.60 is immediately legible.
        """
        return 1 / self.market_probability if self.market_probability > 0 else 0.0

    def describe(self) -> str:
        """State the disagreement without implying who is right."""
        return (
            f"We make {self.outcome} {self.model_probability * 100:.0f}%; "
            f"the market prices it at {self.market_probability * 100:.0f}%. "
            f"A {self.gap * 100:.0f}-point disagreement."
        )


def _devig(probabilities: dict[str, float]) -> dict[str, float]:
    """Remove the bookmaker's margin from a set of implied probabilities.

    Raw implied probabilities sum above one because the margin is baked in.
    Comparing our estimate against those unadjusted figures would manufacture
    a disagreement out of the bookmaker's commission.
    """
    total = sum(probabilities.values())
    if total <= 0:
        return probabilities
    return {key: value / total for key, value in probabilities.items()}


def _read(values: dict[str, object] | None, key: str) -> float | None:
    """Read one probability from a stored mapping."""
    if not isinstance(values, dict):
        return None
    raw = values.get(key)
    if raw is None:
        return None
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return None


def _sample(record: StoredAnalysis) -> int:
    """Return the smaller of the two sides' match counts."""
    counts: list[int] = []
    for stats in (record.home_stats or {}, record.away_stats or {}):
        raw = stats.get("matches", 0) if isinstance(stats, dict) else 0
        try:
            counts.append(int(str(raw)))
        except (TypeError, ValueError):
            counts.append(0)
    return min(counts) if counts else 0


def find_divergences(
    records: list[StoredAnalysis],
    limit: int = 10,
    now: datetime | None = None,
    max_market_probability: float = MAX_MARKET_PROBABILITY,
) -> list[Divergence]:
    """Return the outsiders our model rates furthest above their price.

    Only fixtures carrying both a model-only estimate and a real market price
    qualify. Without both there is nothing to compare, and inferring one from
    the other would produce a disagreement with itself.

    Args:
        records: Stored analyses to consider.
        limit: How many to return, one per fixture.
        now: Injected clock; fixtures already kicked off are excluded.
        max_market_probability: The ceiling that makes this an outsider board.
            Raise it to include shorter prices; the default deliberately
            excludes anything the market already makes likely.
    """
    moment = now or datetime.now(UTC)
    found: list[Divergence] = []

    for record in records:
        kickoff = record.kickoff
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=UTC)
        if kickoff <= moment:
            continue

        sample = _sample(record)
        if sample < MIN_SAMPLE:
            continue

        model_raw = {key: _read(record.model_probabilities, key) for key in RESULT_KEYS}
        market_raw = {key: _read(record.market_probabilities, key) for key in RESULT_KEYS}
        if any(value is None for value in model_raw.values()):
            continue
        if any(value is None for value in market_raw.values()):
            continue

        model = _devig({k: v for k, v in model_raw.items() if v is not None})
        market = _devig({k: v for k, v in market_raw.items() if v is not None})

        for key, label in RESULT_KEYS.items():
            model_probability = model.get(key, 0.0)
            market_probability = market.get(key, 0.0)

            if model_probability < MIN_MODEL_PROBABILITY:
                continue
            # The defining constraint: the market must price this against.
            # A favourite we like slightly more is not an outsider, and a
            # board promising unusual conclusions cannot be filled with the
            # shortest prices on the card.
            if market_probability > max_market_probability:
                continue
            if model_probability - market_probability < MIN_DIVERGENCE:
                continue
            if market_probability <= 0 or model_probability / market_probability < MIN_RATIO:
                continue

            found.append(
                Divergence(
                    fixture_id=record.provider_event_id,
                    home_name=record.home_name,
                    away_name=record.away_name,
                    competition=record.competition,
                    kickoff=kickoff,
                    outcome=label,
                    model_probability=model_probability,
                    market_probability=market_probability,
                    coverage=record.coverage,
                    sample=sample,
                )
            )

    found.sort(key=lambda item: (-item.gap, item.fixture_id))

    # One per fixture. The same match appearing three times under different
    # outcomes is one disagreement described three ways.
    seen: set[str] = set()
    unique: list[Divergence] = []
    for item in found:
        if item.fixture_id in seen:
            continue
        seen.add(item.fixture_id)
        unique.append(item)
        if len(unique) >= limit:
            break

    logger.info("divergence.found", count=len(unique), considered=len(records))
    return unique
