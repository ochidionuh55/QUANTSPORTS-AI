"""CSV historical data provider.

Reads football-data.co.uk-style datasets, but is not welded to that site. Raw
bytes arrive through a ``DatasetSource``, so the same parser serves a local
archive today and object storage or a vendor export later; only the source
implementation changes.

Every row is validated. Anything unusable becomes a ``RejectedRecord`` with a
reason and a row number, never a silent omission — a parser that quietly drops
10% of a season produces a model that will not calibrate and cannot be
explained.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from app.core.logging import get_logger
from app.historical.base import DatasetSource, HistoricalDataProvider
from app.historical.models import (
    MAX_PLAUSIBLE_GOALS,
    DatasetNotFoundError,
    DatasetSchemaError,
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

logger = get_logger(__name__)

PARSER_VERSION: Final[str] = "csv-1.0"
SCHEMA_VERSION: Final[str] = "football-data-uk-1.0"

REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {"Div", "Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"}
)

# Bookmaker odds columns, in preference order per book. football-data.co.uk
# publishes both pre-closing and closing prices; the closing price is the
# sharper number and the one worth measuring against, so it is preferred.
#
# Pinnacle is included deliberately. It is a low-margin book that welcomes
# winning bettors, so its closing line is close to a consensus fair price —
# which makes it the reference prior, while a soft book like Bet365 is the
# tradeable side. Both being present in one file means the reference/tradeable
# split works without a second data feed.
ODDS_COLUMNS: Final[dict[str, tuple[tuple[str, str, str], ...]]] = {
    "pinnacle": (("PSCH", "PSCD", "PSCA"), ("PSH", "PSD", "PSA")),
    "bet365": (("B365CH", "B365CD", "B365CA"), ("B365H", "B365D", "B365A")),
    "market_max": (("MaxCH", "MaxCD", "MaxCA"), ("BbMxH", "BbMxD", "BbMxA")),
    "market_avg": (("AvgCH", "AvgCD", "AvgCA"), ("BbAvH", "BbAvD", "BbAvA")),
}

REFERENCE_BOOKMAKER: Final[str] = "pinnacle"
TRADEABLE_BOOKMAKER: Final[str] = "bet365"

_DATE_FORMATS: Final[tuple[str, ...]] = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d")
"""Accepted date formats.

The source switched from two-digit to four-digit years partway through its
history, and both appear in the archive. Day-first throughout: parsing
``01/02/2024`` as January would silently shift a third of every season.
"""


class LocalDirectorySource(DatasetSource):
    """Reads datasets from a directory on disk."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def read(self, key: str) -> bytes:
        """Return raw bytes for a dataset file.

        Raises:
            DatasetNotFoundError: If the file does not exist.
        """
        path = self._root / key
        if not path.is_file():
            raise DatasetNotFoundError(f"No dataset file at '{key}'.")
        return path.read_bytes()

    async def list_keys(self) -> tuple[str, ...]:
        """Return every CSV file in the directory."""
        if not self._root.is_dir():
            return ()
        return tuple(sorted(p.name for p in self._root.glob("*.csv")))

    @property
    def origin(self) -> str | None:
        """Directory path, recorded in provenance."""
        return str(self._root)


class InMemorySource(DatasetSource):
    """Holds datasets in memory, for tests and fixtures."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)

    async def read(self, key: str) -> bytes:
        """Return raw bytes for a key.

        Raises:
            DatasetNotFoundError: If the key is absent.
        """
        try:
            return self._files[key]
        except KeyError:
            raise DatasetNotFoundError(f"No dataset for '{key}'.") from None

    async def list_keys(self) -> tuple[str, ...]:
        """Return every available key."""
        return tuple(sorted(self._files))


def decode_csv(payload: bytes) -> str:
    """Decode dataset bytes to text, reporting which encoding was used.

    The archive is inconsistently encoded — UTF-8 in places, Latin-1 in others,
    sometimes with a BOM. Club names carrying accents are where this bites: a
    mangled name silently fails team resolution rather than raising.

    This function cannot fail. Latin-1 maps every possible byte, so it always
    succeeds — which means a genuinely mis-encoded file yields mojibake rather
    than an error. The fallback is therefore logged at warning level, so the
    condition is visible in logs and detectable by the team-resolution review
    queue downstream, which is where a corrupted club name will surface.

    Returns:
        The decoded text.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue

    logger.warning(
        "historical.encoding_fallback",
        encoding="latin-1",
        detail=(
            "Payload is not valid UTF-8 or CP1252. Decoding as Latin-1, which "
            "always succeeds but may produce mojibake in team names."
        ),
    )
    return payload.decode("latin-1")


