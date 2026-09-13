"""Basketball competitions, and what we are entitled to say about each.

**A league is not supported until it has been measured.** The model's structure
transfers between competitions; its numbers do not. An NBA game totals around
225 points and a WNBA game around 160, so pointing NBA parameters at a WNBA
fixture would not be slightly wrong — it would be confidently wrong, which is
the failure this whole system exists to prevent.

Each competition therefore carries its own fitted parameters and a readiness
state. Adding a league is a data task, not a code change: ingest its history,
fit its parameters, measure its calibration, then flip it to ``VALIDATED``. A
competition sitting in ``PLANNED`` produces nothing and says why.

**Defaults are deliberately absent.** There is no fallback parameter set,
because a fallback is how an unmeasured league quietly starts producing
numbers that look as confident as a measured one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class Readiness(str, Enum):
    """How far a competition has got towards being publishable."""

    VALIDATED = "validated"
    """History ingested, parameters fitted, calibration measured. Publishes."""

    FITTING = "fitting"
    """History ingested and parameters fitted, calibration not yet measured.

    Visible internally, never published: a fitted model that has not been
    tested out of sample is a hypothesis.
    """

    INGESTING = "ingesting"
    """History being collected. No parameters yet."""

    PLANNED = "planned"
    """Identified, with no usable historical data yet."""


@dataclass(frozen=True)
class BasketballCompetition:
    """One competition and its measured behaviour."""

    key: str
    name: str
    country: str
    readiness: Readiness

    margin_sigma: float | None = None
    """Spread of the margin around its expectation, in points."""

    total_sigma: float | None = None
    home_advantage: float | None = None
    typical_total: float | None = None
    """Roughly what a game in this league totals, as a sanity check.

    A fitted total far from this is a sign the data has been misread — wrong
    league, wrong units, or quarters mistaken for finals.
    """

    games_ingested: int = 0
    season_months: str = ""
    data_note: str = ""

    @property
    def publishable(self) -> bool:
        """Whether selections may be published for this competition."""
        return (
            self.readiness is Readiness.VALIDATED
            and self.margin_sigma is not None
            and self.total_sigma is not None
            and self.home_advantage is not None
        )

    @property
    def status_line(self) -> str:
        """A plain description of where this competition stands."""
        if self.readiness is Readiness.VALIDATED:
            return f"Live · {self.games_ingested:,} games measured"
        if self.readiness is Readiness.FITTING:
            return (
                f"Parameters fitted from {self.games_ingested:,} games, "
                "calibration not yet measured"
            )
        if self.readiness is Readiness.INGESTING:
            return f"Collecting history · {self.games_ingested:,} games so far"
        return self.data_note or "No usable historical data yet"


BASKETBALL_COMPETITIONS: Final[dict[str, BasketballCompetition]] = {
    "NBA": BasketballCompetition(
        key="NBA",
        name="NBA",
        country="United States",
        readiness=Readiness.VALIDATED,
        margin_sigma=13.04,
        total_sigma=18.66,
        home_advantage=2.4,
        typical_total=225.0,
        games_ingested=19164,
        season_months="October to June",
        data_note=(
            "Fitted on 2011-2022 and tested on 2022-2026. Calibrated; no edge "
            "over closing prices."
        ),
    ),
    "NBL": BasketballCompetition(
        key="NBL",
        name="NBL",
        country="Australia",
        readiness=Readiness.PLANNED,
        typical_total=170.0,
        season_months="September to February",
        data_note=(
            "Season runs September to February. No free historical dataset "
            "with closing prices found; scores are available and would allow "
            "calibration but not a market comparison."
        ),
    ),
    "WNBA": BasketballCompetition(
        key="WNBA",
        name="WNBA",
        country="United States",
        readiness=Readiness.PLANNED,
        typical_total=160.0,
        season_months="May to October",
        data_note=(
            "Scores are freely available; closing prices are not. Totals run "
            "far below the NBA, so NBA parameters would be badly wrong here."
        ),
    ),
    "EUROLEAGUE": BasketballCompetition(
        key="EUROLEAGUE",
        name="EuroLeague",
        country="Europe",
        readiness=Readiness.PLANNED,
        typical_total=160.0,
        season_months="October to May",
        data_note=(
            "Scores available through public archives; no free odds history "
            "found. Slower pace and lower totals than the NBA."
        ),
    ),
    "FIBA_WC": BasketballCompetition(
        key="FIBA_WC",
        name="FIBA World Cup",
        country="International",
        readiness=Readiness.PLANNED,
        typical_total=160.0,
        season_months="Tournament",
        data_note=(
            "Short tournaments give too few games per side to build ratings "
            "from. National teams also change squads between cycles, so old "
            "results say little about current strength."
        ),
    ),
}


def publishable_competitions() -> tuple[BasketballCompetition, ...]:
    """Return the competitions permitted to publish selections."""
    return tuple(c for c in BASKETBALL_COMPETITIONS.values() if c.publishable)


def competition(key: str) -> BasketballCompetition | None:
    """Return one competition by key."""
    return BASKETBALL_COMPETITIONS.get(key.upper())


def parameters_for(key: str) -> tuple[float, float, float] | None:
    """Return a competition's fitted parameters, or ``None`` if unmeasured.

    Returning ``None`` rather than a default is the point: a caller must decide
    what to do about an unmeasured league, and cannot accidentally inherit
    another competition's numbers.
    """
    found = competition(key)
    if found is None or not found.publishable:
        return None
    assert found.margin_sigma is not None
    assert found.total_sigma is not None
    assert found.home_advantage is not None
    return (found.margin_sigma, found.total_sigma, found.home_advantage)
