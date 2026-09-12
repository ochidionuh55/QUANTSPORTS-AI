"""Best of the Day: model-only selection, ranked deterministically.

Each service asks one question of every fixture we can model — which is the
strongest home win today, the strongest over 1.5, the strongest "home or over
2.5" — and answers it from our own mathematics alone. No bookmaker price enters
the probability or the ranking. A fixture with no odds at all is still eligible,
which is the point: the engine must be able to reach a conclusion the market has
not already reached for it.

**Probability is not the same as strength.** A 78% figure computed from eleven
matches of history, with the three components disagreeing, is a worse selection
than a 70% figure from four hundred matches where they agree. Ranking by raw
probability alone would systematically promote the fixtures we know least about,
because thin samples produce extreme estimates. The score below therefore
combines probability with the evidence behind it.

**Everything here is deterministic.** The same fixtures produce the same
selection every time, and the reason is recorded as numbers rather than
narrative. Nothing is promoted for being a famous club, and nothing is
suppressed for being obscure.

**No claim of edge.** Backtesting found the models behind these numbers do not
beat bookmaker prices on any market where a price exists to compare against.
These services rank what our mathematics believes; the track record, collected
live over time, is what will decide whether any of them is worth following.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Final

from app.quant.markets import MARKETS, derive_markets

Grid = dict[tuple[int, int], Decimal]


@dataclass(frozen=True)
class ServiceDefinition:
    """One daily service."""

    key: str
    label: str
    market: str
    outcome: str
    min_probability: float
    """The bar below which nothing qualifies.

    Set per service rather than globally: 55% is a strong home win and an
    unremarkable over 1.5, so one threshold across all markets would fill the
    goals services with noise and starve the result services.
    """


SERVICES: Final[tuple[ServiceDefinition, ...]] = (
    # Result markets.
    ServiceDefinition("home", "🏆 Best Home Win", "1X2", "Home", 0.55),
    ServiceDefinition("away", "✈️ Best Away Win", "1X2", "Away", 0.45),
    ServiceDefinition("draw", "🤝 Best Draw", "1X2", "Draw", 0.30),
    ServiceDefinition(
        "home_draw", "🛡️ Best Home or Draw", "Double chance", "1X (home or draw)", 0.72
    ),
    ServiceDefinition(
        "away_draw", "🛡️ Best Away or Draw", "Double chance", "X2 (draw or away)", 0.65
    ),
    # Goals markets.
    ServiceDefinition("over_15", "⚽ Best Over 1.5", "Goals", "Over 1.5", 0.80),
    ServiceDefinition("under_25", "⚽ Best Under 2.5", "Goals", "Under 2.5", 0.58),
    ServiceDefinition("under_35", "🧱 Best Under 3.5", "Goals", "Under 3.5", 0.78),
    ServiceDefinition("btts", "🔥 Best BTTS", "Both teams to score", "Yes", 0.62),
    ServiceDefinition("no_btts", "❌ Best No BTTS", "Both teams to score", "No", 0.58),
    # Combination markets, exact from the joint distribution.
    ServiceDefinition(
        "home_or_over", "🔥 Best Home or Over 2.5", "Result or goals", "Home or Over 2.5", 0.78
    ),
    ServiceDefinition(
        "home_or_under", "🔥 Best Home or Under 2.5", "Result or goals", "Home or Under 2.5", 0.80
    ),
    ServiceDefinition(
        "draw_or_over", "🔥 Best Draw or Over 2.5", "Result or goals", "Draw or Over 2.5", 0.75
    ),
    ServiceDefinition(
        "draw_or_under", "🔥 Best Draw or Under 2.5", "Result or goals", "Draw or Under 2.5", 0.62
    ),
    ServiceDefinition(
        "away_or_over", "🔥 Best Away or Over 2.5", "Result or goals", "Away or Over 2.5", 0.72
    ),
    ServiceDefinition(
        "away_or_under", "🔥 Best Away or Under 2.5", "Result or goals", "Away or Under 2.5", 0.72
    ),
    ServiceDefinition(
        "home_or_btts", "🔥 Best Home or BTTS", "Result or BTTS", "Home or BTTS", 0.80
    ),
    ServiceDefinition(
        "draw_or_btts", "🔥 Best Draw or BTTS", "Result or BTTS", "Draw or BTTS", 0.65
    ),
    ServiceDefinition(
        "away_or_btts", "🔥 Best Away or BTTS", "Result or BTTS", "Away or BTTS", 0.72
    ),
    ServiceDefinition(
        "home_or_cs",
        "🔥 Best Home or Clean Sheet",
        "Result or clean sheet",
        "Home or clean sheet",
        0.75,
    ),
)

SERVICES_BY_KEY: Final[dict[str, ServiceDefinition]] = {s.key: s for s in SERVICES}

MODEL_ONLY_VERSION: Final[str] = "model-only-v1"
"""Version recorded against published selections.