def parse_match_date(value: str) -> date:
    """Parse a source date.

    Raises:
        ValueError: If no accepted format matches.
    """
    text = value.strip()
    if not text:
        raise ValueError("empty date")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date '{text}'")


def _season_label(match_date: date) -> str:
    """Derive a season label from a date.

    European seasons straddle the calendar year; July is used as the boundary.
    """
    if match_date.month >= 7:
        return f"{match_date.year}/{match_date.year + 1}"
    return f"{match_date.year - 1}/{match_date.year}"


class CsvHistoricalDataProvider(HistoricalDataProvider):
    """Parses football-data.co.uk-style CSV datasets."""

    def __init__(
        self,
        source: DatasetSource,
        competitions: dict[str, tuple[str, str]],
        name: str = "csv_historical",
        source_name: str = "football-data-style-csv",
        source_url: str | None = None,
    ) -> None:
        """Create the provider.

        Args:
            source: Where raw bytes come from.
            competitions: Division code to ``(name, country)``.
            name: Provider identifier.
            source_name: Origin label for provenance.
            source_url: Origin URL for provenance, when known.
        """
        self._source = source
        self._competitions = competitions
        self._name = name
        self._source_name = source_name
        self._source_url = source_url

    @property
    def name(self) -> str:
        """Provider identifier."""
        return self._name

    @property
    def source_name(self) -> str:
        """Origin label."""
        return self._source_name

    @property
    def parser_version(self) -> str:
        """Parser version."""
        return PARSER_VERSION

    @property
    def schema_version(self) -> str:
        """Expected source schema version."""
        return SCHEMA_VERSION

    async def get_competitions(self) -> tuple[HistoricalCompetition, ...]:
        """Return the configured competitions."""
        return tuple(
            HistoricalCompetition(
                provider_name=self._name,
                external_id=code,
                name=name,
                country=country,
            )
            for code, (name, country) in sorted(self._competitions.items())
        )

    def _key(self, competition: str, season: str) -> str:
        """Return the dataset key for a competition-season."""
        return f"{competition}_{season.replace('/', '-')}.csv"

    async def get_seasons(self, competition_external_id: str) -> tuple[HistoricalSeason, ...]:
        """Return seasons discoverable from the available dataset keys."""
        if competition_external_id not in self._competitions:
            raise DatasetNotFoundError(f"Unknown competition '{competition_external_id}'.")
        seasons = []
        prefix = f"{competition_external_id}_"
        for key in await self._source.list_keys():
            if not key.startswith(prefix) or not key.endswith(".csv"):
                continue
            label = key[len(prefix) : -len(".csv")].replace("-", "/")
            try:
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
            except ValueError:
                logger.warning("historical.unparseable_key", key=key)
        return tuple(seasons)

    async def load_dataset(
        self, competition_external_id: str, season: str
    ) -> HistoricalDatasetSnapshot:
        """Parse one competition-season into matches, rejects and provenance."""
        if competition_external_id not in self._competitions:
            raise DatasetNotFoundError(f"Unknown competition '{competition_external_id}'.")

        payload = await self._source.read(self._key(competition_external_id, season))
        digest = content_hash(payload)
        text = decode_csv(payload)

        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise DatasetSchemaError("Dataset has no header row.")

        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise DatasetSchemaError(f"Dataset is missing required columns: {sorted(missing)}.")

        matches: list[HistoricalMatch] = []
        rejected: list[RejectedRecord] = []
        seen: set[tuple[str, str, str]] = set()
        row_count = 0

        for row_number, row in enumerate(reader, start=2):
            row_count += 1
            record = self._parse_row(row, row_number, competition_external_id, season, seen)
            if isinstance(record, RejectedRecord):
                rejected.append(record)
            else:
                matches.append(record)

        logger.info(
            "historical.dataset_parsed",
            provider=self._name,
            competition=competition_external_id,
            season=season,
            accepted=len(matches),
            rejected=len(rejected),
            content_hash=digest[:12],
        )

        return HistoricalDatasetSnapshot(
            metadata=HistoricalDatasetMetadata(
                provider_name=self._name,
                source_name=self._source_name,
                source_url=self._source_url,
                competition_external_id=competition_external_id,
                season=season,
                ingested_at=datetime.now(UTC),
                content_hash=digest,
                row_count=row_count,
                accepted_count=len(matches),
                rejected_count=len(rejected),
                parser_version=PARSER_VERSION,
                schema_version=SCHEMA_VERSION,
            ),
            matches=tuple(matches),
            rejected=tuple(rejected),
        )

    def _parse_row(
        self,
        row: dict[str, str | None],
        row_number: int,
        competition: str,
        season: str,
        seen: set[tuple[str, str, str]],
    ) -> HistoricalMatch | RejectedRecord:
        """Validate and convert one CSV row."""
        raw = {k: v for k, v in row.items() if k and v is not None}

        def reject(reason: RejectionReason, detail: str) -> RejectedRecord:
            return RejectedRecord(reason=reason, detail=detail, row_number=row_number, raw=raw)

        home = (row.get("HomeTeam") or "").strip()
        away = (row.get("AwayTeam") or "").strip()
        if not home or not away:
            return reject(RejectionReason.MISSING_TEAM, "HomeTeam or AwayTeam is empty.")
        if home == away:
            return reject(
                RejectionReason.SAME_TEAM_BOTH_SIDES,
                f"'{home}' listed on both sides.",
            )

        try:
            match_date = parse_match_date(row.get("Date") or "")
        except ValueError as exc:
            return reject(RejectionReason.INVALID_DATE, str(exc))

        try:
            home_goals = int((row.get("FTHG") or "").strip())
            away_goals = int((row.get("FTAG") or "").strip())
        except ValueError:
            return reject(
                RejectionReason.INVALID_SCORE,
                "FTHG or FTAG is not an integer. A blank score usually means "
                "the fixture had not been played when the file was published.",
            )

        if home_goals < 0 or away_goals < 0:
            return reject(RejectionReason.INVALID_SCORE, "Negative score.")
        if home_goals > MAX_PLAUSIBLE_GOALS or away_goals > MAX_PLAUSIBLE_GOALS:
            return reject(
                RejectionReason.IMPOSSIBLE_SCORE,
                f"Score exceeds {MAX_PLAUSIBLE_GOALS}; treating as corrupt.",
            )

        identity = (match_date.isoformat(), home, away)
        if identity in seen:
            return reject(
                RejectionReason.DUPLICATE_MATCH,
                f"'{home}' versus '{away}' on {match_date} already parsed.",
            )
        seen.add(identity)

        half_home = _optional_int(row.get("HTHG"))
        half_away = _optional_int(row.get("HTAG"))

        return HistoricalMatch(
            provider_name=self._name,
            external_id=f"{competition}-{match_date.isoformat()}-{home}-{away}",
            competition_external_id=competition,
            season=season or _season_label(match_date),
            match_date=match_date,
            home_team=HistoricalTeamRef(source_name=home),
            away_team=HistoricalTeamRef(source_name=away),
            home_goals=home_goals,
            away_goals=away_goals,
            half_time_home_goals=half_home,
            half_time_away_goals=half_away,
            closing_odds=parse_odds(row),
            raw=raw,
        )


