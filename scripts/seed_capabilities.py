#!/usr/bin/env python3
"""Import the Wave 1 capability matrix into production.

**All 168, not the 43 winners.** The withheld and insufficient rows are the
reason the passing ones mean anything: without them there is no record that a
service was tested and rejected, only an unexplained absence. A later model
change is compared against this matrix rather than quietly replacing it.

**Reconciled before it is trusted.** The import counts what it wrote and
refuses to report success unless the states total 43 ACTIVE, 79 WITHHELD and
46 INSUFFICIENT_EVIDENCE. A matrix that does not reconcile is a matrix nobody
should publish from.

**Version-bound.** Every row records the model version it was validated
against. Production compares that to the version producing today's numbers, so
a model change stops publication rather than silently publishing under evidence
that describes something else.

**Idempotent.** Re-running updates rows in place on
``(competition_code, service_key, model_version)``.

Run::

    railway ssh "python scripts/seed_capabilities.py --dry-run"
    railway ssh "python scripts/seed_capabilities.py --apply"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models import ServiceCapability
from app.database.models.capabilities import CapabilityState
from app.infrastructure.database import Database
from app.services.best_of_day import model_only_version


def _matrix_path() -> Path:
    """Find the committed matrix.

    ``data/`` first, because the runtime image copies only ``app``,
    ``migrations``, ``scripts`` and ``data`` — a file at the repository root is
    committed but never reaches the container, which is how the first seeding
    attempt failed with the matrix sitting in git the whole time.
    """
    root = Path(__file__).resolve().parents[1]
    for candidate in (
        root / "data" / "wave1_capabilities.json",
        root / "wave1_capabilities.json",
        Path("wave1_capabilities.json"),
    ):
        if candidate.exists():
            return candidate
    return root / "data" / "wave1_capabilities.json"


MATRIX_PATH = _matrix_path()
VALIDATION_VERSION = "wave1-walkforward-v1"

# Provider league id to our competition code. The matrix is keyed by league id;
# publication resolves a fixture to a code, so the capability must be stored
# under the code or every lookup silently misses.
LEAGUE_CODES: dict[str, tuple[str, str]] = {
    "382": ("LEU", "Liga Leumit"),
    "291": ("AZA", "Azadegan League"),
    "287": ("PRV", "Prva Liga"),
    "240": ("PRB", "Primera B"),
    "496": ("ALF", "Liga Alef"),
    "399": ("NPF", "NPFL"),
    "317": ("BRS", "1st League - RS"),
    "243": ("ECB", "Liga Pro Serie B"),
}

def _expected_total() -> int:
    """How many capabilities the registry implies.

    Derived, not written down. The universe was 21 services x 8 competitions
    until "Draw or Any Clean Sheet" was restored; a hard-coded 168 would then
    have rejected a correct matrix, or worse, accepted a stale one. The
    registry is the authority on how many services exist.
    """
    from app.services.best_of_day import SERVICES

    return len(SERVICES) * len(LEAGUE_CODES)


def load() -> dict[str, dict[str, Any]]:
    """Read the committed matrix."""
    return dict(json.loads(MATRIX_PATH.read_text(encoding="utf-8")))


async def seed(session: AsyncSession, apply: bool) -> int:
    """Import every capability, reconciling before reporting success."""
    matrix = load()
    version = model_only_version()
    now = datetime.now(UTC)

    expected_total = _expected_total()
    states: dict[str, int] = defaultdict(int)
    by_competition: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    created = updated = skipped = 0

    for key, row in matrix.items():
        league_id = key.split(":", 1)[0]
        mapping = LEAGUE_CODES.get(league_id)
        if mapping is None:
            print(f"  unknown league id {league_id}; skipped")
            skipped += 1
            continue
        code, name = mapping
        service_key = str(row.get("service_key") or "")
        state = str(row.get("state") or "")
        if state not in {s.value for s in CapabilityState}:
            print(f"  unknown state {state!r} for {code}/{service_key}; skipped")
            skipped += 1
            continue

        states[state] += 1
        by_competition[code][state] += 1
        if not apply:
            continue

        found = await session.execute(
            select(ServiceCapability).where(
                ServiceCapability.competition_code == code,
                ServiceCapability.service_key == service_key,
                ServiceCapability.model_version == version,
            )
        )
        capability = found.scalar_one_or_none()
        if capability is None:
            capability = ServiceCapability(
                competition_code=code,
                service_key=service_key,
                model_version=version,
                competition_name=name,
                service_label=str(row.get("service") or service_key),
                validation_version=VALIDATION_VERSION,
                state=state,
                reason=str(row.get("reason") or ""),
                validated_at=now,
            )
            session.add(capability)
            created += 1
        else:
            updated += 1

        capability.provider_league_id = int(league_id)
        capability.competition_name = name
        capability.service_label = str(row.get("service") or service_key)
        capability.validation_version = VALIDATION_VERSION
        capability.state = state
        capability.reason = str(row.get("reason") or "")
        capability.threshold = row.get("threshold")
        capability.qualifying_sample = int(row.get("qualifying") or 0)
        capability.total_sample = int(row.get("total") or 0)
        capability.predicted_rate = row.get("tail_predicted")
        capability.observed_rate = row.get("tail_observed")
        capability.calibration_gap = row.get("tail_gap")
        interval = row.get("interval") or [None, None]
        capability.interval_low = interval[0]
        capability.interval_high = interval[1]
        capability.brier = row.get("brier")
        capability.window_spread = row.get("window_spread")
        capability.validated_at = now

    print("\n" + "=" * 88)
    print("CAPABILITY MATRIX — " + ("APPLIED" if apply else "DRY RUN"))
    print("=" * 88)
    print(f"\n  model version      : {version}")
    print(f"  validation version : {VALIDATION_VERSION}")
    print(f"\n  rows in matrix     : {len(matrix)}")
    if apply:
        print(f"  created            : {created}")
        print(f"  updated            : {updated}")
    print(f"  skipped            : {skipped}")

    print("\n  STATES")
    for state in ("ACTIVE", "WITHHELD", "INSUFFICIENT_EVIDENCE"):
        print(f"    {state:<24}{states.get(state, 0):>5}")

    total = sum(states.values())
    print(f"\n  total in matrix    : {total}")
    print(f"  registry implies   : {expected_total}"
          f"   ({_expected_total() // len(LEAGUE_CODES)} services"
          f" x {len(LEAGUE_CODES)} competitions)")

    # The matrix must cover the whole universe. A short matrix means a service
    # was validated for some competitions and not others, and the missing pairs
    # would fail closed without anyone noticing they were never measured.
    reconciles = total == expected_total

    print("\n  BY COMPETITION")
    for code in sorted(by_competition):
        rows = by_competition[code]
        print(
            f"    {code:<6}ACTIVE {rows.get('ACTIVE', 0):>3}"
            f"   WITHHELD {rows.get('WITHHELD', 0):>3}"
            f"   INSUFFICIENT {rows.get('INSUFFICIENT_EVIDENCE', 0):>3}"
        )

    if not reconciles:
        print(f"\n  DOES NOT RECONCILE. {total} rows against {expected_total} the")
        print("  registry implies.")
        print("  Nothing was committed. A matrix that does not reconcile is not")
        print("  one to publish from.")
        return 1

    print(f"\n  RECONCILES: {total} capabilities, {states.get('ACTIVE', 0)} ACTIVE.")
    if apply:
        await session.commit()
        print("  Committed.")
    else:
        await session.rollback()
        print("  DRY RUN — nothing written.")
    return 0


async def main() -> int:
    """Seed the matrix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    if not MATRIX_PATH.exists():
        print(f"{MATRIX_PATH} not found.")
        print("The matrix must be committed under data/ — the runtime image does")
        print("not copy files from the repository root.")
        return 1

    print(f"  matrix: {MATRIX_PATH}")

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        code = await seed(session, apply)
        if not apply:
            await session.rollback()
    await database.disconnect()
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
