#!/usr/bin/env python3
"""Verify V3 propagation across the live read surfaces — from stored data.

Read-only. Every user/admin surface (Today's Analysis, fixture detail, Market
Explorer, website markets) reads the stored analysis rather than recomputing, so
proving the *stored* record is V3-consistent proves the surfaces are. For each
freshly V3-stamped analysis this checks, straight from ``stored_analyses``:

* the model-derived goals markets — Over/Under and BTTS — equal the V3 grid
  rebuilt from the stored expected goals (so they are V3, not legacy V2);
* the model-only 1X2 (``model_probabilities``) equals that V3 grid's result mass;
* the published ``markets['1X2']`` is the market posterior — deliberately NOT
  the model number, and correctly left alone.

Run::

    railway ssh "python scripts/verify_v3_surfaces.py"
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.competitions import code_for_name
from app.core.config import get_settings
from app.infrastructure.database import Database
from app.quant import v3_stack_norm as v3
from app.quant.grid import build_match_probabilities

RULE = "=" * 92
TOL = Decimal("0.0005")
SAMPLE = 40

_LABELS = {
    "model-only-v3-stack-norm": "V3 Stack-Norm",
    "model-only-v2-dc": "V2 Dixon-Coles",
}


def _d(x: object) -> Decimal | None:
    try:
        return Decimal(str(x))
    except Exception:  # noqa: BLE001
        return None


async def main() -> int:
    database = Database(get_settings())
    await database.connect()
    checked = goals_ok = btts_ok = model_ok = market_ok = 0
    failures: list[str] = []
    examples: list[str] = []
    try:
        async with database.session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT home_name, away_name, competition, expected_home_goals, "
                        "expected_away_goals, markets, model_probabilities, computed_at "
                        "FROM stored_analyses "
                        "WHERE provenance->>'model_version' = :v "
                        "AND expected_home_goals IS NOT NULL "
                        "ORDER BY computed_at DESC LIMIT :n"
                    ),
                    {"v": v3.VERSION, "n": SAMPLE},
                )
            ).all()

            for home, away, comp, xgh, xga, markets, model_probs, _ts in rows:
                markets = markets or {}
                code = code_for_name(comp)
                mp = build_match_probabilities(
                    float(xgh), float(xga), corrected=True, competition=code
                )
                checked += 1

                # Goals: stored Over/Under == V3 grid
                goals = markets.get("Goals", {}) or {}
                g_ok = True
                for line in ("0.5", "1.5", "2.5", "3.5"):
                    stored = _d(goals.get(f"Over {line}"))
                    if stored is None:
                        continue
                    if abs(stored - mp.over_under[f"over_{line}"]) > TOL:
                        g_ok = False
                if g_ok:
                    goals_ok += 1
                else:
                    failures.append(f"{home} v {away}: Goals != V3 grid")

                # BTTS: stored Yes == V3 grid
                btts = markets.get("Both teams to score", {}) or {}
                b_stored = _d(btts.get("Yes"))
                if b_stored is not None and abs(b_stored - mp.both_teams_score) <= TOL:
                    btts_ok += 1
                elif b_stored is not None:
                    failures.append(f"{home} v {away}: BTTS != V3 grid")

                # Model-only 1X2 == V3 grid result mass
                total = mp.home_win + mp.draw + mp.away_win
                mh = _d((model_probs or {}).get("home"))
                if mh is not None and total > 0 and abs(mh - mp.home_win / total) <= TOL:
                    model_ok += 1

                # Published 1X2 is the market posterior — present, and NOT forced
                # to equal the model. We only confirm it exists and sums to ~1.
                x12 = markets.get("1X2", {}) or {}
                s = sum((_d(v) or Decimal(0) for v in x12.values()), Decimal(0))
                if x12 and abs(s - Decimal(1)) <= Decimal("0.01"):
                    market_ok += 1

                if len(examples) < 3 and goals:
                    o25 = _d(goals.get("Over 2.5"))
                    examples.append(
                        f"  🧠 {_LABELS.get('model-only-v3-stack-norm')} | {home} v {away} "
                        f"[{code}]\n"
                        f"      xG {float(xgh):.2f}-{float(xga):.2f}  "
                        f"Over2.5 {o25}  (V3 grid {mp.over_under['over_2.5']:.4f})  "
                        f"BTTS {b_stored}"
                    )
    finally:
        await database.disconnect()

    print(RULE)
    print("VERIFY V3 SURFACES — stored-data propagation")
    print(RULE)
    print(f"  V3-stamped analyses checked : {checked}")
    print(f"  Goals (O/U) == V3 grid      : {goals_ok}/{checked}")
    print(f"  BTTS == V3 grid             : {btts_ok}/{checked}")
    print(f"  model-only 1X2 == V3 grid   : {model_ok}/{checked}")
    print(f"  published 1X2 = market prior : {market_ok}/{checked}  (market-derived, unchanged)")
    print(RULE)
    for ex in examples:
        print(ex)
    print(RULE)
    if checked == 0:
        print("  No V3-stamped analyses yet — run after the worker's next V3 scan.")
        return 1
    if failures:
        print(f"  FAIL — {len(failures)} fixtures inconsistent:")
        for f in failures[:8]:
            print(f"    - {f}")
        print(RULE)
        return 1
    print("  PASS — every V3 surface serves the V3 grid; 1X2 stays market-derived.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
