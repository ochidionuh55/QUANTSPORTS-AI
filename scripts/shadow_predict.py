#!/usr/bin/env python3
"""Shadow prediction producer — populates the research store before kickoff.

For today's upcoming fixtures it writes the **control** (``model-only-v2-dc``)
1X2 into ``research_predictions`` as append-only shadow rows. It does not
recompute the control: it copies ``stored_analyses.model_probabilities``, which
is the production model's *own* output (no bookmaker input). So the research
store carries exactly what v2-dc said, and the Telegram Research Lab can read it
without any surface ever calculating a probability itself.

Challenger variants (EXP-002, …) will append their own rows here through the
same store once their walk-forward is validated; nothing is fabricated to fill a
screen, and a fixture simply shows only the models that legitimately produced a
prediction for it.

**Append-only and pre-kickoff.** Rows are written only for fixtures whose
kickoff is still in the future, ``generated_at`` is stamped now (< kickoff), and
the unique key ``(experiment_id, challenger_version, provider_event_id, market,
outcome)`` means a re-run before kickoff never duplicates or overwrites an
earlier shadow. A prediction, once stored, is never regenerated.

Writes only to ``research_predictions``. No production table is touched.

Run (ideally on a schedule before the day's kickoffs)::

    railway ssh "python scripts/shadow_predict.py"
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import ResearchPrediction, StoredAnalysis
from app.infrastructure.database import Database

CONTROL_EXPERIMENT = "CTRL-V2DC"
CONTROL_VERSION = "model-only-v2-dc"
MARKET = "1X2"
OUTCOMES = ("home", "draw", "away")
RULE = "=" * 92


def _prob(value: object) -> float | None:
    try:
        p = float(str(value))
    except (TypeError, ValueError):
        return None
    return p if 0.0 <= p <= 1.0 else None


async def main() -> int:
    database = Database(get_settings())
    await database.connect()
    now = datetime.now(UTC)
    written = 0
    fixtures = 0
    skipped_existing = 0
    skipped_incomplete = 0

    async with database.session() as session:
        upcoming = list(
            (
                await session.execute(
                    select(StoredAnalysis).where(StoredAnalysis.kickoff > now)
                )
            )
            .scalars()
            .all()
        )

        # Preload existing control shadow keys for upcoming fixtures, so a re-run
        # adds only what is missing and never rewrites a stored prediction.
        existing_rows = await session.execute(
            select(
                ResearchPrediction.provider_event_id, ResearchPrediction.outcome
            ).where(
                ResearchPrediction.experiment_id == CONTROL_EXPERIMENT,
                ResearchPrediction.challenger_version == CONTROL_VERSION,
                ResearchPrediction.market == MARKET,
            )
        )
        existing = {(row[0], row[1]) for row in existing_rows.all()}

        for record in upcoming:
            probs = record.model_probabilities or {}
            values = {o: _prob(probs.get(o)) for o in OUTCOMES}
            if any(v is None for v in values.values()):
                skipped_incomplete += 1
                continue
            fixtures += 1
            for outcome in OUTCOMES:
                if (record.provider_event_id, outcome) in existing:
                    skipped_existing += 1
                    continue
                session.add(
                    ResearchPrediction(
                        experiment_id=CONTROL_EXPERIMENT,
                        challenger_version=CONTROL_VERSION,
                        mode="shadow",
                        provider_event_id=record.provider_event_id,
                        competition=record.competition,
                        home_name=record.home_name,
                        away_name=record.away_name,
                        kickoff=record.kickoff,
                        market=MARKET,
                        outcome=outcome,
                        probability=values[outcome],
                        generated_at=now,
                    )
                )
                written += 1

        await session.commit()

    await database.disconnect()
    print(RULE)
    print("SHADOW PREDICT — CONTROL (model-only-v2-dc)")
    print(RULE)
    print(f"\n  as of                       : {now:%Y-%m-%d %H:%M} UTC")
    print(f"  upcoming fixtures scanned   : {len(upcoming):,}")
    print(f"  fixtures with full 1X2      : {fixtures:,}")
    print(f"  shadow rows written         : {written:,}")
    print(f"  rows already present (kept) : {skipped_existing:,}")
    print(f"  fixtures skipped (no 1X2)   : {skipped_incomplete:,}")
    print("\n  Append-only. Wrote only research_predictions. No production change.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
