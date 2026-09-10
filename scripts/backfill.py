#!/usr/bin/env python3
"""Backfill historical predictions.

Replays completed matches in date order, forecasting each with only the
information available before kickoff, then settles it against the known result.

    docker compose exec api python scripts/backfill.py --data /data

Idempotent: re-running skips fixtures already backfilled, so it is safe to run
again after adding season files.

Backfilled predictions are stored with ``source="backfill"`` and are never
blended with live results.
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
from app.infrastructure.database import Database
from app.services.backfill import BackfillService
from app.services.settlement import PerformanceService
from scripts.backtest import COMPETITIONS, load


async def main() -> int:
    """Backfill every requested competition."""
    parser = argparse.ArgumentParser(description="Backfill historical predictions.")
    parser.add_argument("--data", type=Path, default=None)
    parser.add_argument("--competition", action="append", dest="competitions")
    parser.add_argument("--reference", default="market_avg")
    parser.add_argument("--seasons", type=int, default=9)
    args = parser.parse_args()

    settings = get_settings()
    settings.observability.log_level = "WARNING"
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

    wanted = args.competitions or list(COMPETITIONS)
    seasons = [f"{y}/{y + 1}" for y in range(2026 - args.seasons, 2026)]

    database = Database(settings)
    await database.connect()
    totals = {"stored": 0, "present": 0, "seen": 0}

    try:
        for code in wanted:
            if code not in COMPETITIONS:
                print(f"Unknown competition '{code}', skipping.")
                continue

            name = COMPETITIONS[code][0]
            print(f"\n=== {name} ({code}) ===")
            matches = await load(args.data, code, seasons)
            if not matches:
                print("  no data")
                continue

            def progress(done: int, total: int) -> None:
                print(f"  {done}/{total} replayed", end="\r", flush=True)

            # One transaction per competition: a competition present in the
            # table is complete, and a failure leaves nothing half-written.
            async with database.session() as session:
                report = await BackfillService(session).backfill(
                    matches,
                    competition_name=name,
                    reference_bookmaker=args.reference,
                    progress=progress,
                )
            print(" " * 40, end="\r")
            print(f"  {report.summary()}")

            totals["stored"] += report.stored
            totals["present"] += report.already_present
            totals["seen"] += report.matches_seen

        print(
            f"\nTotal: {totals['seen']} matches replayed, "
            f"{totals['stored']} predictions stored, "
            f"{totals['present']} already present"
        )

        async with database.session() as session:
            service = PerformanceService(session)
            counts = await service.counts_by_source()
            print(f"\nSettled predictions by source: {counts}")

            summary = await service.summarise(source="backfill")
            if summary.sample:
                print("\n=== BACKFILL PERFORMANCE ===")
                print(f"  sample          {summary.sample}")
                print(f"  favourite won   {summary.favourite_accuracy:.1%}")
                print(f"  brier           {summary.brier:.5f}")
                if summary.market_brier:
                    print(f"  market brier    {summary.market_brier:.5f}")
                    print(f"  skill vs market {summary.brier_skill:+.3%}")
                if summary.over_2_5_accuracy is not None:
                    print(f"  over 2.5 acc    {summary.over_2_5_accuracy:.1%}")
                if summary.btts_accuracy is not None:
                    print(f"  btts acc        {summary.btts_accuracy:.1%}")
                print(f"\n  {summary.verdict}")
    finally:
        await database.disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
