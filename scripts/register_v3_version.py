#!/usr/bin/env python3
"""Register the immutable ``model-only-v3-stack-norm`` model-version row.

This writes a single ledger entry in ``model_versions`` recording the V3 engine
promoted from EXP-009: its frozen lineage, the strict out-of-sample validation
windows, and the metrics from the incumbent-vs-V3 promotion comparison. It is a
record, not a switch — **nothing about serving is decided here**. Which engine
production actually serves is governed solely by
``FEATURES__ACTIVE_MODEL_VERSION`` (see ``app.services.v3_pipeline``). The row is
registered **VALIDATED, not PROMOTED**, so it can never drive the market
value-detection path; it exists so every stored V3 selection names a version the
ledger can explain.

Idempotent: if a row for this version already exists it is left exactly as-is
(the ledger is append-only and immutable), and the script reports that and
exits 0. Run once::

    railway ssh "python scripts/register_v3_version.py"
    railway ssh "python scripts/register_v3_version.py --dry-run"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.config import get_settings
from app.database.enums import EnsembleMethod, MarginMethod, ModelStatus
from app.database.models.historical import ModelVersion
from app.infrastructure.database import Database
from app.quant import v3_stack_norm as v3

RULE = "=" * 92

# The two strict out-of-sample windows the promotion evidence was measured on
# (see scripts/incumbent_vs_v3.py and scripts/verify_v3_equivalence.py).
NEWEST_TEST = (date(2025, 9, 3), date(2026, 9, 3))
OOS_TEST = (date(2022, 9, 4), date(2023, 9, 4))


def _configuration() -> dict[str, object]:
    """The frozen, do-not-tune lineage — the single source of truth for V3."""
    return {
        "engine": v3.VERSION,
        "promoted_from_experiment": "EXP-009 (exp009-stack-norm-hl365-it2)",
        "ratio": "recency-weighted + opponent-adjusted team strength",
        "recency_half_life_days": v3.HALF_LIFE,
        "opponent_adjustment_iterations": v3.ITERATIONS,
        "total_goals_anchor": "equal-weight control total (totals-preserving)",
        "grid": "canonical Dixon-Coles, per-competition rho",
        "min_league_results": v3.MIN_LEAGUE_RESULTS,
        "ratio_clamp": [v3.RATIO_FLOOR, v3.RATIO_CEIL],
        "canonical_module": "app.quant.v3_stack_norm",
        "research_equals_production": "verified Δλ=0 over 101,940 fixtures (both windows)",
        "serving_switch": "FEATURES__ACTIVE_MODEL_VERSION (not this row's status)",
        "note": (
            "Registered non-PROMOTED on purpose: this engine drives the "
            "model-only view and the canonical services grid, never the market "
            "value-detection path."
        ),
    }


def _validation_metrics() -> dict[str, object]:
    """Metrics from the ACTUAL-incumbent vs V3 promotion comparison."""
    return {
        "comparison": "actual production incumbent (Poisson+Elo+form blend) vs pure V3",
        "newest_window": {
            "test_start": NEWEST_TEST[0].isoformat(),
            "test_end": NEWEST_TEST[1].isoformat(),
            "brier_incumbent": 0.6199,
            "brier_v3": 0.6146,
            "markets_better": 18,
            "markets_worse": 0,
        },
        "oos_window": {
            "test_start": OOS_TEST[0].isoformat(),
            "test_end": OOS_TEST[1].isoformat(),
            "brier_incumbent": 0.6140,
            "brier_v3": 0.6089,
            "markets_better": 18,
            "markets_worse": 0,
        },
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written without writing.",
    )
    args = parser.parse_args()

    database = Database(get_settings())
    await database.connect()
    try:
        async with database.session() as session:
            existing = (
                await session.execute(
                    select(ModelVersion).where(ModelVersion.version == v3.VERSION)
                )
            ).scalar_one_or_none()

            print(RULE)
            print(f"REGISTER MODEL VERSION — {v3.VERSION}")
            print(RULE)

            if existing is not None:
                print("  already registered — the ledger is immutable, leaving it untouched.")
                print(f"  id={existing.id} status={existing.status} "
                      f"created_at={existing.created_at}")
                print(RULE)
                return 0

            row = ModelVersion(
                name="Model-only V3 (stack-norm)",
                version=v3.VERSION,
                status=ModelStatus.VALIDATED,
                margin_method=MarginMethod.SHIN,
                ensemble_method=EnsembleMethod.MARKET_ONLY,
                configuration=_configuration(),
                feature_set={
                    "recency_half_life_days": v3.HALF_LIFE,
                    "opponent_adjustment_iterations": v3.ITERATIONS,
                    "totals_preserving": True,
                },
                validation_metrics=_validation_metrics(),
                validation_start=OOS_TEST[0],
                validation_end=OOS_TEST[1],
                test_start=NEWEST_TEST[0],
                test_end=NEWEST_TEST[1],
                promoted_at=None,
                promotion_note=(
                    "Not promoted. Serving is governed by "
                    "FEATURES__ACTIVE_MODEL_VERSION; this row is the audit record."
                ),
            )

            if args.dry_run:
                print("  DRY RUN — would insert:")
                print(f"    name={row.name!r} version={row.version!r} status={row.status}")
                print(f"    validation {OOS_TEST[0]}..{OOS_TEST[1]}  "
                      f"test {NEWEST_TEST[0]}..{NEWEST_TEST[1]}")
                print(RULE)
                return 0

            session.add(row)
            await session.flush()
            print(f"  registered id={row.id} version={row.version} status={row.status}")
            print(f"  created_at={datetime.now(UTC).isoformat()}")
            print(RULE)
            return 0
    finally:
        await database.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
