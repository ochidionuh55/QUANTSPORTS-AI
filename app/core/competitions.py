"""Supported competitions.

One registry, used by the historical parser, the live provider and the bot, so
a competition cannot be analysable in one place and invisible in another.

``api_football_id`` is the vendor's numeric league id. A competition with
history but no id still backfills and backtests; it simply produces no live
fixtures. That asymmetry is deliberate and visible rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Competition:
    """A league we hold historical data for."""

    code: str
    name: str
    country: str
    api_football_id: int | None = None

    @property
    def has_live_fixtures(self) -> bool:
        """Whether live fixtures can be fetched for this competition."""
        return self.api_football_id is not None


COMPETITIONS: Final[tuple[Competition, ...]] = (
    # England
    Competition("E0", "Premier League", "England", 39),
    Competition("E1", "Championship", "England", 40),
    Competition("E2", "League One", "England", 41),
    Competition("E3", "League Two", "England", 42),
    Competition("EC", "National League", "England", 43),
    # Scotland
    Competition("SC0", "Scottish Premiership", "Scotland", 179),
    Competition("SC1", "Scottish Championship", "Scotland", 180),
    Competition("SC2", "Scottish League One", "Scotland", 183),
    Competition("SC3", "Scottish League Two", "Scotland", 184),
    # Western Europe
    Competition("D1", "Bundesliga", "Germany", 78),
    Competition("D2", "2. Bundesliga", "Germany", 79),
    Competition("SP1", "La Liga", "Spain", 140),
    Competition("SP2", "Segunda Division", "Spain", 141),
    Competition("I1", "Serie A", "Italy", 135),
    Competition("I2", "Serie B", "Italy", 136),
    Competition("F1", "Ligue 1", "France", 61),
    Competition("F2", "Ligue 2", "France", 62),
    Competition("N1", "Eredivisie", "Netherlands", 88),
    Competition("B1", "Belgian Pro League", "Belgium", 144),
    Competition("P1", "Primeira Liga", "Portugal", 94),
    Competition("SUI", "Swiss Super League", "Switzerland", 207),
    Competition("AUT", "Austrian Bundesliga", "Austria", 218),
    # Nordics
    Competition("DEN", "Danish Superliga", "Denmark", 119),
    Competition("SWE", "Allsvenskan", "Sweden", 113),
    Competition("NOR", "Eliteserien", "Norway", 103),
    Competition("FIN", "Veikkausliiga", "Finland", 244),
    Competition("IRL", "League of Ireland", "Ireland", 357),
    # Central and eastern Europe
    Competition("POL", "Ekstraklasa", "Poland", 106),
    Competition("ROM", "Liga I", "Romania", 283),
    Competition("RUS", "Russian Premier League", "Russia", 235),
    Competition("T1", "Super Lig", "Turkey", 203),
    Competition("G1", "Super League", "Greece", 197),
    # Rest of the world
    Competition("USA", "Major League Soccer", "USA", 253),
    Competition("MEX", "Liga MX", "Mexico", 262),
    Competition("BRA", "Brasileirao", "Brazil", 71),
    Competition("ARG", "Liga Profesional", "Argentina", 128),
    Competition("JAP", "J1 League", "Japan", 98),
    Competition("CHN", "Chinese Super League", "China", 169),
)

BY_CODE: Final[dict[str, Competition]] = {c.code: c for c in COMPETITIONS}

LEAGUE_IDS: Final[dict[str, int]] = {
    c.code: c.api_football_id for c in COMPETITIONS if c.api_football_id is not None
}

API_FOOTBALL_IDS: Final[frozenset[int]] = frozenset(LEAGUE_IDS.values())

# Legacy shape used by the CSV parser and scripts: code -> (name, country).
CSV_COMPETITIONS: Final[dict[str, tuple[str, str]]] = {
    c.code: (c.name, c.country) for c in COMPETITIONS
}


def by_api_football_id(league_id: int) -> Competition | None:
    """Return the competition for a vendor league id, if supported."""
    for competition in COMPETITIONS:
        if competition.api_football_id == league_id:
            return competition
    return None
