"""Historical match data providers.

Deliberately separate from the odds provider layer: historical data answers
what happened, not what is priced, and carries its own concerns around seasons,
provenance and record rejection.
"""

from app.historical.base import DatasetSource, HistoricalDataProvider
from app.historical.csv_provider import (
    CsvHistoricalDataProvider,
    InMemorySource,
    LocalDirectorySource,
    decode_csv,
    parse_match_date,
)
from app.historical.mock import MockHistoricalDataProvider
from app.historical.models import (
    DatasetNotFoundError,
    DatasetParseError,
    DatasetSchemaError,
    HistoricalCompetition,
    HistoricalDatasetMetadata,
    HistoricalDatasetSnapshot,
    HistoricalMatch,
    HistoricalProviderError,
    HistoricalSeason,
    HistoricalTeamRef,
    MatchResult,
    RejectedRecord,
    RejectionReason,
    content_hash,
)

__all__ = [
    "CsvHistoricalDataProvider",
    "DatasetNotFoundError",
    "DatasetParseError",
    "DatasetSchemaError",
    "DatasetSource",
    "HistoricalCompetition",
    "HistoricalDataProvider",
    "HistoricalDatasetMetadata",
    "HistoricalDatasetSnapshot",
    "HistoricalMatch",
    "HistoricalProviderError",
    "HistoricalSeason",
    "HistoricalTeamRef",
    "InMemorySource",
    "LocalDirectorySource",
    "MatchResult",
    "MockHistoricalDataProvider",
    "RejectedRecord",
    "RejectionReason",
    "content_hash",
    "decode_csv",
    "parse_match_date",
]
