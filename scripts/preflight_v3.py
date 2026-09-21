#!/usr/bin/env python3
"""Stage-2 V3 production preflight — read-only, fail-closed.

Proves, against real production data and the real code paths, that routing the
canonical pipeline through V3 is safe **before** the activation flag is ever
flipped. It writes nothing to the database. It touches only its own process's
environment (never the running services) to exercise the activation switch.

Checks (all must pass; any failure exits non-zero and V2 stays active):

  A. Canonical-grid invariant. For real modellable fixtures, the grid the
     services rebuild from the stored full-precision lambdas (per-competition
     rho, no tilt — exactly what ``selections._forecast`` does under V3) is
     bit-identical to the canonical ``v3_stack_norm`` grid. "Services grid ==
     V3 grid, exactly."
  B. 22-service propagation. Every published service's (market, outcome) is
     produced by ``derive_markets`` from a V3 grid, with a valid probability,
     and the result/goal/BTTS partitions each sum to one. V3 feeds every
     market naturally — no market is left unfilled.
  C. Settlement compatibility. The market universe a V3 grid yields is exactly
     the market universe a V2 grid yields (same keys; only the numbers move),
     so every settlement predicate in ``markets.py`` still applies unchanged.
  D. Default-OFF equivalence. Under the real current settings the pipeline is
     V2: ``is_v3_active()`` is False and ``model_only_version()`` returns the
     V2 string. Deploying this code changes nothing until the flag is flipped.
  E. Activation + rollback semantics. Flipping ``FEATURES__ACTIVE_MODEL_VERSION``
     to V3 makes ``is_v3_active()`` True and ``model_only_version()`` return the
     V3 string; flipping it back restores V2 exactly. One reversible switch.

Run::

    railway ssh "python scripts/preflight_v3.py"
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.competitions import code_for_name
from app.core.config import get_settings
from app.infrastructure.database import Database
from app.quant import v3_stack_norm as v3
from app.quant.grid import build_grid
from app.quant.markets import MARKETS, derive_markets
from app.services.best_of_day import SERVICES, model_only_version
from app.services.v3_pipeline import is_v3_active

RULE = "=" * 92
WINDOW_DAYS = 1095
SAMPLE_TARGET = 250  # modellable fixtures to exercise across competitions
TOL = Decimal("0")  # the invariant is exact; anything non-zero fails

RESULT_PARTITION = ("1X2", {"Home", "Draw", "Away"})
GOALS_OU = ("Goals", {"Over 2.5", "Under 2.5"})
BTTS_PARTITION = ("Both teams to score", {"Yes", "No"})


class Check:
    """One named check that accumulates failures."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.samples = 0

    def fail(self, detail: str) -> None:
        self.failures.append(detail)

    @property
    def ok(self) -> bool:
        return not self.failures

    def report(self) -> str:
        head = "PASS" if self.ok else "FAIL"
        line = f"  [{head}] {self.name}  (samples: {self.samples})"
        if self.failures:
            line += "\n" + "\n".join(f"        - {f}" for f in self.failures[:8])
            if len(self.failures) > 8:
                line += f"\n        … and {len(self.failures) - 8} more"
        return line


async def _load(session) -> tuple[dict[int, str], list[tuple]]:
    comp_rows = await session.execute(text("SELECT id, canonical_name FROM competitions"))
    names = {int(cid): str(name) for cid, name in comp_rows.all()}
    match_rows = await session.execute(
        text(
            "SELECT competition_id, home_team_id, away_team_id, home_goals, "
            "away_goals, match_date FROM historical_matches "
            "WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL"
        )
    )
    rows = match_rows.all()
    return names, rows


def _grid_equal(a: dict, b: dict, check: Check, label: str) -> None:
    if set(a.keys()) != set(b.keys()):
        check.fail(f"{label}: grid support differs")
        return
    worst = Decimal(0)
    for key in a:
        diff = abs(Decimal(a[key]) - Decimal(b[key]))
        if diff > worst:
            worst = diff
    if worst > TOL:
        check.fail(f"{label}: max cell |Δ|={worst} > {TOL}")