Deliberately distinct from the ensemble version used elsewhere, which names
the market prior. A selection produced without any bookmaker input must not
carry a version string implying one was used.
"""

MIN_SAMPLE: Final[int] = 15
"""Matches each side needs before a fixture is eligible at all.

Below this the estimates are driven by noise, and noise produces extreme
probabilities — exactly the fixtures a probability ranking would promote.
"""

FULL_SAMPLE: Final[int] = 60
"""Sample size at which the evidence term stops improving."""


@dataclass(frozen=True)
class ModelForecast:
    """One fixture as the model-only engine sees it."""

    fixture_id: str
    home_name: str
    away_name: str
    competition: str | None
    kickoff: datetime
    grid: Grid
    """Joint scoreline distribution. Every market derives from this."""

    components: tuple[str, ...]
    """Which models contributed."""

    component_results: tuple[dict[str, Decimal], ...] = ()
    """Each component's own home/draw/away view, for measuring agreement."""

    home_matches: int = 0
    away_matches: int = 0
    coverage: str = "fully_modelled"

    @property
    def sample(self) -> int:
        """The smaller of the two sides' histories."""
        return min(self.home_matches, self.away_matches)


@dataclass
class Selection:
    """One service's answer for the day."""

    service: ServiceDefinition
    forecast: ModelForecast
    probability: float
    score: float
    factors: dict[str, float] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        """Explain, in numbers, why this fixture ranked first."""
        return (
            f"{self.probability:.0%} from {len(self.forecast.components)} model(s) "
            f"agreeing at {self.factors.get('agreement', 0):.0%}, on "
            f"{self.forecast.sample} matches of history for the thinner side."
        )


def _agreement(forecast: ModelForecast) -> float:
    """How closely the independent components agree on the result.

    Disagreement is the honest signal that a fixture is hard to call. Two models
    reaching 70% by different routes is far stronger evidence than one model
    reaching 70% while another says 45%, yet averaging hides the difference
    entirely — so it is measured and scored separately.
    """
    views = forecast.component_results
    if len(views) < 2:
        return 0.5

    spread = 0.0
    for key in ("home", "draw", "away"):
        values = [float(view.get(key, Decimal(0))) for view in views]
        spread = max(spread, max(values) - min(values))

    # A 40-point spread on any outcome is treated as total disagreement.
    return max(0.0, 1.0 - spread / 0.4)


def _evidence(forecast: ModelForecast) -> float:
    """How much history stands behind the estimate."""
    if forecast.sample <= MIN_SAMPLE:
        return 0.0
    return min(1.0, (forecast.sample - MIN_SAMPLE) / (FULL_SAMPLE - MIN_SAMPLE))


def _coverage_score(forecast: ModelForecast) -> float:
    """Reward fixtures where every model ran."""
    return {
        "fully_modelled": 1.0,
        "partially_modelled": 0.6,
        "data_only": 0.2,
    }.get(forecast.coverage, 0.0)


