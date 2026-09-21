#!/usr/bin/env python3
"""Immutable snapshot of today's current publications — before any takeover.

Read-only. Captures every currently-published record for today so the pre-V3
state is preserved and auditable no matter what happens next: service
selections and daily highlights, each with its ``model_version``, ``rank``,
``published_at``/``recorded_at``, ``status`` and probability, split into
already-kicked-off (locked) and still-pending (the takeover's candidate set).

Writes the full snapshot to a timestamped JSON file and prints a summary plus
the JSON so it is captured in the run log too.

    railway ssh "python scripts/snapshot_today.py"
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import ServiceSelection
from app.database.models.highlights import HighlightSelection
from app.infrastructure.database import Database

RULE = "=" * 92


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


async def main() -> int:
    now = datetime.now(UTC)
    today = now.date()
    database = Database(get_settings())
    await database.connect()
    try:
        async with database.session() as session:
            sel_rows = (
                await session.execute(
                    select(ServiceSelection).where(ServiceSelection.selection_date == today)
                )
            ).scalars().all()
            hi_rows = (
                await session.execute(
                    select(HighlightSelection).where(
                        HighlightSelection.selection_date == today
                    )
                )
            ).scalars().all()
    finally:
        await database.disconnect()

    selections = [
        {
            "id": s.id,
            "service_key": s.service_key,
            "service_label": s.service_label,
            "rank": s.rank,
            "provider_event_id": s.provider_event_id,
            "fixture": f"{s.home_name} v {s.away_name}",
            "competition": s.competition,
            "market": s.market,
            "outcome": s.outcome,
            "probability": s.probability,
            "score": s.score,
            "model_version": s.model_version,
            "published_at": _iso(s.published_at),
            "kickoff": _iso(s.kickoff),
            "status": s.status,
            "kicked_off": s.kickoff <= now,
        }
        for s in sel_rows
    ]
    highlights = [
        {
            "id": h.id,
            "rank": h.rank,
            "provider_event_id": h.provider_event_id,
            "fixture": f"{h.home_name} v {h.away_name}",
            "competition": h.competition,
            "market": h.market,
            "outcome": h.outcome,
            "probability": h.probability,
            "model_version": h.model_version,
            "recorded_at": _iso(h.recorded_at),
            "kickoff": _iso(h.kickoff),
            "status": h.status,
            "kicked_off": h.kickoff <= now,
        }
        for h in hi_rows
    ]

    snapshot = {
        "captured_at": now.isoformat(),
        "selection_date": today.isoformat(),
        "service_selections": selections,
        "highlight_selections": highlights,
    }
    out = Path(f"/tmp/quantsport_today_snapshot_{today.isoformat()}.json")  # noqa: S108
    try:
        out.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        wrote = str(out)
    except OSError as error:
        wrote = f"(could not write file: {error})"

    sel_by_ver: dict[str, int] = defaultdict(int)
    for s in selections:
        sel_by_ver[str(s["model_version"])] += 1
    sel_pending = sum(1 for s in selections if not s["kicked_off"])
    sel_locked = sum(1 for s in selections if s["kicked_off"])
    hi_pending = sum(1 for h in highlights if not h["kicked_off"])

    print(RULE)
    print(f"TODAY SNAPSHOT — {today.isoformat()}  (captured {now.isoformat()})")
    print(RULE)
    print(f"  service selections     : {len(selections)}")
    for ver, n in sorted(sel_by_ver.items()):
        print(f"      {ver:<28}{n:>4}")
    print(f"  pending (not kicked off): {sel_pending}   <- the takeover's candidate set")
    print(f"  locked (kicked off)     : {sel_locked}   <- immutable, never touched")
    print(f"  highlight selections    : {len(highlights)}  (pending {hi_pending})")
    print(f"  snapshot file           : {wrote}")
    print(RULE)
    print(json.dumps(snapshot, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
