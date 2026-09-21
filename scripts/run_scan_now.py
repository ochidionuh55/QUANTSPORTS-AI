#!/usr/bin/env python3
"""Run one normal production cycle on demand — scan, highlights, publish.

Identical to the worker's scheduled job (``app/worker/main.py`` daily_scan),
just triggered now instead of on the interval, so remaining unplayed fixtures
are re-analysed with the currently active engine immediately rather than at the
next 3-hourly scan. With V3 active this restamps today's upcoming fixtures with
the full V3 view (including the goals/BTTS markets) and publishes V3 selections
into any still-empty service/rank slots — filled slots keep the version they
were published under (no restamping, no rewriting history).

    railway ssh "python scripts/run_scan_now.py"
    # if it reports no provider on that service:
    railway ssh --service worker "python scripts/run_scan_now.py"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.infrastructure.database import Database
from app.providers.live import live_odds_provider
from app.services.daily_scan import DailyScanService
from app.services.highlights import HighlightService
from app.services.selections import SelectionService
from app.services.v3_pipeline import is_v3_active

RULE = "=" * 92


async def main() -> int:
    settings = get_settings()
    print(RULE)
    print("RUN PRODUCTION CYCLE NOW")
    print(f"  active engine   : {settings.features.active_model_version}")
    print(f"  is_v3_active()  : {is_v3_active()}")
    print(RULE)

    provider = live_odds_provider()
    if provider is None:
        print("  No live provider configured on this service — scan skipped.")
        print("  Re-run with:  railway ssh --service worker \"python scripts/run_scan_now.py\"")
        print(RULE)
        return 1

    database = Database(settings)
    await database.connect()
    try:
        async with database.session() as session:
            report = await DailyScanService(session).scan(provider)
        print(f"  scan       : {report.summary()}")

        async with database.session() as session:
            chosen = await HighlightService(session).record_daily()
        print(f"  highlights : {chosen.summary()}")

        async with database.session() as session:
            published = await SelectionService(session).publish()
        print(f"  selections : {published.summary()}")
    finally:
        await database.disconnect()
    print(RULE)
    print("  Done. Now run:  python scripts/verify_v3_surfaces.py")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
