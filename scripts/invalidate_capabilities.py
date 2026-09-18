#!/usr/bin/env python3
"""Fail-close capabilities whose market predicate has changed underneath them.

**Why all sixteen, not the two that were ACTIVE.** Every clean-sheet capability
was measured against "each team's own clean sheet". The market is "Any Clean
Sheet" — either side. A verdict computed from the wrong predicate is not
evidence about the right one, whichever way it came out. An
INSUFFICIENT_EVIDENCE reached on the wrong rule is no more informative than an
ACTIVE reached on it.

**Immediate, ahead of revalidation.** Two of them are publishing right now:
Azadegan and Bosnia both hold ``Home or Clean Sheet`` ACTIVE. Leaving those in
place until a fresh validation run finishes would keep publishing selections
priced on a predicate the product no longer uses.

**The rows are marked, never deleted.** Their evidence is what explains the
six corrected settlements, and a future model change is compared against it.

Run::

    railway ssh "python scripts/invalidate_capabilities.py --market 'Result or clean sheet'"
    railway ssh "python scripts/invalidate_capabilities.py --market 'Result or clean sheet' --apply"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import ServiceCapability
from app.database.models.capabilities import CapabilityState
from app.infrastructure.database import Database
from app.services.best_of_day import SERVICES

REASON = "MARKET_PREDICATE_CHANGED"
"""Why the verdict no longer applies.

Distinct from CALIBRATION_FAIL and INSUFFICIENT_SAMPLE: nothing was learned
about this service that turned out badly. The question it answered stopped
being the question production asks.
"""


async def main() -> int:
    """Invalidate every capability for a market whose predicate changed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--market",
        required=True,
        help="Canonical market name whose predicate changed.",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    # Services publishing this market, by key — the capability rows are keyed
    # by service, not by market.
    affected_keys = {s.key for s in SERVICES if s.market == args.market}
    if not affected_keys:
        print(f"No service publishes {args.market!r}.")
        return 1

    print("=" * 88)
    print(f"CAPABILITY INVALIDATION — {args.market}")
    print("=" * 88)
    print(f"\n  services affected: {', '.join(sorted(affected_keys))}")

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        rows = await session.execute(
            select(ServiceCapability).where(
                ServiceCapability.service_key.in_(affected_keys)
            )
        )
        capabilities = list(rows.scalars().all())

        if not capabilities:
            print("\n  No capability rows found for these services.")
            await database.disconnect()
            return 0

        was_active = [c for c in capabilities if c.state == CapabilityState.ACTIVE.value]
        print(f"\n  capability rows  : {len(capabilities)}")
        print(f"  currently ACTIVE : {len(was_active)}")

        print(f"\n  {'competition':<20}{'service':<16}{'was':<24}becomes")
        for capability in sorted(
            capabilities, key=lambda c: (c.competition_code, c.service_key)
        ):
            print(
                f"  {capability.competition_code:<20}{capability.service_key:<16}"
                f"{capability.state:<24}INSUFFICIENT_EVIDENCE"
            )
            if args.apply:
                capability.state = CapabilityState.INSUFFICIENT_EVIDENCE.value
                capability.reason = REASON
                # The old numbers described a different predicate. Keeping them
                # against the new state would read as evidence for it.
                capability.predicted_rate = None
                capability.observed_rate = None
                capability.calibration_gap = None
                capability.interval_low = None
                capability.interval_high = None
                capability.brier = None
                capability.window_spread = None

        if args.apply:
            await session.commit()
            print(f"\n  APPLIED. {len(capabilities)} capabilities invalidated,")
            print(f"  reason {REASON}.")
            if was_active:
                print(f"  {len(was_active)} were publishing and have stopped.")
            print("\n  These services publish nothing for Wave 1 competitions until")
            print("  revalidated against the restored predicate.")
        else:
            await session.rollback()
            print("\n  DRY RUN — nothing written.")

    await database.disconnect()
    print("\n" + "=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
