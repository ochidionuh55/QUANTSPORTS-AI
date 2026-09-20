#!/usr/bin/env python3
"""Establish the provenance of ``settled_predictions`` — read-only, run once.

A settled row is not automatically out-of-sample. The table stores a ``source``
flag (``live`` vs ``backfill``); a backfilled row used the same engine and
leakage discipline but with hindsight about which fixtures exist. This probe
reads the split and the timing so we label the 93,633 rows honestly before any
evaluation leans on them. It writes nothing.

Run::

    railway ssh "python scripts/probe_settled_predictions.py"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.database import Database

RULE = "=" * 92


async def rows(session, sql: str):
    return (await session.execute(text(sql))).fetchall()


async def main() -> int:
    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        print(RULE)
        print("SETTLED_PREDICTIONS — PROVENANCE PROBE (read-only)")
        print(RULE)

        total = (await rows(session, "SELECT count(*) FROM settled_predictions"))[0][0]
        distinct = (
            await rows(
                session,
                "SELECT count(DISTINCT (provider_name, provider_event_id)) "
                "FROM settled_predictions",
            )
        )[0][0]
        print(f"\n  total rows                         : {total:,}")
        print(f"  distinct (provider,event) tuples   : {distinct:,}")
        print(f"  duplicate rows (should be 0)        : {total - distinct:,}")

        print("\n  BY SOURCE (live = prospective; backfill = reconstructed w/ hindsight):")
        for src, n in await rows(
            session,
            "SELECT source, count(*) FROM settled_predictions "
            "GROUP BY source ORDER BY count(*) DESC",
        ):
            print(f"    {src!s:<12} {n:>10,}")

        print("\n  BY MODEL_VERSION:")
        for mv, n in await rows(
            session,
            "SELECT model_version, count(*) FROM settled_predictions "
            "GROUP BY model_version ORDER BY count(*) DESC",
        ):
            print(f"    {mv!s:<28} {n:>10,}")

        print("\n  SOURCE x MODEL_VERSION:")
        for src, mv, n in await rows(
            session,
            "SELECT source, model_version, count(*) FROM settled_predictions "
            "GROUP BY source, model_version ORDER BY source, count(*) DESC",
        ):
            print(f"    {src!s:<10} {mv!s:<28} {n:>10,}")

        print("\n  GENUINE MARKET BASELINE (market_* non-null), by source:")
        for src, n, with_mkt in await rows(
            session,
            "SELECT source, count(*), "
            "count(*) FILTER (WHERE market_home IS NOT NULL) "
            "FROM settled_predictions GROUP BY source ORDER BY source",
        ):
            print(f"    {src!s:<10} {with_mkt:>10,} / {n:<10,} have market prices")

        print("\n  DATE / TIME SEMANTICS, by source:")
        for src, k0, k1, s0, s1 in await rows(
            session,
            "SELECT source, min(kickoff), max(kickoff), min(settled_at), max(settled_at) "
            "FROM settled_predictions GROUP BY source ORDER BY source",
        ):
            print(f"    {src!s:<10} kickoff {k0} -> {k1}")
            print(f"    {'':<10} settled {s0} -> {s1}")

        print("\n  PRE-KICKOFF EVIDENCE (created_at < kickoff), by source:")
        print("    live rows created before kickoff are genuine prospective forecasts;")
        print("    backfill rows are reconstructed and created_at reflects the backfill run.")
        for src, n, before in await rows(
            session,
            "SELECT source, count(*), "
            "count(*) FILTER (WHERE created_at < kickoff) "
            "FROM settled_predictions GROUP BY source ORDER BY source",
        ):
            print(f"    {src!s:<10} {before:>10,} / {n:<10,} created before kickoff")

        await session.rollback()
    await database.disconnect()
    print("\n" + RULE)
    print("PROVENANCE PROBE COMPLETE — nothing was written.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
