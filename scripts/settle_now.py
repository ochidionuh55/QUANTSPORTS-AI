#!/usr/bin/env python3
"""Settle every pending record now, without waiting for the worker.

    python scripts/settle_now.py

The worker settles on a schedule, which is right for normal operation and
useless when you want to know whether something works. This runs the same
settlement, immediately, and prints what changed.

Also recovers records the worker could not reach. Settlement normally compares
a forecast against a result using the stored analysis, but analyses are pruned
once a fixture is done — so a published selection can outlive the row that
produced it. Selections carry their own fixture id, so the provider is asked
directly for anything the settlement table cannot answer.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.models import HighlightSelection, ServiceSelection
from app.infrastructure.database import Database
from app.providers.live import live_odds_provider
from app.services.highlights import HighlightService
from app.services.selections import SelectionService
from app.services.settlement import SettlementService


async def _counts(database: Database) -> tuple[int, int, int]:
    """Return pending, won and lost selection counts."""
    async with database.session() as session:
        rows = await session.execute(
            select(ServiceSelection.status, func.count()).group_by(ServiceSelection.status)
        )
        tally = {str(status): int(count) for status, count in rows.all()}
    return tally.get("pending", 0), tally.get("won", 0), tally.get("lost", 0)


async def main() -> int:
    """Settle everything pending and report."""
    parser = argparse.ArgumentParser(description="Settle pending records now.")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="How far back to report on. Settlement itself has no limit.",
    )
    args = parser.parse_args()

    settings = get_settings()
    settings.observability.log_level = "WARNING"
    configure_logging(settings)

    provider = live_odds_provider()
    if provider is None:
        print("No results provider configured. Set API_FOOTBALL_KEY.")
        return 1
    if not hasattr(provider, "get_results"):
        print("The configured provider cannot report results.")
        return 1

    database = Database(settings)
    await database.connect()

    try:
        before = await _counts(database)
        print(f"Before:  {before[0]} pending, {before[1]} won, {before[2]} lost")

        budget = getattr(provider, "budget", None)
        if budget is not None:
            print(f"Provider requests left today: {budget.remaining}")
        print()

        async with database.session() as session:
            report = await SettlementService(session).settle(provider)
            print(f"Fixtures settled from stored analyses: {report.settled}")

        async with database.session() as session:
            highlights = await HighlightService(session).settle()
            print(f"Highlights settled: {highlights}")

        async with database.session() as session:
            # The provider is passed so selections whose analysis was pruned
            # can still be scored. Without it, anything older than the
            # retention window would stay pending permanently.
            service = SelectionService(session)
            selections = await service.settle(source=provider)
            print(f"Service selections settled: {selections}")
            if service.last_results_error:
                print(f"\n  Provider said: {service.last_results_error}")

        after = await _counts(database)
        print(f"\nAfter:   {after[0]} pending, {after[1]} won, {after[2]} lost")

        moved = before[0] - after[0]
        if moved > 0:
            print(f"\n{moved} selection(s) moved out of pending.")
        elif before[0] == 0:
            print("\nNothing was pending.")
        else:
            print(
                "\nNothing moved. Either those matches have not finished, or "
                "the provider\nreturned no result for them — a postponed or "
                "abandoned fixture never will."
            )

        await _by_day(database, args.days)
    finally:
        await database.disconnect()
    return 0


async def _by_day(database: Database, days: int) -> None:
    """Print the recent record, day by day."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).date()

    async with database.session() as session:
        rows = await session.execute(
            select(
                ServiceSelection.selection_date,
                ServiceSelection.status,
                func.count(),
            )
            .where(ServiceSelection.selection_date >= cutoff)
            .group_by(ServiceSelection.selection_date, ServiceSelection.status)
            .order_by(ServiceSelection.selection_date.desc())
        )
        entries = rows.all()

    if not entries:
        print("\nNo selections in that window.")
        return

    by_day: dict[object, dict[str, int]] = {}
    for day, status, count in entries:
        by_day.setdefault(day, {})[str(status)] = int(count)

    print(f"\nLast {days} days")
    for day, tally in by_day.items():
        won = tally.get("won", 0)
        lost = tally.get("lost", 0)
        pending = tally.get("pending", 0)
        settled = won + lost
        rate = f"{won}/{settled} ({won / settled:.0%})" if settled else "none settled"
        extra = f", {pending} pending" if pending else ""
        print(f"  {day}: {rate}{extra}")

    async with database.session() as session:
        highlights = await session.execute(
            select(func.count())
            .select_from(HighlightSelection)
            .where(HighlightSelection.status == "pending")
        )
        print(f"\nHighlights still pending: {int(highlights.scalar_one())}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
