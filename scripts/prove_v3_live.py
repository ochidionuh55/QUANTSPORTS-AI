#!/usr/bin/env python3
"""Prove which engine production is actually serving — from data, not the flag.

Read-only. Prints three things:

1. The **live config** on this service: the active-model-version flag,
   ``is_v3_active()`` and ``model_only_version()``.
2. The **stored analyses** engine breakdown, from each row's provenance
   ``model_version`` and its newest ``computed_at`` — so the freshest scan's
   engine is visible. Rows written before Stage 2 have no stamp and read as
   ``(legacy→v2)``.
3. The **published selections** engine breakdown for the most recent selection
   date, from ``service_selections.model_version``.

Run it before activation (expect V2 everywhere), and again a few minutes after
flipping the worker's flag (the freshest scan and selections should read V3).

    railway ssh "python scripts/prove_v3_live.py"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.database import Database
from app.services.best_of_day import model_only_version
from app.services.v3_pipeline import is_v3_active

RULE = "=" * 92


async def main() -> int:
    settings = get_settings()
    print(RULE)
    print("PROVE V3 LIVE — engine in production, from data")
    print(RULE)
    print("  live config on this service:")
    print(f"    FEATURES__ACTIVE_MODEL_VERSION = {settings.features.active_model_version}")
    print(f"    is_v3_active()                 = {is_v3_active()}")
    print(f"    model_only_version()           = {model_only_version()}")
    print(RULE)

    database = Database(settings)
    await database.connect()
    try:
        async with database.session() as session:
            print("  stored_analyses — engine breakdown (newest first by last compute):")
            rows = (
                await session.execute(
                    text(
                        "SELECT COALESCE(provenance->>'model_version', '(legacy→v2)') AS mv, "
                        "count(*) AS n, max(computed_at) AS newest "
                        "FROM stored_analyses GROUP BY 1 ORDER BY newest DESC"
                    )
                )
            ).all()
            for mv, n, newest in rows:
                print(f"    {mv:<28} n={n:<6} newest_computed_at={newest}")

            freshest = (
                await session.execute(
                    text(
                        "SELECT COALESCE(provenance->>'model_version', '(legacy→v2)'), "
                        "computed_at FROM stored_analyses "
                        "ORDER BY computed_at DESC LIMIT 1"
                    )
                )
            ).first()
            print(RULE)
            if freshest:
                print(f"  freshest stored analysis engine: {freshest[0]}  at {freshest[1]}")

            print(RULE)
            print("  service_selections — engine on the most recent selection date:")
            latest_day = (
                await session.execute(
                    text("SELECT max(selection_date) FROM service_selections")
                )
            ).scalar()
            if latest_day is None:
                print("    (no selections published yet)")
            else:
                sel = (
                    await session.execute(
                        text(
                            "SELECT model_version, count(*), max(published_at) "
                            "FROM service_selections WHERE selection_date = :d "
                            "GROUP BY 1 ORDER BY 3 DESC"
                        ),
                        {"d": latest_day},
                    )
                ).all()
                print(f"    selection_date = {latest_day}")
                for mv, n, newest in sel:
                    print(f"      {mv:<28} n={n:<4} newest_published_at={newest}")
    finally:
        await database.disconnect()
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
