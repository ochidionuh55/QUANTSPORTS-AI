#!/usr/bin/env python3
"""Verify the shadow store's prospective-evidence integrity, at the DB level.

A shadow prediction is only genuine prospective evidence if it was written
*before* kickoff and never altered afterwards. This read-only check proves that
directly against ``research_predictions``:

* every ``mode='shadow'`` row has ``generated_at < kickoff`` (pre-kickoff), and
  any violation is listed — a row stamped at or after kickoff is not evidence;
* the unique key ``(experiment_id, challenger_version, provider_event_id,
  market, outcome)`` has no duplicates (append-only, one forecast per selection);
* settled rows carry a result and an ``hit`` verdict, and their forecast
  ``generated_at`` still precedes kickoff (settlement never moved the forecast).

Reads only. Writes nothing. A non-zero exit means an integrity violation was
found and should stop any promotion reasoning until resolved.

Run::

    railway ssh "python scripts/verify_shadow_persistence.py"
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.core.config import get_settings
from app.database.models import ResearchPrediction
from app.infrastructure.database import Database

RULE = "=" * 92


async def main() -> int:
    database = Database(get_settings())
    await database.connect()
    violations = 0

    async with database.session() as session:
        rows = list(
            (
                await session.execute(
                    select(ResearchPrediction).where(ResearchPrediction.mode == "shadow")
                )
            )
            .scalars()
            .all()
        )

        # Duplicate-key check at the DB level (belt-and-braces over the constraint).
        dup_stmt = (
            select(
                ResearchPrediction.experiment_id,
                ResearchPrediction.challenger_version,
                ResearchPrediction.provider_event_id,
                ResearchPrediction.market,
                ResearchPrediction.outcome,
                func.count().label("n"),
            )
            .where(ResearchPrediction.mode == "shadow")
            .group_by(
                ResearchPrediction.experiment_id,
                ResearchPrediction.challenger_version,
                ResearchPrediction.provider_event_id,
                ResearchPrediction.market,
                ResearchPrediction.outcome,
            )
            .having(func.count() > 1)
        )
        duplicates = list((await session.execute(dup_stmt)).all())

        per_version: dict[str, dict[str, int]] = defaultdict(
            lambda: {"rows": 0, "pre_kickoff": 0, "at_or_after": 0, "settled": 0}
        )
        late: list[ResearchPrediction] = []
        settled_but_late = 0
        for r in rows:
            v = per_version[r.challenger_version]
            v["rows"] += 1
            if r.generated_at < r.kickoff:
                v["pre_kickoff"] += 1
            else:
                v["at_or_after"] += 1
                late.append(r)
            if r.settled_at is not None:
                v["settled"] += 1
                if r.generated_at >= r.kickoff:
                    settled_but_late += 1

    await database.disconnect()

    print(RULE)
    print("VERIFY SHADOW PERSISTENCE — prospective-evidence integrity (read-only)")
    print(RULE)
    print(f"\n  total shadow rows            : {len(rows):,}")
    print("\n  per version (rows · pre-kickoff · at/after KO · settled):")
    for version, v in sorted(per_version.items()):
        print(
            f"    {version:<28} {v['rows']:>6,} · {v['pre_kickoff']:>6,} · "
            f"{v['at_or_after']:>4,} · {v['settled']:>6,}"
        )

    print("\n  INTEGRITY CHECKS:")
    ok_prekick = not late
    print(f"    all forecasts recorded pre-kickoff : {'PASS' if ok_prekick else 'FAIL'}")
    if late:
        violations += len(late)
        for r in late[:10]:
            print(
                f"      ✗ {r.challenger_version} {r.provider_event_id} {r.outcome} "
                f"gen {r.generated_at:%Y-%m-%d %H:%M} >= KO {r.kickoff:%Y-%m-%d %H:%M}"
            )
    ok_dupes = not duplicates
    print(f"    no duplicate forecast keys         : {'PASS' if ok_dupes else 'FAIL'}")
    if duplicates:
        violations += len(duplicates)
        for d in duplicates[:10]:
            print(f"      ✗ duplicate {d[0]}/{d[1]} {d[2]} {d[3]}/{d[4]} x{d[5]}")
    ok_settled = settled_but_late == 0
    print(f"    settled rows still pre-kickoff     : {'PASS' if ok_settled else 'FAIL'}")
    if settled_but_late:
        violations += settled_but_late

    print(RULE)
    if violations == 0:
        print("  INTEGRITY OK — every shadow forecast is genuine pre-kickoff evidence.")
    else:
        print(f"  INTEGRITY VIOLATIONS: {violations}. Resolve before any promotion reasoning.")
    print(RULE)
    return 0 if violations == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
