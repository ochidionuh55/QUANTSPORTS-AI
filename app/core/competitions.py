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
    # ── Wave 1 ────────────────────────────────────────────────────────────
    #
    # Added after the full onboarding pipeline: history audited and counted,
    # identities seeded across every ingested season, fixtures reconciled,
    # the model fitted per competition and validated walk-forward.
    #
    # Being listed here only puts a competition's fixtures into the scan. What
    # may be *published* from them is decided separately by
    # ``app.services.capability_gate``, which permits the 43 competition-and-
    # service pairs that passed validation and blocks the other 125. A new
    # competition here without a capability row publishes nothing.
    #
    # NPFL (399) and Liga Pro Serie B (243) are deliberately absent: NPFL for a
    # 9.4-point home-win bias, Ecuador for 686 validation forecasts against an
    # 800 minimum. Their history is ingested and their capabilities recorded,
    # but neither enters the scan.
    Competition(code="LEU", name="Liga Leumit", country="Israel", api_football_id=382),
    Competition(
        code="AZA", name="Azadegan League", country="Iran", api_football_id=291
    ),
    Competition(code="PRV", name="Prva Liga", country="Serbia", api_football_id=287),
    Competition(code="PRB", name="Primera B", country="Colombia", api_football_id=240),
    Competition(code="ALF", name="Liga Alef", country="Israel", api_football_id=496),
    Competition(
        code="BRS", name="1st League - RS", country="Bosnia", api_football_id=317
    ),
)

BY_CODE: Final[dict[str, Competition]] = {c.code: c for c in COMPETITIONS}

BY_NAME: Final[dict[str, Competition]] = {c.name.casefold(): c for c in COMPETITIONS}
"""Lookup by display name.

Analyses carry the competition's name, not its code, because that is what a
provider supplies and what a screen shows. Anything keyed by code — the fitted
Dixon-Coles parameters, for one — needs this to get back.
"""


BY_API_ID: Final[dict[int, Competition]] = {
    int(c.api_football_id): c for c in COMPETITIONS if c.api_football_id
}
"""Lookup by the provider's league id.

The reliable key. Display names are neither unique nor stable: API-Football
calls both Italy's and Brazil's top division "Serie A", so resolving by name
silently files Brazilian fixtures under an Italian competition — and the
mis-attribution is invisible, because a wrong answer looks exactly like a
right one.
"""


def code_for_api_id(external_id: object) -> str | None:
    """Return our competition code for a provider league id.

    Accepts the id in whatever form the provider supplied it. ``external_id``
    is a string on ``ProviderCompetition`` while ``api_football_id`` is an int,
    and comparing them unnormalised is always false — the mistake that made a
    discovery report list every configured competition as unknown.
    """
    if external_id is None:
        return None
    try:
        numeric = int(str(external_id).strip())
    except (TypeError, ValueError):
        return None
    found = BY_API_ID.get(numeric)
    return found.code if found else None


def code_for_name(name: str | None) -> str | None:
    """Return the competition code for a display name, if we know it.

    Returns ``None`` for an unknown name rather than guessing. A wrong code
    would silently select another league's fitted parameter, which is worse
    than falling back to the default.

    Prefer :func:`code_for_api_id` wherever the provider's league id is
    available: names collide across countries and this cannot tell them apart.
    """
    if not name:
        return None
    found = BY_NAME.get(name.casefold())
    return found.code if found else None

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
