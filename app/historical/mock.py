"""Deterministic mock historical data provider.

Generates a full season of results with no network access and no randomness.
Scorelines come from a hash of the fixture identity, so a given match always
produces the same result on every machine and every run — the property that
makes a backtest reproducible.

The dataset deliberately includes a newly promoted side with a short history
and a small number of unusable source records, so ingestion is exercised on
sparse-history handling and on rejection accounting rather than only on the
happy path.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Final

from app.historical.base import HistoricalDataProvider
from app.historical.models import (
    DatasetNotFoundError,
    HistoricalCompetition,
    HistoricalDatasetMetadata,
    HistoricalDatasetSnapshot,
    HistoricalMatch,
    HistoricalSeason,
    HistoricalTeamRef,
    RejectedRecord,
    RejectionReason,
    content_hash,
)

PARSER_VERSION: Final[str] = "mock-1.0"
SCHEMA_VERSION: Final[str] = "mock-1.0"

INGESTED_AT: Final[datetime] = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
"""Fixed ingestion timestamp so provenance is deterministic too."""

_COMPETITIONS: Final[tuple[tuple[str, str, str], ...]] = (
    ("E0", "Premier League", "England"),
    ("SP1", "La Liga", "Spain"),
)

_SEASONS: Final[tuple[str, ...]] = ("2023/2024", "2024/2025")

_TEAMS: Final[dict[str, tuple[str, ...]]] = {
    "E0": (
        "Arsenal",
        "Chelsea",
        "Liverpool",
        "Manchester United",
        "Manchester City",
        "Everton",
    ),
    "SP1": ("Real Madrid", "Barcelona", "Sevilla", "Valencia"),
}

PROMOTED_TEAM: Final[str] = "Luton Town"
"""Appears only in the later season, and only a few times.

Elo and form models must cope with a side that has almost no history rather
than silently treating it as average.
"""

PROMOTED_SEASON: Final[str] = "2024/2025"
PROMOTED_COMPETITION: Final[str] = "E0"


def _score(seed: str) -> int:
    """Return a repeatable goal count with a football-like distribution."""
    digest = hashlib.sha256(seed.encode()).hexdigest()
    value = int(digest[:6], 16) % 100
    # Roughly matches observed frequencies: 0-1 goals common, 4+ rare.
    if value < 26:
        return 0
    if value < 60:
        return 1
    if value < 82:
        return 2
    if value < 94:
        return 3
    return 4


class MockHistoricalDataProvider(HistoricalDataProvider):
    """In-memory historical provider producing deterministic results."""

    def __init__(self, name: str = "mock_historical") -> None:
        self._name = name

    @property
    def name(self) -> str:
        """Provider identifier."""
        return self._name

    @property
    def source_name(self) -> str:
        """Origin recorded in provenance."""
        return "mock"

    @property
    def parser_version(self) -> str:
        """Parser version."""
        return PARSER_VERSION

    @property
    def schema_version(self) -> str:
        """Schema version."""
        return SCHEMA_VERSION

    async def get_competitions(self) -> tuple[HistoricalCompetition, ...]:
        """Return the mock competitions."""
        return tuple(
            HistoricalCompetition(
                provider_name=self._name,
                external_id=external_id,
                name=name,
                country=country,
            )
            for external_id, name, country in _COMPETITIONS
        )

    async def get_seasons(self, competition_external_id: str) -> tuple[HistoricalSeason, ...]:
        """Return available seasons for a competition."""
        if competition_external_id not in _TEAMS:
            raise DatasetNotFoundError(f"Unknown competition '{competition_external_id}'.")
        seasons = []
        for label in _SEASONS:
            start, end = label.split("/")
            seasons.append(
                HistoricalSeason(
                    provider_name=self._name,
                    competition_external_id=competition_external_id,
                    season=label,
                    start_year=int(start),
                    end_year=int(end),
                )
            )
        return tuple(seasons)

    def _teams(self, competition: str, season: str) -> tuple[str, ...]:
        """Return the sides competing, including the promoted one when due."""
        teams = _TEAMS[competition]
        if competition == PROMOTED_COMPETITION and season == PROMOTED_SEASON:
            return (*teams, PROMOTED_TEAM)
        return teams

    async def load_dataset(
        self, competition_external_id: str, season: str
    ) -> HistoricalDatasetSnapshot:
        """Build a deterministic double round-robin for one season."""
        if competition_external_id not in _TEAMS:
            raise DatasetNotFoundError(f"Unknown competition '{competition_external_id}'.")
        if season not in _SEASONS:
            raise DatasetNotFoundError(
                f"Season '{season}' unavailable for '{competition_external_id}'."
            )

        teams = self._teams(competition_external_id, season)
        start_year = int(season.split("/")[0])
        kickoff_day = date(start_year, 8, 12)

        matches: list[HistoricalMatch] = []
        index = 0
        for home in teams:
            for away in teams:
                if home == away:
                    continue
                # The promoted side plays a short schedule, so downstream code
                # meets a team with genuinely sparse history.
                if PROMOTED_TEAM in (home, away) and index % 3 != 0:
                    index += 1
                    continue

                seed = f"{competition_external_id}:{season}:{home}:{away}"
                match_date = kickoff_day + timedelta(days=index * 3)
                matches.append(
                    HistoricalMatch(
                        provider_name=self._name,
                        external_id=f"{competition_external_id}-{season}-{index:04d}",
                        competition_external_id=competition_external_id,
                        season=season,
                        match_date=match_date,
                        kickoff=datetime(
                            match_date.year,
                            match_date.month,
                            match_date.day,
                            15,
                            0,
                            tzinfo=UTC,
                        ),
                        home_team=HistoricalTeamRef(source_name=home),
                        away_team=HistoricalTeamRef(source_name=away),
                        home_goals=_score(f"{seed}:H"),
                        away_goals=_score(f"{seed}:A"),
                    )
                )
                index += 1

        rejected = self._rejects(competition_external_id, season)
        row_count = len(matches) + len(rejected)
        digest = content_hash(f"{competition_external_id}:{season}:{row_count}".encode())

        return HistoricalDatasetSnapshot(
            metadata=HistoricalDatasetMetadata(
                provider_name=self._name,
                source_name=self.source_name,
                competition_external_id=competition_external_id,
                season=season,
                ingested_at=INGESTED_AT,
                content_hash=digest,
                row_count=row_count,
                accepted_count=len(matches),
                rejected_count=len(rejected),
                parser_version=PARSER_VERSION,
                schema_version=SCHEMA_VERSION,
            ),
            matches=tuple(matches),
            rejected=rejected,
        )

    @staticmethod
    def _rejects(competition: str, season: str) -> tuple[RejectedRecord, ...]:
        """Return the unusable rows every real dataset contains."""
        return (
            RejectedRecord(
                reason=RejectionReason.INVALID_DATE,
                detail="Date field was empty.",
                row_number=7,
                raw={"Div": competition, "Date": "", "HomeTeam": "Arsenal"},
            ),
            RejectedRecord(
                reason=RejectionReason.MISSING_TEAM,
                detail="AwayTeam field was empty.",
                row_number=19,
                raw={"Div": competition, "HomeTeam": "Chelsea", "AwayTeam": ""},
            ),
            RejectedRecord(
                reason=RejectionReason.DUPLICATE_MATCH,
                detail=f"Fixture already present in {season}.",
                row_number=31,
                raw={"Div": competition, "HomeTeam": "Liverpool"},
            ),
        )
