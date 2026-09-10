"""Historical data DTOs, provenance and error model.

Kept separate from the odds provider layer. Historical data answers a different
question — what happened — and has different concerns: seasons, provenance,
reproducibility of an ingestion, and the fate of records that could not be
parsed.

**On provenance.** Free sources such as football-data.co.uk revise files in
place and keep no vintage history, so what a file said six months ago is not
recoverable. That limitation is accepted. What is *not* acceptable is being
unable to identify what we ourselves ingested, so every snapshot carries a
content hash, row counts, parser and schema versions, and timestamps. Source
truth may drift; our research snapshot stays identifiable.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HistoricalProviderError(Exception):
    """Base class for historical data failures."""


class DatasetNotFoundError(HistoricalProviderError):
    """The requested competition or season is not available."""


class DatasetParseError(HistoricalProviderError):
    """The dataset could not be read at all."""


class DatasetSchemaError(DatasetParseError):
    """The dataset is missing columns the parser requires."""


class RejectionReason(StrEnum):
    """Why an individual source record was not accepted.

    Rejected records are counted and retained rather than dropped. A silent
    drop turns a parser bug into a quietly smaller dataset, which shows up much
    later as a model that will not calibrate and cannot be explained.
    """

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_DATE = "invalid_date"
    INVALID_SCORE = "invalid_score"
    IMPOSSIBLE_SCORE = "impossible_score"
    MISSING_TEAM = "missing_team"
    SAME_TEAM_BOTH_SIDES = "same_team_both_sides"
    DUPLICATE_MATCH = "duplicate_match"
    UNKNOWN_COMPETITION = "unknown_competition"
    MALFORMED_ROW = "malformed_row"
    ENCODING_ERROR = "encoding_error"


MAX_PLAUSIBLE_GOALS = 30
"""Above this a score is treated as corrupt rather than remarkable.

The record men's professional margin is well under this; anything higher is a
parsing artefact, not a football result.
"""


class HistoricalDTO(BaseModel):
    """Base for historical DTOs: immutable and strict."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class HistoricalCompetition(HistoricalDTO):
    """A league or tournament available from a historical source."""

    provider_name: str
    external_id: str
    name: str
    country: str | None = None
    sport: str = "football"


class HistoricalSeason(HistoricalDTO):
    """One season of one competition."""

    provider_name: str
    competition_external_id: str
    season: str
    """Label as the source writes it, e.g. ``2023/2024``."""

    start_year: int
    end_year: int


class HistoricalTeamRef(HistoricalDTO):
    """A team as the source names it.

    Deliberately unresolved. Canonical identity is assigned downstream by the
    Phase 2 resolver, so that resolution logic lives in exactly one place
    rather than being reimplemented by every provider.
    """

    source_name: str
    external_id: str | None = None


class MatchResult(StrEnum):
    """Full-time result from the home side's perspective."""

    HOME = "H"
    DRAW = "D"
    AWAY = "A"


class HistoricalMatch(HistoricalDTO):
    """A completed match.

    Carries the minimum needed by Poisson, Elo, form and home/away strength.
    Deliberately not every available statistic: shots, corners and cards are
    cheap to add later and would be speculative to model now.
    """

    provider_name: str
    external_id: str
    competition_external_id: str
    season: str
    match_date: date
    kickoff: datetime | None = None
    home_team: HistoricalTeamRef
    away_team: HistoricalTeamRef
    home_goals: int = Field(ge=0)
    away_goals: int = Field(ge=0)
    half_time_home_goals: int | None = Field(default=None, ge=0)
    half_time_away_goals: int | None = Field(default=None, ge=0)

    closing_odds: dict[str, dict[str, Decimal]] = Field(default_factory=dict)
    """1X2 odds by bookmaker, e.g. ``{"pinnacle": {"H": 2.1, "D": 3.4, "A": 3.6}}``.

    Historical odds are what make a backtest meaningful. Without a market
    baseline, a Brier score of 0.20 is unfalsifiable — it could be excellent or
    dreadful, and there is no way to tell which. These are the prices the model
    must beat.
    """

    raw: dict[str, Any] = Field(default_factory=dict)

    def odds_for(self, bookmaker: str) -> tuple[Decimal, Decimal, Decimal] | None:
        """Return ``(home, draw, away)`` odds for a bookmaker, if quoted."""
        quoted = self.closing_odds.get(bookmaker)
        if not quoted or not all(k in quoted for k in ("H", "D", "A")):
            return None
        return quoted["H"], quoted["D"], quoted["A"]

    @field_validator("kickoff")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        """Reject naive datetimes rather than assuming UTC."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Kickoff must declare a timezone.")
        return value.astimezone(UTC)

    @property
    def result(self) -> MatchResult:
        """Full-time result from the home side's perspective."""
        if self.home_goals > self.away_goals:
            return MatchResult.HOME
        if self.home_goals < self.away_goals:
            return MatchResult.AWAY
        return MatchResult.DRAW

    @property
    def total_goals(self) -> int:
        """Combined goals, used by totals markets and Poisson fitting."""
        return self.home_goals + self.away_goals

    @property
    def both_teams_scored(self) -> bool:
        """Whether both sides scored."""
        return self.home_goals > 0 and self.away_goals > 0


class RejectedRecord(HistoricalDTO):
    """A source record that could not be accepted, and why."""

    reason: RejectionReason
    detail: str
    row_number: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class HistoricalDatasetMetadata(HistoricalDTO):
    """Provenance for one ingested dataset.

    ``content_hash`` is what makes an ingestion reproducible: re-reading the
    same source later either produces the same hash, or tells us the source
    changed underneath us.
    """

    provider_name: str
    source_name: str
    source_url: str | None = None
    competition_external_id: str
    season: str
    downloaded_at: datetime | None = None
    ingested_at: datetime
    content_hash: str
    row_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    parser_version: str
    schema_version: str

    @field_validator("ingested_at", "downloaded_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        """Reject naive datetimes."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Dataset timestamps must declare a timezone.")
        return value.astimezone(UTC)


class HistoricalDatasetSnapshot(HistoricalDTO):
    """Everything one ingestion produced: matches, rejects and provenance."""

    metadata: HistoricalDatasetMetadata
    matches: tuple[HistoricalMatch, ...]
    rejected: tuple[RejectedRecord, ...] = ()

    @property
    def acceptance_rate(self) -> float:
        """Fraction of source rows accepted.

        A sudden drop between ingestions is the earliest signal that a source's
        format has changed.
        """
        total = self.metadata.row_count
        return len(self.matches) / total if total else 0.0


def content_hash(payload: bytes) -> str:
    """Return a stable SHA-256 hex digest of raw dataset bytes."""
    return hashlib.sha256(payload).hexdigest()
