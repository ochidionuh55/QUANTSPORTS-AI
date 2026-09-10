#!/usr/bin/env python3
"""Compare Poisson against Dixon-Coles on identical fixtures.

Both models are run through the same harness, on the same matches, against the
same market baseline. Anything less is not a comparison.

    docker compose exec api python scripts/compare_models.py --data /data
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.quant.backtest_runner import build_forecasts
from app.quant.dixon_coles import (
    DEFAULT_RHO,
    RHO_BOUNDS,
    estimate_rho,
)
from app.quant.metrics import brier_score, calibration, log_loss
from app.quant.poisson import (
    LeagueAverages,
    expected_goals,
    team_strength,
)
from scripts.backtest import COMPETITIONS, load


def _score(forecasts: list, label: str) -> dict[str, float]:
    """Score a set of forecasts against the market."""
    model = [f.model_probability for f in forecasts]
    market = [f.market_probability for f in forecasts]
    outcomes = [f.outcome for f in forecasts]
    report = calibration(model, outcomes)
    return {
        "n": len(forecasts),
        "brier": brier_score(model, outcomes),
        "market_brier": brier_score(market, outcomes),
        "log_loss": log_loss(model, outcomes),
        "ece": report.expected_calibration_error,
        "floor": report.noise_floor,
    }


def _draw_analysis(forecasts: list) -> tuple[float, float]:
    """Return mean predicted draw probability and observed draw rate.

    Every third forecast is the draw outcome, in the order the harness emits
    home, draw, away.
    """
    draws = forecasts[1::3]
    if not draws:
        return 0.0, 0.0
    predicted = sum(f.model_probability for f in draws) / len(draws)
    observed = sum(f.outcome for f in draws) / len(draws)
    return predicted, observed


async def main() -> int:
    """Run both goal models and print the comparison."""
    parser = argparse.ArgumentParser(description="Compare goal models.")
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--competition", default="E1")
    parser.add_argument("--reference", default="market_avg")
    parser.add_argument("--seasons", type=int, default=9)
    parser.add_argument("--reliability", type=float, default=0.3)
    args = parser.parse_args()

    settings = get_settings()
    settings.observability.log_level = "ERROR"
    configure_logging(settings)

    seasons = [f"{y}/{y + 1}" for y in range(2026 - args.seasons, 2026)]
    matches = await load(args.data, args.competition, seasons)
    if len(matches) < 500:
        print("Not enough matches to compare.", file=sys.stderr)
        return 1

    name = COMPETITIONS.get(args.competition, (args.competition, ""))[0]
    print(f"\n{name} — {len(matches)} matches\n")

    # Fit rho on the actual scorelines, using rates from full-sample strengths.
    # This is an in-sample fit used only to choose a parameter, not to score.
    results = [(m.home_goals, m.away_goals) for m in matches]
    league = LeagueAverages(
        home_goals=sum(h for h, _ in results) / len(results),
        away_goals=sum(a for _, a in results) / len(results),
        matches=len(results),
    )
    by_team: dict[str, list[tuple[int, int]]] = {}
    for match in matches:
        by_team.setdefault(match.home_team.source_name, []).append(
            (match.home_goals, match.away_goals)
        )
        by_team.setdefault(match.away_team.source_name, []).append(
            (match.away_goals, match.home_goals)
        )

    observations = []
    for match in matches:
        home, away = match.home_team.source_name, match.away_team.source_name
        if home not in by_team or away not in by_team:
            continue
        home_strength = team_strength(0, by_team[home], [], league)
        away_strength = team_strength(0, by_team[away], [], league)
        lambda_home, lambda_away = expected_goals(home_strength, away_strength, league)
        observations.append((match.home_goals, match.away_goals, lambda_home, lambda_away))

    fit = estimate_rho(observations)
    print(
        f"rho fitted: {fit.rho:+.4f} on {fit.matches} matches "
        f"(default {DEFAULT_RHO:+.2f}, bounds {RHO_BOUNDS})"
    )
    print(
        "  meaningful correction"
        if fit.improves_on_independence
        else "  fitted value is near zero: the data does not support a correction"
    )
    print()

    rows = []
    for label, model, rho in (
        ("poisson", "poisson", DEFAULT_RHO),
        ("dixon-coles (default rho)", "dixon_coles", DEFAULT_RHO),
        ("dixon-coles (fitted rho)", "dixon_coles", fit.rho),
    ):
        forecasts, _ = build_forecasts(
            matches,
            reliability=args.reliability,
            reference_bookmaker=args.reference,
            goal_model=model,
            rho=rho,
        )
        if not forecasts:
            continue
        scores = _score(forecasts, label)
        predicted_draw, observed_draw = _draw_analysis(forecasts)
        rows.append((label, scores, predicted_draw, observed_draw))

    print(
        f"{'model':28}{'brier':>10}{'logloss':>10}{'skill':>10}"
        f"{'ece':>9}{'draw pred':>11}{'draw obs':>10}"
    )
    for label, scores, predicted_draw, observed_draw in rows:
        skill = (scores["market_brier"] - scores["brier"]) / scores["market_brier"]
        print(
            f"{label:28}{scores['brier']:>10.5f}{scores['log_loss']:>10.5f}"
            f"{skill:>+10.4%}{scores['ece']:>9.4f}"
            f"{predicted_draw:>11.4f}{observed_draw:>10.4f}"
        )

    if rows:
        market = rows[0][1]["market_brier"]
        print(f"\nmarket brier: {market:.5f} on {rows[0][1]['n']} forecasts")
        print(f"calibration noise floor: {rows[0][1]['floor']:.4f}")
        print(
            "\nDraw columns show mean predicted draw probability against the "
            "observed draw rate. Poisson under-predicting draws is the specific "
            "weakness Dixon-Coles exists to fix."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
