"""First-run data bootstrap.

Loads the historical record, backfills predictions and reconstructs highlight
history — automatically, once, on worker startup.

**Why this is not a console command.** A deployment that requires someone to
open a terminal, run three long jobs in the right order and keep the session
alive is a deployment that will be done wrong, or not at all. Worse, a failure
halfway through leaves the product looking broken with no obvious cause. The
worker is already the process responsible for keeping data current, so it may
as well be responsible for there being data in the first place.

**Safe to run every time.** Each step checks whether its work is already done
and returns immediately if so, which makes a restart cheap and a partial run
resumable. Ingestion is idempotent by construction, so an interrupted load
continues from where it stopped rather than duplicating.

**Never blocks the bot.** Bootstrap runs in the background after the scheduler
starts. Users see an honest "no fixtures analysed yet" during the first load
rather than a bot that does not answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select

from app.core.competitions import CSV_COMPETITIONS
from app.core.logging import get_logger
from app.core.paths import data_dir
from app.database.models import (
    HighlightSelection,
    HistoricalMatch,
    SettledPrediction,
    Team,
)
from app.historical.csv_provider import CsvHistoricalDataProvider, LocalDirectorySource
from app.historical.models import HistoricalProviderError
from app.infrastructure.database import Database
from app.services.aliases import AliasSeeder, prune_superseded_reviews
from app.services.backfill import BackfillService
from app.services.highlights import HighlightService
from app.services.historical_ingestion import HistoricalIngestionService

logger = get_logger(__name__)

MIN_MATCHES = 1_000
"""Below this the database is treated as empty and ingestion runs.

Not zero: a partially loaded database from an interrupted run should continue
rather than being mistaken for a complete one.
"""

MIN_SETTLED = 1_000
MIN_HIGHLIGHTS = 50
BACKFILL_SEASONS = 9


@dataclass
class BootstrapReport:
    """What the bootstrap did."""

    ingested: int = 0
    backfilled: int = 0
    highlights: int = 0
    skipped: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Initialise the skipped list."""
        if self.skipped is None:
            self.skipped = []

    def summary(self) -> str:
        """Return a one-line human summary."""
        if not (self.ingested or self.backfilled or self.highlights):
            return f"nothing to do ({', '.join(self.skipped) or 'all present'})"
        return (
            f"{self.ingested} matches ingested, "
            f"{self.backfilled} predictions backfilled, "
            f"{self.highlights} highlights reconstructed"
        )


async def bootstrap(database: Database) -> BootstrapReport:
    """Load everything the product needs, once.

    Each stage is independent: a failure in one is logged and the next still
    runs, because partial data is far better than none.
    """
    report = BootstrapReport()
    directory = data_dir()

    if not directory.is_dir() or not any(directory.glob("*.csv")):
        logger.warning("bootstrap.no_data_directory", path=str(directory))
        report.skipped.append("no CSV files shipped")
        return report

    try:
        report.ingested = await _ingest(database, directory)
    except Exception as exc:
        logger.exception("bootstrap.ingest_failed", error_type=type(exc).__name__)

    try:
        report.backfilled = await _backfill(database, directory)
    except Exception as exc:
        logger.exception("bootstrap.backfill_failed", error_type=type(exc).__name__)

    try:
        report.highlights = await _reconstruct(database)
    except Exception as exc:
        logger.exception("bootstrap.reconstruct_failed", error_type=type(exc).__name__)

    logger.info("bootstrap.completed", summary=report.summary())
    return report


async def _count(database: Database, model: type) -> int:
    """Return how many rows a table holds."""
    async with database.session() as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


async def _ingest(database: Database, directory: Path) -> int:
    """Load the historical record, if it is not already loaded."""
    existing = await _count(database, HistoricalMatch)
    if existing >= MIN_MATCHES:
        logger.info("bootstrap.ingest_skipped", matches=existing)
        return 0

    logger.info("bootstrap.ingest_started", existing=existing)
    source = LocalDirectorySource(directory)
    provider = CsvHistoricalDataProvider(
        source=source,
        competitions=CSV_COMPETITIONS,
        source_url="https://www.football-data.co.uk/",
    )

    inserted = 0
    for key in sorted(await source.list_keys()):
        stem = key.removesuffix(".csv")
        if "_" not in stem:
            continue
        division, season_label = stem.split("_", 1)
        if division not in CSV_COMPETITIONS:
            continue

        # One transaction per season, so an interruption leaves whole seasons
        # rather than a half-written one.
        async with database.session() as session:
            service = HistoricalIngestionService(session)
            try:
                report = await service.ingest_dataset(
                    provider,
                    division,
                    season_label.replace("-", "/"),
                    country=CSV_COMPETITIONS[division][1],
                )
            except HistoricalProviderError as exc:
                logger.warning("bootstrap.season_failed", season=key, error=str(exc))
                continue
        inserted += report.inserted

    async with database.session() as session:
        pruned = await prune_superseded_reviews(session)
    async with database.session() as session:
        await AliasSeeder(session).seed()

    teams = await _count(database, Team)
    logger.info(
        "bootstrap.ingest_finished",
        inserted=inserted,
        teams=teams,
        pruned_reviews=len(pruned),
    )
    return inserted


async def _backfill(database: Database, directory: Path) -> int:
    """Replay history into settled predictions, if not already done."""
    existing = await _count(database, SettledPrediction)
    if existing >= MIN_SETTLED:
        logger.info("bootstrap.backfill_skipped", predictions=existing)
        return 0

    matches = await _count(database, HistoricalMatch)
    if matches < MIN_MATCHES:
        logger.info("bootstrap.backfill_skipped_no_history")
        return 0

    logger.info("bootstrap.backfill_started")
    from scripts.backtest import load

    seasons = [f"{year}/{year + 1}" for year in range(2026 - BACKFILL_SEASONS, 2026)]
    stored = 0

    for code, (name, _country) in CSV_COMPETITIONS.items():
        try:
            loaded = await load(directory, code, seasons)
        except Exception as exc:  # noqa: BLE001 - one competition must not stop the rest
            logger.warning("bootstrap.load_failed", competition=code, error=str(exc))
            continue
        if not loaded:
            continue

        async with database.session() as session:
            report = await BackfillService(session).backfill(
                loaded, competition_name=name, reference_bookmaker="market_avg"
            )
        stored += report.stored

    logger.info("bootstrap.backfill_finished", stored=stored)
    return stored


async def _reconstruct(database: Database) -> int:
    """Rebuild highlight history, if not already present."""
    existing = await _count(database, HighlightSelection)
    if existing >= MIN_HIGHLIGHTS:
        logger.info("bootstrap.reconstruct_skipped", highlights=existing)
        return 0

    settled = await _count(database, SettledPrediction)
    if settled < MIN_SETTLED:
        logger.info("bootstrap.reconstruct_skipped_no_predictions")
        return 0

    logger.info("bootstrap.reconstruct_started")
    async with database.session() as session:
        created = await HighlightService(session).reconstruct(days=365)

    logger.info("bootstrap.reconstruct_finished", created=created)
    return created
