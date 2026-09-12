#!/usr/bin/env python3
"""Backtest the model-only pipeline, with no bookmaker input at all.

Answers the question the product now rests on: **do the models predict, on
their own, or only agree with a price they were shown?**

The existing backtest measures the published probability, which is the market
prior wherever odds exist. It therefore cannot answer this. Here the market is
never consulted — components are built from history alone, blended without a
prior, and every market is derived from the resulting scoreline distribution.
The market is loaded afterwards purely as a yardstick to measure against.

Chronological throughout: every forecast uses only matches that finished before
its own kickoff, so nothing can leak backwards.

    python scripts/backtest_model_only.py --divisions E0,E1,SP1 --seasons 6

Reports, per market, Brier score and log loss for the models and for the
market, plus calibration error and sample size. A model that is worse than the
market on Brier is not predictive in the sense that matters, however
sophisticated it looks.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.competitions import CSV_COMPETITIONS
from app.core.paths import data_dir
from app.quant.backtest_runner import (
    _team_id,
    _TeamHistory,
    components_and_goals,
)
from app.quant.elo import EloEngine
from app.quant.markets import MARKETS, derive_markets
from app.quant.poisson import score_matrix
from scripts.backtest import load

MIN_HISTORY = 20
"""Matches required before a forecast is made at all."""


@dataclass
class MarketScore:
    """Accuracy for one market, models against the market price."""

    market: str
    outcome: str
    n: int = 0
    wins: int = 0
    model_brier: float = 0.0
    model_logloss: float = 0.0
    market_brier: float = 0.0
    market_logloss: float = 0.0
    model_sum: float = 0.0
    market_sum: float = 0.0
    bands: dict[int, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))

    def observe(self, model: float, market: float | None, won: bool) -> None:
        """Record one settled forecast."""
        self.n += 1
        outcome = 1.0 if won else 0.0
        self.wins += int(won)
        self.model_sum += model
        self.model_brier += (model - outcome) ** 2
        self.model_logloss += -math.log(
            max(min(model, 1 - 1e-9), 1e-9) if won else max(min(1 - model, 1 - 1e-9), 1e-9)
        )

        band = min(9, int(model * 10))
        self.bands[band][0] += int(won)
        self.bands[band][1] += 1

        if market is not None:
            self.market_sum += market
            self.market_brier += (market - outcome) ** 2
            self.market_logloss += -math.log(
                max(min(market, 1 - 1e-9), 1e-9) if won else max(min(1 - market, 1 - 1e-9), 1e-9)
            )

    @property
    def skill(self) -> float | None:
        """Brier improvement over the market, as a percentage.

        Positive means the models are closer to the truth than the price.
        Negative means the price is better, which is the usual finding.
        """
        if not self.n or not self.market_brier:
            return None
        return (self.market_brier - self.model_brier) / self.market_brier * 100

    @property
    def calibration_error(self) -> float:
        """Mean gap between forecast and observed frequency, by band."""
        if not self.n:
            return 0.0
        total = 0.0
        for band, (won, played) in self.bands.items():
            if played < 20:
                continue
            forecast = (band + 0.5) / 10
            total += abs(won / played - forecast) * played
        return total / self.n

    def line(self) -> str:
        """Return one report row."""
        if not self.n:
            return f"{self.outcome:24} no data"
        skill = self.skill
        base = (
            f"{self.outcome:24} n={self.n:6}  "
            f"actual {self.wins / self.n:6.1%}  "
            f"model {self.model_sum / self.n:6.1%}  "
            f"brier {self.model_brier / self.n:.4f}  "
            f"ece {self.calibration_error:.4f}"
        )
        if skill is None:
            return f"{base}  skill: no market price in dataset"
        return f"{base}  vs mkt {self.market_brier / self.n:.4f}  skill {skill:+.2f}%"


async def run(divisions: list[str], seasons: list[str]) -> int:
    """Backtest every division and print the market matrix."""
    scores: dict[tuple[str, str], MarketScore] = {
        definition.key: MarketScore(definition.market, definition.outcome) for definition in MARKETS
    }
    directory = data_dir()
    total_matches = 0
    forecast_count = 0

    for code in divisions:
        if code not in CSV_COMPETITIONS:
            print(f"  unknown division {code}, skipping")
            continue

        matches = await load(directory, code, seasons)
        if not matches:
            continue
        matches.sort(key=lambda m: m.match_date)
        total_matches += len(matches)

        history = _TeamHistory()
        elo = EloEngine()

        for match in matches:
            home = match.home_team.source_name
            away = match.away_team.source_name
            kickoff = match.kickoff or datetime.combine(match.match_date, time(12, 0), tzinfo=UTC)
            components, goals = components_and_goals(history, elo, home, away, kickoff)

            if components and goals is not None and len(history.results) >= MIN_HISTORY:
                # Blend the components with no market prior anywhere in sight.
                blended = {
                    key: sum(
                        (component.probabilities[key] for component in components),
                        Decimal(0),
                    )
                    / len(components)
                    for key in ("home", "draw", "away")
                }
                total = sum(blended.values())
                result = {k: float(v / total) for k, v in blended.items()}

                # Scale the scoreline grid so its result probabilities match
                # the blended estimate, keeping one distribution behind every
                # market rather than two disagreeing sources.
                grid = score_matrix(goals.lambda_home, goals.lambda_away)
                model_markets = _rescaled_markets(grid, result)
                market_markets = _market_markets(match)

                for definition in MARKETS:
                    model = model_markets.get(definition.market, {}).get(definition.outcome)
                    if model is None:
                        continue
                    market = market_markets.get(definition.market, {}).get(definition.outcome)
                    scores[definition.key].observe(
                        float(model),
                        float(market) if market is not None else None,
                        definition.holds(match.home_goals, match.away_goals),
                    )
                forecast_count += 1

            history.record(match)
            elo.record(
                _team_id(home),
                _team_id(away),
                match.home_goals,
                match.away_goals,
                kickoff,
            )

        print(f"  {code}: {len(matches)} matches")

    _report(scores, total_matches, forecast_count)
    return 0


def _rescaled_markets(
    grid: dict[tuple[int, int], Decimal], result: dict[str, float]
) -> dict[str, dict[str, Decimal]]:
    """Derive markets from a grid tilted to the blended result estimate.

    The Poisson grid alone ignores Elo and form. Reweighting its result
    partitions to match the blend keeps every component's contribution while
    preserving one joint distribution, so combination markets stay exact.
    """
    weights: dict[str, Decimal] = {}
    for name, predicate in (
        ("home", lambda h, a: h > a),
        ("draw", lambda h, a: h == a),
        ("away", lambda h, a: h < a),
    ):
        mass = sum((p for (h, a), p in grid.items() if predicate(h, a)), Decimal(0))
        weights[name] = Decimal(str(result[name])) / mass if mass > 0 else Decimal(0)

    tilted = {
        (h, a): p * (weights["home"] if h > a else weights["draw"] if h == a else weights["away"])
        for (h, a), p in grid.items()
    }
    total = sum(tilted.values())
    if total <= 0:
        return derive_markets(grid)
    return derive_markets({k: v / total for k, v in tilted.items()})


# Markets for which a genuine bookmaker price exists in the dataset.
#
# The CSVs carry closing 1X2 odds and nothing else. Double chance follows
# exactly from those three prices, so both can be compared honestly. Totals,
# both-teams-to-score and every combination cannot: constructing a baseline for
# them from 1X2 prices alone would invent a weak opponent and then report
# beating it as skill, which is the most common way a backtest lies.
PRICED_MARKETS: frozenset[str] = frozenset({"1X2", "Double chance"})


def _market_markets(match: object) -> dict[str, dict[str, Decimal]]:
    """Return the bookmaker's own view, for comparison only.

    Restricted to markets the dataset actually prices. Never touches the model
    path.
    """
    quotes = getattr(match, "closing_odds", None) or {}
    prices = quotes.get("market_avg") or quotes.get("market_max")
    if not prices:
        return {}
    try:
        home = 1 / float(prices["H"])
        draw = 1 / float(prices["D"])
        away = 1 / float(prices["A"])
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return {}

    total = home + draw + away
    if total <= 0:
        return {}
    result = {"home": home / total, "draw": draw / total, "away": away / total}

    # A grid consistent with those prices: start neutral, tilt to match.
    grid = score_matrix(1.35, 1.15)
    priced = _rescaled_markets(grid, result)
    return {k: v for k, v in priced.items() if k in PRICED_MARKETS}


def _report(scores: dict[tuple[str, str], MarketScore], matches: int, forecasts: int) -> None:
    """Print the market performance matrix."""
    print(f"\n{matches:,} matches loaded, {forecasts:,} forecast\n")
    print("MARKET PERFORMANCE MATRIX — model-only, no bookmaker input")
    print("skill = Brier improvement over the market. Negative means the price wins.\n")

    by_market: dict[str, list[MarketScore]] = defaultdict(list)
    for score in scores.values():
        by_market[score.market].append(score)

    for market, entries in by_market.items():
        print(f"{market}")
        for entry in entries:
            print(f"  {entry.line()}")
        print()

    measured = [s for s in scores.values() if s.n and s.skill is not None]
    unmeasured = [s for s in scores.values() if s.n and s.skill is None]

    if measured:
        average = sum(s.skill or 0 for s in measured) / len(measured)
        beaten = [s for s in measured if (s.skill or 0) > 0]
        print("WHERE A REAL PRICE EXISTS TO COMPARE AGAINST")
        print(f"  Markets measured: {len(measured)}")
        print(f"  Average skill: {average:+.2f}%")
        print(f"  Models beat the price in {len(beaten)} of {len(measured)}")
        if beaten:
            print("  " + ", ".join(f"{s.outcome} ({s.skill:+.1f}%)" for s in beaten))

    if unmeasured:
        print(f"\n{len(unmeasured)} markets have no bookmaker price in this dataset.")
        print(
            "  Their calibration is reported above, but no skill claim can be "
            "made about them.\n  Comparing against a baseline built from 1X2 "
            "prices would invent a weak\n  opponent and report beating it as "
            "edge. Real totals prices are needed."
        )

    print(
        "\nCalibration says the numbers are honest. Skill says they are "
        "better than the price.\nOnly the second justifies selling anything "
        "as an edge."
    )


async def main() -> int:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(description="Model-only backtest.")
    parser.add_argument("--divisions", default="E0,E1,E2,SP1,I1,D1,F1,N1")
    parser.add_argument("--seasons", type=int, default=8)
    args = parser.parse_args()

    seasons = [f"{y}/{y + 1}" for y in range(2026 - args.seasons, 2026)]
    divisions = [d.strip() for d in args.divisions.split(",") if d.strip()]

    print(f"Model-only backtest: {len(divisions)} divisions, {len(seasons)} seasons\n")
    return await run(divisions, seasons)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
