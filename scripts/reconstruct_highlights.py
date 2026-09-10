#!/usr/bin/env python3
"""Rebuild what daily highlights would have been, from settled predictions.

Gives the track record a history from day one instead of waiting months.

    docker compose exec api python scripts/reconstruct_highlights.py --days 180

Honest because the underlying forecasts were produced chronologically with no
access to their own results, and the same scoring is applied. Stored as
``reconstructed`` and never averaged with live selections — a highlight nobody
saw is evidence about the method, not about the product.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.infrastructure.database import Database
from app.services.highlights import RECONSTRUCTED, HighlightService


async def main() -> int:
    """Reconstruct and report the resulting track record."""
    parser = argparse.ArgumentParser(description="Reconstruct highlights.")
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()

    settings = get_settings()
    settings.observability.log_level = "WARNING"
    configure_logging(settings)

    database = Database(settings)
    await database.connect()
    try:
        async with database.session() as session:
            service = HighlightService(session)
            created = await service.reconstruct(days=args.days)
        print(f"reconstructed {created} highlight selections\n")

        async with database.session() as session:
            service = HighlightService(session)
            for label, days in (
                ("Last 7 days", 7),
                ("Last 30 days", 30),
                ("Last 90 days", 90),
                ("All time", None),
            ):
                record = await service.track_record(label, days=days, source=RECONSTRUCTED)
                if record.settled:
                    expected = record.expected_rate or 0.0
                    print(f"  {record.describe():40} forecasts implied {expected:.1%}")
                else:
                    print(f"  {record.describe()}")

            overall = await service.track_record("all", days=None, source=RECONSTRUCTED)
            if overall.by_market:
                print("\n  by market:")
                for market, (won, played) in sorted(
                    overall.by_market.items(), key=lambda item: -item[1][1]
                ):
                    print(f"    {market:24} {won}/{played} ({won / played:.1%})")

            print(
                "\n  Winning at about the rate the forecasts implied is " "calibration, not edge."
            )
    finally:
        await database.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
