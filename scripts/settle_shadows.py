#!/usr/bin/env python3
"""Shadow settler — fills results onto past shadow predictions, append-only.

For every shadow row whose fixture has kicked off and is not yet settled, this
looks up the final score in ``historical_matches`` (matched by
``provider_event_id`` = ``provider_match_id``) and fills only the settlement
fields: ``home_goals``, ``away_goals``, ``settled_outcome``, ``hit`` and
``settled_at``. The forecast itself — ``probability`` and ``generated_at`` — is
never touched, so a stored prediction is scored against its result exactly as it
was recorded before kickoff. A once-settled row is left alone.

Writes only to ``research_predictions``. No production table is read for
writing and none is modified.

Run (ideally on a schedule after the day's fixtures finish)::

    railway ssh "python scripts/settle_shadows.py"
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import HistoricalMatch, ResearchPrediction
from app.infrastructure.database import Database

RULE = "=" * 92


def _outcome(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"


async def main() -> int:
    database = Database(get_settings())
    await database.connect()
    now = datetime.now(UTC)
    settled = 0
    fixtures_settled: set[str] = set()
    no_result = 0

    async with database.session() as session:
        pending = list(
            (
                await session.execute(
                    select(ResearchPrediction).where(
                        ResearchPrediction.mode == "shadow",
                        ResearchPrediction.settled_at.is_(None),
                        ResearchPrediction.kickoff < now,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not pending:
            print("No past-kickoff shadow rows awaiting settlement.")
            await database.disconnect()
            return 0

        # Resolve final scores once per fixture.
        event_ids = {r.provider_event_id for r in pending}
        result_rows = await session.execute(
            select(
                HistoricalMatch.provider_match_id,
                HistoricalMatch.home_goals,
                HistoricalMatch.away_goals,
            ).where(HistoricalMatch.provider_match_id.in_(event_ids))
        )
        results = {r[0]: (int(r[1]), int(r[2])) for r in result_rows.all()}

        for row in pending:
            score = results.get(row.provider_event_id)
            if score is None:
                no_result += 1
                continue
            hg, ag = score
            outcome = _outcome(hg, ag)
            row.home_goals = hg
            row.away_goals = ag
            row.settled_outcome = outcome
            row.hit = row.outcome == outcome
            row.settled_at = now
            settled += 1
            fixtures_settled.add(row.provider_event_id)

        await session.commit()

    await database.disconnect()
    print(RULE)
    print("SETTLE SHADOWS — fill results onto pre-kickoff forecasts")
    print(RULE)
    print(f"\n  as of                        : {now:%Y-%m-%d %H:%M} UTC")
    print(f"  rows awaiting settlement     : {len(pending):,}")
    print(f"  rows settled                 : {settled:,}")
    print(f"  fixtures settled             : {len(fixtures_settled):,}")
    print(f"  rows without a result yet    : {no_result:,}")
    print("\n  Filled settlement fields only. Forecast + generated_at untouched.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
