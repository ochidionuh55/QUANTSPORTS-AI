#!/usr/bin/env python3
"""Extended validation: per-window consistency, uncertainty, and baselines.

Answers three questions a single aggregate number cannot:

**Was it consistent?** An overall win driven by one good period is not an edge.
Per-window results show whether the model helps repeatedly or got lucky once.

**Is it distinguishable from noise?** A paired bootstrap on per-forecast squared
errors gives a confidence interval on the Brier difference. If that interval
straddles zero, the result is indistinguishable from chance regardless of how
positive the point estimate looks.

**Which component earned it?** An ensemble beating the market says nothing
about which part did the work. Each model is scored alone against the same
market, on the same fixtures.

Run::

    docker compose exec api python scripts/validate.py --competition E1
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.quant.backtest import (
    DatedForecast,
    walk_forward,
)
from app.quant.backtest_runner import build_forecasts
from app.quant.metrics import brier_score, calibration, log_loss
from scripts.backtest import load

BOOTSTRAP_SAMPLES = 2000


def bootstrap_brier_difference(
    forecasts: list[DatedForecast], samples: int = BOOTSTRAP_SAMPLES, seed: int = 17
) -> tuple[float, float, float]:
    """Return the mean and 95% interval of the market-minus-model Brier gap.

    Paired: each resample draws the same fixtures for both, so the interval
    reflects uncertainty in the *difference* rather than in either score alone.
    Positive means the model is better.
    """
    paired = [
        (
            (f.market_probability - f.outcome) ** 2,
            (f.model_probability - f.outcome) ** 2,
        )
        for f in forecasts
    ]
    rng = random.Random(seed)
    size = len(paired)
    means: list[float] = []
    for _ in range(samples):
        picks = [paired[rng.randrange(size)] for _ in range(size)]
        means.append(statistics.fmean(market - model for market, model in picks))
    means.sort()
    point = statistics.fmean(market - model for market, model in paired)
    return point, means[int(0.025 * samples)], means[int(0.975 * samples)]


def evaluate_variant(
    matches: list,
    label: str,
    reliability: float,
    components: frozenset[str] | None,
    reference: str = "market_avg",
) -> dict[str, object] | None:
    """Score one model configuration against the market."""
    forecasts, run = build_forecasts(
        matches,
        reliability=reliability,
        allowed_components=components,
        reference_bookmaker=reference,
    )
    if len(forecasts) < 400:
        return None

    model = [f.model_probability for f in forecasts]
    market = [f.market_probability for f in forecasts]
    outcomes = [f.outcome for f in forecasts]

    point, low, high = bootstrap_brier_difference(forecasts)
    result = walk_forward(forecasts, folds=6, min_train=400)

    return {
        "label": label,
        "n": len(forecasts),
        "brier": brier_score(model, outcomes),
        "market_brier": brier_score(market, outcomes),
        "log_loss": log_loss(model, outcomes),
        "market_log_loss": log_loss(market, outcomes),
        "ece": calibration(model, outcomes).expected_calibration_error,
        "noise_floor": calibration(model, outcomes).noise_floor,
        "diff": point,
        "low": low,
        "high": high,
        "significant": low > 0,
        "consistency": result.consistency,
        "windows": len(result.per_split),
        "per_window": [r.brier_skill for r in result.per_split],
        "coverage": run.coverage,
    }


async def main() -> int:
    """Run the extended validation and print the analysis."""
    parser = argparse.ArgumentParser(description="Extended model validation.")
    parser.add_argument("--data", type=Path, default=None)
    parser.add_argument("--competition", default="E1")
    parser.add_argument("--reference", default="market_avg")
    parser.add_argument("--reliability", type=float, default=0.2)
    parser.add_argument("--seasons", type=int, default=9)
    args = parser.parse_args()

    settings = get_settings()
    settings.observability.log_level = "ERROR"
    configure_logging(settings)

    seasons = [f"{year}/{year + 1}" for year in range(2025 - args.seasons, 2025)]
    print(f"Loading {args.competition}: {seasons[0]} to {seasons[-1]}")
    matches = await load(args.data, args.competition, seasons)
    priced = sum(1 for m in matches if m.odds_for(args.reference))
    print(f"\n{len(matches)} matches, {priced} with {args.reference} odds\n")

    if priced < 500:
        print("Not enough priced matches to validate.", file=sys.stderr)
        return 1

    print("=" * 78)
    print("RELIABILITY SWEEP (full ensemble)")
    print("=" * 78)
    print(f"{'rel':>5} {'brier skill':>12} {'95% interval':>24} {'consistency':>12}")
    for reliability in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5):
        row = evaluate_variant(matches, "ensemble", reliability, None, args.reference)
        if row is None:
            continue
        skill = (row["market_brier"] - row["brier"]) / row["market_brier"]  # type: ignore[operator]
        print(
            f"{reliability:>5.1f} {skill:>+12.5f} "
            f"[{row['low']:>+.6f}, {row['high']:>+.6f}] "
            f"{row['consistency']:>11.0%}"
        )

    print()
    print("=" * 78)
    print(f"MODEL BASELINES (reliability {args.reliability})")
    print("=" * 78)

    variants: list[tuple[str, float, frozenset[str] | None]] = [
        ("A market only", 0.0, None),
        ("B poisson alone", 1.0, frozenset({"poisson"})),
        ("C market + poisson", args.reliability, frozenset({"poisson"})),
        ("D market + elo", args.reliability, frozenset({"elo"})),
        ("E market + form", args.reliability, frozenset({"form"})),
        ("F market + ensemble", args.reliability, None),
    ]

    print(
        f"{'variant':<22}{'n':>6}{'brier':>9}{'logloss':>9}"
        f"{'skill':>10}{'sig?':>6}{'consist':>9}{'ece':>8}"
    )
    for label, reliability, components in variants:
        row = evaluate_variant(matches, label, reliability, components, args.reference)
        if row is None:
            print(f"{label:<22}{'insufficient data':>40}")
            continue
        skill = (row["market_brier"] - row["brier"]) / row["market_brier"]  # type: ignore[operator]
        print(
            f"{label:<22}{row['n']:>6}{row['brier']:>9.5f}"
            f"{row['log_loss']:>9.5f}{skill:>+10.5f}"
            f"{('yes' if row['significant'] else 'no'):>6}"
            f"{row['consistency']:>9.0%}{row['ece']:>8.4f}"
        )

    print()
    print("=" * 78)
    print(f"PER-WINDOW DETAIL (ensemble, reliability {args.reliability})")
    print("=" * 78)
    detail = evaluate_variant(matches, "ensemble", args.reliability, None, args.reference)
    if detail:
        for index, skill in enumerate(detail["per_window"], start=1):  # type: ignore[arg-type]
            marker = "+" if skill > 0 else "-"
            print(f"  window {index}: brier skill {skill:>+.5f}  [{marker}]")
        print()
        print(
            f"  windows positive : {sum(1 for s in detail['per_window'] if s > 0)}"  # type: ignore[union-attr]
            f" of {detail['windows']}"
        )
        print(
            f"  paired bootstrap : {detail['diff']:+.6f} "
            f"[{detail['low']:+.6f}, {detail['high']:+.6f}]"
        )
        print(
            f"  calibration      : {detail['ece']:.4f} "
            f"(noise floor {detail['noise_floor']:.4f})"
        )
        print()
        if detail["significant"]:
            print("  The interval excludes zero: the improvement is unlikely to")
            print("  be chance alone. Confirm on a competition not used to")
            print("  choose these settings before treating it as real.")
        else:
            print("  The interval includes zero: this result is NOT")
            print("  distinguishable from noise. Any apparent edge here would")
            print("  be model error rather than market error.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
