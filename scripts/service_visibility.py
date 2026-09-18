#!/usr/bin/env python3
"""Why a service is or is not visible in the product today.

**Four states, routinely conflated.** A service can be registered in the
canonical registry, hold an ACTIVE capability for a competition, produce
qualifying fixtures today, and appear in the Telegram menu — and any of those
can be true while the others are false. "It isn't showing" has at least four
different causes, and the fix differs for each.

    REGISTERED        the canonical registry defines it
    PUBLISHABLE       it is not on the withheld list
    QUALIFIED TODAY   at least one of today's fixtures cleared its threshold
    VISIBLE           the menu lists it, which requires a selection today

The Best of Today menu deliberately hides services with no selection: a button
reading "(0)" invites a tap that leads nowhere. So absence from the menu is
expected when nothing qualified, and is only a bug when something did.

Run::

    railway ssh "python scripts/service_visibility.py"
    railway ssh "python scripts/service_visibility.py --service draw_or_cs"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.core.config import get_settings
from app.database.models import ServiceCapability, ServiceSelection
from app.database.models.capabilities import CapabilityState
from app.infrastructure.database import Database
from app.services.best_of_day import SERVICES
from app.services.selections import WITHHELD, published_services


async def main() -> int:
    """Report each service's four states for today."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", default="")
    args = parser.parse_args()

    today = datetime.now(UTC).date()
    publishable = set(published_services())

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        rows = await session.execute(
            select(
                ServiceSelection.service_key, func.count(ServiceSelection.id)
            )
            .where(ServiceSelection.selection_date == today)
            .group_by(ServiceSelection.service_key)
        )
        published_today: dict[str, int] = {
            str(key): int(count) for key, count in rows.all()
        }

        capability_rows = await session.execute(
            select(
                ServiceCapability.service_key,
                ServiceCapability.state,
                func.count(ServiceCapability.id),
            ).group_by(ServiceCapability.service_key, ServiceCapability.state)
        )
        active_for: dict[str, int] = {}
        for service_key, state, count in capability_rows.all():
            if state == CapabilityState.ACTIVE.value:
                active_for[service_key] = int(count)

    await database.disconnect()

    chosen = [s for s in SERVICES if not args.service or s.key == args.service]

    print("=" * 100)
    print(f"SERVICE VISIBILITY — {today}")
    print("=" * 100)
    print(f"\n  registry holds {len(SERVICES)} services")
    print(f"  withheld from publication: {sorted(WITHHELD) or 'none'}")
    print(
        f"\n  {'service':<16}{'registered':>11}{'publishable':>13}"
        f"{'wave1 ACTIVE':>14}{'today':>8}{'visible':>9}  reason"
    )

    for service in chosen:
        registered = True
        can_publish = service.key in publishable
        active = active_for.get(service.key, 0)
        today_count = int(published_today.get(service.key, 0))
        visible = today_count > 0

        if not can_publish:
            reason = "withheld from publication"
        elif visible:
            reason = "listed in the menu"
        else:
            # Absence with nothing published is the menu working as intended.
            reason = "no selection today — menu hides empty services"

        print(
            f"  {service.key:<16}{'yes':>11}{('yes' if can_publish else 'no'):>13}"
            f"{active:>14}{today_count:>8}{('yes' if visible else 'no'):>9}  {reason}"
        )

    print("\n" + "-" * 100)
    print("READING THIS")
    print("-" * 100)
    print("  registered   : defined in the canonical registry, so it exists")
    print("                 everywhere that derives from it")
    print("  publishable  : not on the withheld list")
    print("  wave1 ACTIVE : how many Wave 1 competitions earned this service.")
    print("                 Zero does not stop the 38 established competitions,")
    print("                 which are not capability-gated")
    print("  today        : selections actually published for today")
    print("  visible      : whether Best of Today lists it, which needs today > 0")
    print("\n  A service registered and publishable with today = 0 is not a bug.")
    print("  It is the product declining to show an empty button.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