async def main() -> int:
    database = Database(get_settings())
    await database.connect()
    try:
        async with database.session() as session:
            names, rows = await _load(session)
    finally:
        await database.disconnect()

    if not rows:
        print("No historical matches — cannot run preflight.")
        return 1

    max_date = max(r[5] for r in rows)
    if max_date.tzinfo is None:
        max_date = max_date.replace(tzinfo=UTC)
    window_start = max_date - timedelta(days=WINDOW_DAYS)

    by_comp: dict[int, list[tuple]] = {}
    for r in rows:
        md = r[5]
        if md.tzinfo is None:
            md = md.replace(tzinfo=UTC)
        by_comp.setdefault(int(r[0]), []).append(
            (int(r[1]), int(r[2]), int(r[3]), int(r[4]), md)
        )

    check_a = Check("A. canonical-grid invariant (services grid == V3 grid)")
    check_b = Check("B. 22-service propagation (every market filled from V3)")
    check_c = Check("C. settlement compatibility (V3 market universe == V2's)")

    ref = max_date.toordinal()
    sampled = 0
    for comp_id, matches in by_comp.items():
        if sampled >= SAMPLE_TARGET:
            break
        name = names.get(comp_id)
        if not name:
            continue
        code = code_for_name(name)
        # Pool = history strictly before the reference moment (as production sees it).
        pool = [
            (h, a, hg, ag, md.toordinal())
            for (h, a, hg, ag, md) in matches
            if window_start <= md < max_date
        ]
        if len(pool) < v3.MIN_LEAGUE_RESULTS:
            continue
        # Recent fixtures in this competition are the ones to model.
        recent = sorted(matches, key=lambda m: m[4], reverse=True)[:12]
        for h, a, _hg, _ag, _md in recent:
            if sampled >= SAMPLE_TARGET:
                break
            lambdas = v3.v3_expected_goals(pool, h, a, ref)
            if lambdas is None:
                continue
            sampled += 1

            # --- Check A: two grids, one truth ---
            canonical = v3.v3_grid(pool, h, a, ref, competition=code)
            services_rebuilt = build_grid(
                float(lambdas[0]), float(lambdas[1]), corrected=True, competition=code
            )
            check_a.samples += 1
            if canonical is None:
                check_a.fail(f"{code} {h}-{a}: canonical grid None though lambdas present")
            else:
                _grid_equal(canonical, services_rebuilt, check_a, f"{code} {h}-{a}")

            # --- Check B: every service market present + valid, partitions sum to 1 ---
            grid = services_rebuilt
            markets = derive_markets(grid)
            check_b.samples += 1
            for svc in SERVICES:
                got = markets.get(svc.market, {}).get(svc.outcome)
                if got is None:
                    check_b.fail(f"{code}: service {svc.key} → {svc.market}/{svc.outcome} missing")
                elif not (Decimal(0) <= got <= Decimal("1.0000001")):
                    check_b.fail(f"{code}: {svc.key} probability out of range: {got}")
            for market_name, outcomes in (RESULT_PARTITION, GOALS_OU, BTTS_PARTITION):
                total = sum(
                    (markets.get(market_name, {}).get(o, Decimal(0)) for o in outcomes),
                    Decimal(0),
                )
                if abs(total - Decimal(1)) > Decimal("0.0005"):
                    check_b.fail(f"{code}: {market_name} partition sums to {total}, not 1")

            # --- Check C: V3 market universe == V2 market universe ---
            v2_grid = build_grid(float(lambdas[0]), float(lambdas[1]))  # global rho, no comp
            v3_keys = {(m, o) for m, outs in derive_markets(grid).items() for o in outs}
            v2_keys = {(m, o) for m, outs in derive_markets(v2_grid).items() for o in outs}
            registry_keys = {(d.market, d.outcome) for d in MARKETS}
            check_c.samples += 1
            if v3_keys != v2_keys:
                check_c.fail(f"{code}: V3 market keys differ from V2 ({v3_keys ^ v2_keys})")
            if v3_keys != registry_keys:
                missing = registry_keys - v3_keys
                extra = v3_keys - registry_keys
                check_c.fail(
                    f"{code}: V3 keys != MARKETS registry "
                    f"(missing={missing} extra={extra})"
                )

    # --- Check D: default-OFF equivalence (real current settings) ---
    check_d = Check("D. default-OFF equivalence (deploy changes nothing)")
    check_d.samples = 1
    if is_v3_active():
        check_d.fail("is_v3_active() is True under current settings — deploy would change output")
    mv_now = model_only_version()
    if mv_now == v3.VERSION:
        check_d.fail(f"model_only_version() already returns V3 ({mv_now}) with flag unset")

    # --- Check E: activation + rollback semantics (process-local env only) ---
    check_e = Check("E. activation + rollback semantics (one reversible switch)")
    check_e.samples = 1
    saved = os.environ.get("FEATURES__ACTIVE_MODEL_VERSION")
    try:
        os.environ["FEATURES__ACTIVE_MODEL_VERSION"] = v3.VERSION
        get_settings.cache_clear()
        if not is_v3_active():
            check_e.fail("flag=V3 but is_v3_active() is False")
        if model_only_version() != v3.VERSION:
            check_e.fail(f"flag=V3 but model_only_version()={model_only_version()}")
        # roll back
        if saved is None:
            os.environ.pop("FEATURES__ACTIVE_MODEL_VERSION", None)
        else:
            os.environ["FEATURES__ACTIVE_MODEL_VERSION"] = saved
        get_settings.cache_clear()
        if is_v3_active():
            check_e.fail("after rollback is_v3_active() is still True")
        if model_only_version() == v3.VERSION:
            check_e.fail("after rollback model_only_version() still returns V3")
    finally:
        if saved is None:
            os.environ.pop("FEATURES__ACTIVE_MODEL_VERSION", None)
        else:
            os.environ["FEATURES__ACTIVE_MODEL_VERSION"] = saved
        get_settings.cache_clear()

    checks = [check_a, check_b, check_c, check_d, check_e]
    print(RULE)
    print("V3 PRODUCTION PREFLIGHT — Stage 2")
    print(f"window [{window_start.date()}, {max_date.date()})  fixtures exercised: {sampled}")
    print(RULE)
    for c in checks:
        print(c.report())
    print(RULE)
    if all(c.ok for c in checks):
        print("  V3 PRODUCTION PREFLIGHT — PASS")
        print("  Safe to activate via FEATURES__ACTIVE_MODEL_VERSION=" + v3.VERSION)
        print(RULE)
        return 0
    print("  V3 PRODUCTION PREFLIGHT — FAIL — leaving V2 active. Nothing to activate.")
    print(RULE)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
