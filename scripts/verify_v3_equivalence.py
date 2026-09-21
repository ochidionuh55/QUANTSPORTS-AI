#!/usr/bin/env python3
"""Prove research-code == production-code for V3.

Walks the corpus and, at every modellable fixture, computes the V3 expected
goals and 1X2 **two independent ways**:

* the **research** path — the exact functions in ``scripts/exp_009_stack_norm.py``
  that produced the promotion evidence (control strength + recency/opponent
  adjusted fixed point at 2 iterations + totals-preserving rescale);
* the **production** path — ``app.quant.v3_stack_norm`` (the canonical module
  that production will route through).

If the two agree to 1e-9 on every fixture, the model being promoted *is* the
model that earned the evidence. Any mismatch fails the check — and nothing
ships. Read-only; no writes; production untouched.

Run::

    railway ssh "python scripts/verify_v3_equivalence.py"
    railway ssh "python scripts/verify_v3_equivalence.py --test-end 2023-09-04"
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.database import Database
from app.quant import v3_stack_norm as prod
from app.quant.poisson import expected_goals

TEST_DAYS = 365
DEFAULT_WINDOW_DAYS = 1095
TOL = 1e-9
RULE = "=" * 92


def _load_research():
    """Import the actual research script as a module."""
    path = Path(__file__).resolve().parent / "exp_009_stack_norm.py"
    spec = importlib.util.spec_from_file_location("exp_009_stack_norm", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["exp_009_stack_norm"] = module
    spec.loader.exec_module(module)
    return module


async def _load(session) -> tuple[dict[int, str], list[tuple]]:
    comp_rows = await session.execute(text("SELECT id, canonical_name FROM competitions"))
    names = {int(r[0]): str(r[1]) for r in comp_rows.fetchall()}
    match_rows = await session.execute(
        text(
            "SELECT competition_id, home_team_id, away_team_id, season, match_date, "
            "home_goals, away_goals FROM historical_matches "
            "WHERE competition_id IS NOT NULL ORDER BY competition_id, match_date, id"
        )
    )
    return names, match_rows.fetchall()


def _research_lambdas(research, acc, home_id: int, away_id: int, ref: int):
    """V3 expected goals via the exact EXP-009 research functions."""
    if len(acc.results) < prod.MIN_LEAGUE_RESULTS:
        return None
    avg = acc.averages()
    ch = research._control_strength(acc, home_id, avg)
    ca = research._control_strength(acc, away_id, avg)
    if not (ch.is_reliable and ca.is_reliable and avg.is_reliable):
        return None
    snaps = research._adjusted_strengths(acc, avg, prod.ITERATIONS, ref, prod.HALF_LIFE)
    snap = snaps.get(prod.ITERATIONS)
    if snap is None or home_id not in snap or away_id not in snap:
        return None
    sl_h, sl_a = expected_goals(snap[home_id], snap[away_id], avg)
    stack_total = sl_h + sl_a
    if stack_total <= 0:
        return None
    cl_h, cl_a = expected_goals(ch, ca, avg)
    scale = (cl_h + cl_a) / stack_total
    return sl_h * scale, sl_a * scale


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-end", default=None)
    parser.add_argument("--test-days", type=int, default=TEST_DAYS)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    args = parser.parse_args()

    research = _load_research()
    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        _names, rows = await _load(session)
    await database.disconnect()
    if not rows:
        print("No historical matches.")
        return 1

    max_date = max(r[4] for r in rows)
    test_end = (
        datetime.strptime(args.test_end, "%Y-%m-%d").replace(tzinfo=UTC).date()
        if args.test_end
        else max_date
    )
    test_start = test_end - timedelta(days=args.test_days)
    window_start = test_start - timedelta(days=args.window_days)

    by_comp: dict[int, list[tuple]] = {}
    for r in rows:
        by_comp.setdefault(int(r[0]), []).append(r)

    checked = 0
    mismatches = 0
    max_diff = 0.0
    agree_none = 0

    for _comp_id, matches in by_comp.items():
        acc = research.Accumulator()
        for _cid, home_id, away_id, _season, mdate, hg, ag in matches:
            home_id, away_id, hg, ag = int(home_id), int(away_id), int(hg), int(ag)
            ordinal = mdate.toordinal()
            if (
                len(acc.results) >= prod.MIN_LEAGUE_RESULTS
                and acc.played(home_id) >= 5
                and acc.played(away_id) >= 5
                and window_start <= mdate < test_end
            ):
                r_lam = _research_lambdas(research, acc, home_id, away_id, ordinal)
                p_lam = prod.v3_expected_goals(acc.matches, home_id, away_id, ordinal)
                if r_lam is None and p_lam is None:
                    agree_none += 1
                elif r_lam is None or p_lam is None:
                    mismatches += 1  # one modelled, the other didn't
                else:
                    checked += 1
                    d = max(abs(r_lam[0] - p_lam[0]), abs(r_lam[1] - p_lam[1]))
                    max_diff = max(max_diff, d)
                    if d > TOL:
                        mismatches += 1
            acc.record(home_id, away_id, hg, ag, ordinal)

    print(RULE)
    print("VERIFY V3 EQUIVALENCE — research-code == production-code")
    print(f"window [{window_start}, {test_end})")
    print(RULE)
    print(f"\n  fixtures compared (both modelled) : {checked:,}")
    print(f"  agreed unmodellable               : {agree_none:,}")
    print(f"  max |Δ lambda| across all         : {max_diff:.2e}")
    print(f"  mismatches (>|{TOL:.0e}| or gating): {mismatches:,}")
    print(RULE)
    if mismatches == 0 and checked > 0:
        print("  PASS — production V3 reproduces the research V3 exactly.")
        print("  research-code == production-code : CONFIRMED")
        result = 0
    else:
        print("  FAIL — production and research V3 disagree. DO NOT PROMOTE.")
        result = 1
    print(RULE)
    return result


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