def parse_odds(row: dict[str, str | None]) -> dict[str, dict[str, Decimal]]:
    """Extract 1X2 odds for every bookmaker present in a row.

    A book is included only when all three prices are present and each exceeds
    1.0. A partial market cannot have its margin removed, and including it
    would produce a prior that silently omits an outcome.
    """
    found: dict[str, dict[str, Decimal]] = {}
    for book, column_sets in ODDS_COLUMNS.items():
        for home_col, draw_col, away_col in column_sets:
            prices = _read_prices(row, home_col, draw_col, away_col)
            if prices is not None:
                found[book] = prices
                break
    return found


def _read_prices(
    row: dict[str, str | None], home_col: str, draw_col: str, away_col: str
) -> dict[str, Decimal] | None:
    """Read one bookmaker's three prices, or ``None`` if unusable."""
    result: dict[str, Decimal] = {}
    for key, column in (("H", home_col), ("D", draw_col), ("A", away_col)):
        raw = (row.get(column) or "").strip()
        if not raw:
            return None
        try:
            price = Decimal(raw)
        except (ArithmeticError, ValueError):
            return None
        if price <= 1:
            return None
        result[key] = price
    return result


def _optional_int(value: str | None) -> int | None:
    """Parse an optional integer column, tolerating blanks."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
