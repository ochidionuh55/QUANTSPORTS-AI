#!/usr/bin/env python3
"""Run a backtest against real historical CSV data.

Download season files from football-data.co.uk into a directory, then::

    docker compose exec api python scripts/backtest.py --data /data/csv \\
        --competition E0 --season 2023/2024

Files must be named ``<DIV>_<SEASON>.csv`` with the season's slash replaced by
a hyphen, e.g. ``E0_2023-2024.csv``.

The script reports coverage, scores against the market, and a promotion
verdict. A verdict of NOT PROMOTED is a normal and useful result: beating a
bookmaker's closing line is genuinely hard, and knowing that early is worth
more than discovering it after building a product on top.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as `python scripts/backtest.py` from the project root, not only
# as an installed module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.competitions import CSV_COMPETITIONS
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.paths import DATA_DIR_ENV, candidate_data_dirs, data_dir
from app.historical.csv_provider import (
    REFERENCE_BOOKMAKER,
    CsvHistoricalDataProvider,
    LocalDirectorySource,
)
from app.historical.models import (
    HistoricalMatch,
    HistoricalProviderError,
)
from app.quant.backtest_runner import (
    run_backtest,
    sweep_reliability,
)
from app.quant.probability import MarginMethod

COMPETITIONS = CSV_COMPETITIONS


async def load(data_dir: Path, competition: str, seasons: list[str]) -> list[HistoricalMatch]:
    """Load and merge several seasons of one competition."""
    provider = CsvHistoricalDataProvider(
        source=LocalDirectorySource(data_dir),
        competitions=COMPETITIONS,
        source_url="https://www.football-data.co.uk/",
    )

    matches: list[HistoricalMatch] = []
    for season in seasons:
        try:
            snapshot = await provider.load_dataset(competition, season)
        except HistoricalProviderError as exc:
            print(f"  {competition} {season}: SKIPPED ({exc})")
            continue

        with_odds = sum(1 for m in snapshot.matches if m.closing_odds)
        print(
            f"  {competition} {season}: {len(snapshot.matches)} matches, "
            f"{with_odds} with odds, {len(snapshot.rejected)} rejected"
        )
        if snapshot.rejected:
            reasons: dict[str, int] = {}
            for record in snapshot.rejected:
                reasons[str(record.reason)] = reasons.get(str(record.reason), 0) + 1
            print(f"      rejections: {reasons}")
        matches.extend(snapshot.matches)

    return matches


async def main() -> int:
    """Parse arguments, run the backtest and print the verdict."""
    parser = argparse.ArgumentParser(description="Backtest against historical CSVs.")
    parser.add_argument("--data", type=Path, default=None, help="CSV directory")
    parser.add_argument("--competition", default="E0", help="Division code")
    parser.add_argument(
        "--season",
        action="append",
        dest="seasons",
        help="Season, e.g. 2023/2024. Repeatable.",
    )
    parser.add_argument("--reliability", type=float, default=0.4)
    parser.add_argument(
        "--margin-method",
        default=MarginMethod.SHIN.value,
        choices=[m.value for m in MarginMethod],
    )
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--min-train", type=int, default=300)
    parser.add_argument("--reference", default=REFERENCE_BOOKMAKER)
    parser.add_argument("--model-version", default="ensemble-v1")
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Try several reliability values instead of one.",
    )
    args = parser.parse_args()

    settings = get_settings()
    settings.observability.log_level = "WARNING"
    configure_logging(settings)

    args.data = args.data or data_dir()
    if not args.data.is_dir():
        tried = ", ".join(str(p) for p in candidate_data_dirs())
        print(
            f"No CSV directory at {args.data}. Searched: {tried}. "
            f"Set {DATA_DIR_ENV} to point somewhere else.",
            file=sys.stderr,
        )
        return 2

    seasons = args.seasons or ["2023/2024"]
    print(f"Loading {args.competition} from {args.data}")
    matches = await load(args.data, args.competition, seasons)

    if not matches:
        print(
            "\nNo matches loaded. Check file names: <DIV>_<SEASON>.csv, "
            "with the season slash replaced by a hyphen.",
            file=sys.stderr,
        )
        return 1

    priced = sum(1 for m in matches if m.odds_for(args.reference))
    print(f"\n{len(matches)} matches total, {priced} with {args.reference} odds")
    if priced == 0:
        print(
            f"\nNo {args.reference} odds found. Older seasons may not include "
            "them; try --reference bet365 or market_avg.",
            file=sys.stderr,
        )
        return 1

    if args.sweep:
        print("\nReliability sweep (skill versus the market):")
        try:
            findings = sweep_reliability(
                matches,
                margin_method=MarginMethod(args.margin_method),
                folds=args.folds,
                min_train=args.min_train,
                reference_bookmaker=args.reference,
            )
        except ValueError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        for reliability, brier, log_loss in findings:
            print(
                f"  reliability={reliability:.1f}  "
                f"brier_skill={brier:+.5f}  logloss_skill={log_loss:+.5f}"
            )
        print(
            "\nReliability 0.0 is the market itself, so its skill is zero by "
            "construction. If nothing rises above it, the model carries no "
            "information the market lacks."
        )
        return 0

    try:
        report = run_backtest(
            matches,
            model_version=args.model_version,
            reliability=args.reliability,
            margin_method=MarginMethod(args.margin_method),
            folds=args.folds,
            min_train=args.min_train,
            reference_bookmaker=args.reference,
        )
    except ValueError as exc:
        print(f"\n{exc}", file=sys.stderr)
        print("\nTry more seasons, or a lower --min-train.", file=sys.stderr)
        return 1

    print()
    print(report.report())

    if report.decision.passed:
        print(
            "\nThe model cleared the gate on this data. That is a precondition, "
            "not a release: set FEATURES__PROMOTED_MODEL_VERSION explicitly to "
            "enable value detection, and confirm the result holds on a "
            "competition and period that were not used to choose the settings."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
