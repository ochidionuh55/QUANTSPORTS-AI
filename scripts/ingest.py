#!/usr/bin/env python3
"""Load bundled historical CSVs into the database.

Analysis needs canonical history to compute strengths and ratings, so this must
run before the beta can say anything about a fixture.

    docker compose exec api python scripts/ingest.py

Idempotent: re-running skips matches already stored, so it is safe to run after
adding new season files.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.paths import DATA_DIR_ENV, candidate_data_dirs, data_dir
from app.historical.csv_provider import (
    CsvHistoricalDataProvider,
    LocalDirectorySource,
)
from app.historical.models import HistoricalProviderError
from app.infrastructure.database import Database
from app.services.aliases import AliasSeeder, prune_superseded_reviews
from app.services.historical_ingestion import HistoricalIngestionService
from scripts.backtest import COMPETITIONS


async def main() -> int:
    """Ingest every season file found in the data directory."""
    parser = argparse.ArgumentParser(description="Ingest historical CSVs.")
    parser.add_argument("--data", type=Path, default=None)
    parser.add_argument("--competition", action="append", dest="competitions")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)

    args.data = args.data or data_dir()
    if not args.data.is_dir():
        tried = ", ".join(str(p) for p in candidate_data_dirs())
        print(
            f"No CSV directory at {args.data}. Searched: {tried}. "
            f"Set {DATA_DIR_ENV} to point somewhere else.",
            file=sys.stderr,
        )
        return 2

    source = LocalDirectorySource(args.data)
    provider = CsvHistoricalDataProvider(
        source=source,
        competitions=COMPETITIONS,
        source_url="https://www.football-data.co.uk/",
    )

    wanted = set(args.competitions or COMPETITIONS)
    database = Database(settings)
    await database.connect()

    totals = {"inserted": 0, "skipped": 0, "rejected": 0, "teams": 0}
    try:
        for key in sorted(await source.list_keys()):
            stem = key.removesuffix(".csv")
            if "_" not in stem:
                continue
            division, season_label = stem.split("_", 1)
            if division not in wanted:
                continue
            season = season_label.replace("-", "/")

            # One transaction per season: a season present in the database is
            # complete, and a failure leaves nothing half-written.
            async with database.session() as session:
                service = HistoricalIngestionService(session)
                try:
                    report = await service.ingest_dataset(
                        provider,
                        division,
                        season,
                        country=COMPETITIONS[division][1],
                    )
                except HistoricalProviderError as exc:
                    print(f"  {division} {season}: FAILED ({exc})")
                    continue

            print(f"  {report.summary()}")
            totals["inserted"] += report.inserted
            totals["skipped"] += report.skipped_existing
            totals["rejected"] += report.rejected_count
            totals["teams"] += report.teams_created
        # Queued entries the same-provider rule now answers are cleared first:
        # a stale one blocks its fixture from ever resolving.
        async with database.session() as session:
            pruned = await prune_superseded_reviews(session)
        if pruned:
            print(
                f"\ncleared {len(pruned)} superseded review entries: "
                f"{', '.join(sorted(pruned)[:8])}" + (" ..." if len(pruned) > 8 else "")
            )

        # Aliases are seeded after ingestion, since a curated alias can only
        # point at a canonical team that already exists.
        async with database.session() as session:
            counts = await AliasSeeder(session).seed()
        print(
            f"\naliases: {counts['created']} created, "
            f"{counts['existing']} already present, "
            f"{counts['skipped']} skipped (no canonical team)"
        )
    finally:
        await database.disconnect()

    print(
        f"\ninserted {totals['inserted']}, already present {totals['skipped']}, "
        f"rejected {totals['rejected']}, teams created {totals['teams']}"
    )
    if totals["inserted"]:
        # Analyses are precomputed and live for hours. One computed before this
        # ingestion will report clubs as unknown that are now on record, so the
        # card must be rebuilt rather than waiting for the next scheduled scan.
        print(
            "\nStored analyses are now out of date. Rebuild the card with:\n"
            "  docker compose restart worker\n"
            "The scan runs shortly after the worker starts."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