def _margin(probability: float, threshold: float) -> float:
    """How far past the bar the estimate sits.

    Scaled against the room remaining above the threshold, so clearing a 30%
    draw bar by ten points counts as much as clearing an 80% over 1.5 bar by
    two — the services stay comparable despite different natural ranges.
    """
    headroom = 1.0 - threshold
    if headroom <= 0:
        return 0.0
    return min(1.0, max(0.0, (probability - threshold) / headroom))


# Weights for the ranking. Probability carries the most, but not enough to let a
# thin, contradicted fixture outrank a well-evidenced one.
WEIGHT_MARGIN: Final[float] = 0.40
WEIGHT_AGREEMENT: Final[float] = 0.25
WEIGHT_EVIDENCE: Final[float] = 0.20
WEIGHT_COVERAGE: Final[float] = 0.15


def score_selection(
    forecast: ModelForecast, service: ServiceDefinition, probability: float
) -> tuple[float, dict[str, float]]:
    """Return a fixture's strength for one service, and the terms behind it."""
    factors = {
        "probability": round(probability, 4),
        "margin": round(_margin(probability, service.min_probability), 4),
        "agreement": round(_agreement(forecast), 4),
        "evidence": round(_evidence(forecast), 4),
        "coverage": round(_coverage_score(forecast), 4),
    }
    score = (
        WEIGHT_MARGIN * factors["margin"]
        + WEIGHT_AGREEMENT * factors["agreement"]
        + WEIGHT_EVIDENCE * factors["evidence"]
        + WEIGHT_COVERAGE * factors["coverage"]
    )
    factors["score"] = round(score, 4)
    return score, factors


class BestOfDayEngine:
    """Ranks every modelled fixture for every service."""

    def __init__(self, services: tuple[ServiceDefinition, ...] = SERVICES) -> None:
        self._services = services

    def rank(self, forecasts: list[ModelForecast], limit: int = 3) -> dict[str, list[Selection]]:
        """Return the strongest qualifying fixtures for each service.

        A service with nothing above its bar returns an empty list. That is a
        real answer and the product must show it as one.
        """
        ranked: dict[str, list[Selection]] = {}
        eligible = [f for f in forecasts if f.sample >= MIN_SAMPLE]

        for service in self._services:
            candidates: list[Selection] = []
            for forecast in eligible:
                probability = self._probability(forecast, service)
                if probability is None or probability < service.min_probability:
                    continue
                score, factors = score_selection(forecast, service, probability)
                candidates.append(
                    Selection(
                        service=service,
                        forecast=forecast,
                        probability=probability,
                        score=score,
                        factors=factors,
                    )
                )

            candidates.sort(key=lambda s: (-s.score, s.forecast.fixture_id))
            ranked[service.key] = candidates[:limit]
        return ranked

    def best(self, forecasts: list[ModelForecast]) -> dict[str, Selection | None]:
        """Return one selection per service, or ``None`` where none qualified."""
        ranked = self.rank(forecasts, limit=1)
        return {key: (entries[0] if entries else None) for key, entries in ranked.items()}

    def _probability(self, forecast: ModelForecast, service: ServiceDefinition) -> float | None:
        """Read one market off the fixture's joint distribution."""
        markets = derive_markets(forecast.grid)
        outcomes = markets.get(service.market)
        if not outcomes or service.outcome not in outcomes:
            return None
        return float(outcomes[service.outcome])


def settles(service_key: str, home_goals: int, away_goals: int) -> bool | None:
    """Whether a finished scoreline wins a service's selection.

    Resolved through the same predicate that produced the probability, so a
    service cannot be scored by one rule and settled by another.
    """
    service = SERVICES_BY_KEY.get(service_key)
    if service is None:
        return None
    for definition in MARKETS:
        if definition.key == (service.market, service.outcome):
            return definition.holds(home_goals, away_goals)
    return None
