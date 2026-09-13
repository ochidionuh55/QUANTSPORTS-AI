#!/usr/bin/env python3
"""Test the basketball model on seasons it was never fitted to.

The parameters — margin spread, total spread, home advantage — were fitted on
2011 to 2022. This runs the model unchanged against 2022 to 2026, seasons it
has never seen, from a separate source scraped by different people.

    python scripts/holdout_basketball.py

**What this can settle.** Whether the model still forecasts margins and totals
accurately, and whether it still tracks the moneyline market, in the years
after US legalisation made sportsbooks sharper. If accuracy has collapsed, any
edge found in the older data is gone and nothing more needs testing.

**What this cannot settle.** The promising result was on spreads and totals,
and this dataset carries neither — only moneyline. So a good result here is
necessary for the edge to be real, but not sufficient to prove it. Saying
otherwise would be substituting an easier test and implying it answered the
harder question.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.quant.basketball import (
    MARGIN_SIGMA,
    TOTAL_SIGMA,
    forecast,
)
from scripts.validate_basketball import MIN_GAMES, Rolling


@dataclass
class SeasonResult:
    """One season's out-of-sample accuracy."""

    season: str
    n: int = 0
    margin_errors: list[float] = field(default_factory=list)
    total_errors: list[float] = field(default_factory=list)
    moneyline_correct: int = 0
    moneyline_n: int = 0
    model_brier: float = 0.0
    market_brier: float = 0.0
    model_sum: float = 0.0
    wins: int = 0

    @property
    def margin_rmse(self) -> float:
        """Typical error in predicting the margin."""
        if not self.margin_errors:
            return 0.0
        return math.sqrt(sum(e * e for e in self.margin_errors) / len(self.margin_errors))

    @property
    def total_rmse(self) -> float:
        """Typical error in predicting the combined score."""
        if not self.total_errors:
            return 0.0
        return math.sqrt(sum(e * e for e in self.total_errors) / len(self.total_errors))

    @property
    def skill(self) -> float | None:
        """Brier improvement over the moneyline price."""
        if not self.moneyline_n or not self.market_brier:
            return None
        return (self.market_brier - self.model_brier) / self.market_brier * 100

    def line(self) -> str:
        """Return one report row."""
        if not self.moneyline_n:
            return f"{self.season:12} no data"
        skill = self.skill or 0.0
        return (
            f"{self.season:12} n={self.moneyline_n:5}  "
            f"margin rmse {self.margin_rmse:5.2f}  total rmse {self.total_rmse:5.2f}  "
            f"ML picked right {self.moneyline_correct / self.moneyline_n:5.1%}  "
            f"skill {skill:+6.2f}%"
        )


def _implied(decimal_odds: float) -> float:
    """Convert decimal odds to an implied probability."""
    return 1.0 / decimal_odds if decimal_odds > 0 else 0.0


def run(directory: Path) -> int:
    """Replay the holdout seasons and report."""
    files = sorted(directory.glob("NBA_*.csv"))
    if not files:
        print(f"no holdout data in {directory}")
        return 1

    rolling: dict[str, Rolling] = defaultdict(Rolling)
    results: dict[str, SeasonResult] = {}

    for path in files:
        season = path.stem.replace("NBA_", "")
        result = results.setdefault(season, SeasonResult(season))

        rows = list(csv.DictReader(path.open()))
        rows.sort(key=lambda row: row.get("date", ""))

        for row in rows:
            home = str(row.get("home_team", ""))
            away = str(row.get("away_team", ""))
            try:
                home_points = int(row["home_score"])
                away_points = int(row["away_score"])
            except (KeyError, TypeError, ValueError):
                continue

            home_form = rolling[home]
            away_form = rolling[away]

            if home_form.games >= MIN_GAMES and away_form.games >= MIN_GAMES:
                game = forecast(home_form.rating(99.0), away_form.rating(99.0))
                margin = home_points - away_points
                total = home_points + away_points

                result.n += 1
                result.margin_errors.append(margin - game.expected_margin)
                result.total_errors.append(total - game.expected_total)

                try:
                    home_implied = _implied(float(row["home_odds"]))
                    away_implied = _implied(float(row["away_odds"]))
                except (KeyError, TypeError, ValueError):
                    home_form.record(home_points, away_points)
                    away_form.record(away_points, home_points)
                    continue

                overround = home_implied + away_implied
                if overround <= 0:
                    home_form.record(home_points, away_points)
                    away_form.record(away_points, home_points)
                    continue
                market = home_implied / overround

                model = game.home_win()
                won = margin > 0
                outcome = 1.0 if won else 0.0

                result.moneyline_n += 1
                result.wins += int(won)
                result.model_sum += model
                result.model_brier += (model - outcome) ** 2
                result.market_brier += (market - outcome) ** 2
                if (model >= 0.5) == won:
                    result.moneyline_correct += 1

            home_form.record(home_points, away_points)
            away_form.record(away_points, home_points)

    _report(list(results.values()))
    return 0


def _report(results: list[SeasonResult]) -> None:
    """Print the holdout outcome."""
    print("HOLDOUT — seasons the model was never fitted to\n")
    print(f"Fitted on 2011-2022: margin sigma {MARGIN_SIGMA}, " f"total sigma {TOTAL_SIGMA}\n")

    for result in sorted(results, key=lambda r: r.season):
        print(f"  {result.line()}")

    total_n = sum(r.moneyline_n for r in results)
    if not total_n:
        print("\nNo comparable games.")
        return

    margin_errors = [e for r in results for e in r.margin_errors]
    total_errors = [e for r in results for e in r.total_errors]
    margin_rmse = math.sqrt(sum(e * e for e in margin_errors) / len(margin_errors))
    total_rmse = math.sqrt(sum(e * e for e in total_errors) / len(total_errors))
    correct = sum(r.moneyline_correct for r in results)
    model_brier = sum(r.model_brier for r in results) / total_n
    market_brier = sum(r.market_brier for r in results) / total_n
    skill = (market_brier - model_brier) / market_brier * 100

    print(f"\n  OVERALL  n={total_n:,}")
    print(f"    Margin RMSE: {margin_rmse:.2f}  (fitted sigma was {MARGIN_SIGMA})")
    print(f"    Total RMSE:  {total_rmse:.2f}  (fitted sigma was {TOTAL_SIGMA})")
    print(f"    Moneyline picked right: {correct / total_n:.2%}")
    print(f"    Brier {model_brier:.4f} vs market {market_brier:.4f} — skill {skill:+.2f}%")

    print("\n  READING THIS")
    if margin_rmse > MARGIN_SIGMA * 1.08:
        print(
            "    Margin accuracy has degraded materially. Any edge measured on "
            "the older\n    seasons should be treated as gone."
        )
    else:
        print(
            "    Margin accuracy holds on unseen seasons, so the model has not "
            "decayed.\n    That is necessary for the spread edge to be real — "
            "but not sufficient."
        )
    print(
        "\n    This dataset carries no spread or total lines, so the promising "
        "result\n    remains untested out of sample. A source with closing "
        "spreads for\n    2022-2026 is what would settle it."
    )


def main() -> int:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(description="Basketball holdout test.")
    parser.add_argument("--data", default="data/basketball/holdout")
    args = parser.parse_args()
    return run(Path(args.data))


if __name__ == "__main__":
    raise SystemExit(main())
