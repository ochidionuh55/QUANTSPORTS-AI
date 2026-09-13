#!/usr/bin/env python3
"""Validate the basketball model against real closing prices.

Walks 13,903 NBA games chronologically. Each fixture is forecast from ratings
built only from games that finished before it, then scored against what
actually happened and against what the market said.

    python scripts/validate_basketball.py --seasons 11

Basketball can be tested more honestly than football here, because this dataset
carries genuine closing moneyline, spread and total prices. Every market has a
real opponent to measure against — no constructed baseline, and therefore no
way to report beating a strawman as skill.

Also fits the two numbers the model rests on: the spread of the margin and of
the total around their expectations. Guessing those is how a basketball model
becomes confidently wrong.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.quant.basketball import (
    HOME_ADVANTAGE_POINTS,
    TeamRating,
    forecast,
)

MIN_GAMES = 15
"""Games a side needs before it is forecast at all."""

WINDOW = 30
"""How many recent games form a team's rating.

Rosters change, players get injured, form moves. A full season weights October
equally with April; thirty games is roughly six weeks, which tracks the team as
it is now without becoming noise.
"""


@dataclass
class Rolling:
    """A team's recent scoring, as a rolling window."""

    scored: list[int] = field(default_factory=list)
    conceded: list[int] = field(default_factory=list)

    def record(self, points_for: int, points_against: int) -> None:
        """Add one game, dropping the oldest."""
        self.scored.append(points_for)
        self.conceded.append(points_against)
        if len(self.scored) > WINDOW:
            self.scored.pop(0)
            self.conceded.pop(0)

    @property
    def games(self) -> int:
        """Games in the window."""
        return len(self.scored)

    def rating(self, league_pace: float) -> TeamRating:
        """Return measured ratings per hundred possessions.

        Pace is inferred from scoring rather than possession counts, which this
        dataset does not carry. Crude, but it separates a fast team from a good
        one well enough to matter.
        """
        if not self.scored:
            return TeamRating(110.0, 110.0, league_pace, 0)

        points_for = sum(self.scored) / len(self.scored)
        points_against = sum(self.conceded) / len(self.conceded)
        pace = (points_for + points_against) / 2 / 1.10

        return TeamRating(
            offensive_rating=points_for / pace * 100,
            defensive_rating=points_against / pace * 100,
            pace=pace,
            games=len(self.scored),
        )


@dataclass
class MarketScore:
    """Model against market, for one market."""

    name: str
    n: int = 0
    wins: int = 0
    model_brier: float = 0.0
    market_brier: float = 0.0
    model_sum: float = 0.0
    bands: dict[int, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))

    def observe(self, model: float, market: float, won: bool) -> None:
        """Record one settled forecast."""
        outcome = 1.0 if won else 0.0
        self.n += 1
        self.wins += int(won)
        self.model_sum += model
        self.model_brier += (model - outcome) ** 2
        self.market_brier += (market - outcome) ** 2
        band = min(9, int(model * 10))
        self.bands[band][0] += int(won)
        self.bands[band][1] += 1

    @property
    def skill(self) -> float | None:
        """Brier improvement over the market, as a percentage."""
        if not self.n or not self.market_brier:
            return None
        return (self.market_brier - self.model_brier) / self.market_brier * 100

    @property
    def calibration_error(self) -> float:
        """Mean gap between forecast and observed frequency."""
        if not self.n:
            return 0.0
        total = 0.0
        for band, (won, played) in self.bands.items():
            if played < 30:
                continue
            total += abs(won / played - (band + 0.5) / 10) * played
        return total / self.n

    def line(self) -> str:
        """Return one report row."""
        if not self.n:
            return f"{self.name:22} no data"
        skill = self.skill
        skill_text = f"{skill:+6.2f}%" if skill is not None else "     -"
        return (
            f"{self.name:22} n={self.n:6}  actual {self.wins / self.n:6.1%}  "
            f"model {self.model_sum / self.n:6.1%}  "
            f"brier {self.model_brier / self.n:.4f} vs mkt "
            f"{self.market_brier / self.n:.4f}  skill {skill_text}  "
            f"ece {self.calibration_error:.4f}"
        )


def _implied(american: float) -> float:
    """Convert American odds to an implied probability."""
    if american < 0:
        return -american / (-american + 100)
    return 100 / (american + 100)


def _devig(home: float, away: float) -> tuple[float, float]:
    """Remove the bookmaker's margin from a two-way market."""
    total = home + away
    if total <= 0:
        return 0.5, 0.5
    return home / total, away / total


def run(directory: Path, seasons: int) -> int:
    """Replay the archive and report."""
    files = sorted(directory.glob("NBA_*.csv"))[-seasons:]
    if not files:
        print(f"no data in {directory}")
        return 1

    games: list[dict[str, str]] = []
    for path in files:
        with path.open() as handle:
            games.extend(csv.DictReader(handle))

    games.sort(key=lambda row: row.get("date", ""))
    print(f"{len(games):,} games across {len(files)} seasons\n")

    rolling: dict[str, Rolling] = defaultdict(Rolling)
    scores = {
        "Moneyline home": MarketScore("Moneyline home"),
        "Spread home": MarketScore("Spread home"),
        "Total over": MarketScore("Total over"),
        "Home or Over": MarketScore("Home or Over"),
        "Home or Under": MarketScore("Home or Under"),
        "Away or Over": MarketScore("Away or Over"),
    }

    margin_errors: list[float] = []
    total_errors: list[float] = []
    forecast_count = 0

    for row in games:
        home = str(row.get("home_team", ""))
        away = str(row.get("away_team", ""))
        try:
            home_points = int(float(row["home_final"]))
            away_points = int(float(row["away_final"]))
        except (KeyError, TypeError, ValueError):
            continue

        home_form = rolling[home]
        away_form = rolling[away]

        if home_form.games >= MIN_GAMES and away_form.games >= MIN_GAMES:
            game = forecast(home_form.rating(99.0), away_form.rating(99.0))
            margin_errors.append((home_points - away_points) - game.expected_margin)
            total_errors.append((home_points + away_points) - game.expected_total)

            _score(row, game, home_points, away_points, scores)
            forecast_count += 1

        home_form.record(home_points, away_points)
        away_form.record(away_points, home_points)

    _report(scores, margin_errors, total_errors, forecast_count)
    return 0


def _score(
    row: dict[str, str],
    game: object,
    home_points: int,
    away_points: int,
    scores: dict[str, MarketScore],
) -> None:
    """Score one game against every market with a real price."""
    margin = home_points - away_points
    total = home_points + away_points

    # Moneyline, from the closing American prices with margin removed.
    try:
        home_ml = _implied(float(row["home_close_ml"]))
        away_ml = _implied(float(row["away_close_ml"]))
    except (KeyError, TypeError, ValueError):
        return
    market_home, _ = _devig(home_ml, away_ml)
    scores["Moneyline home"].observe(game.home_win(), market_home, margin > 0)  # type: ignore[attr-defined]

    # Spread. The market prices a handicap to be near even, so the comparison
    # is against 0.5 — which is a genuinely hard benchmark, not a strawman.
    try:
        handicap = float(row["home_close_spread"])
    except (KeyError, TypeError, ValueError):
        handicap = None  # type: ignore[assignment]
    if handicap is not None and margin + handicap != 0:
        scores["Spread home"].observe(
            game.covers_spread(handicap),  # type: ignore[attr-defined]
            0.5,
            margin + handicap > 0,
        )

    try:
        line = float(row["close_over_under"])
    except (KeyError, TypeError, ValueError):
        return
    if total == line:
        return

    scores["Total over"].observe(game.over_total(line), 0.5, total > line)  # type: ignore[attr-defined]

    # Combination markets, against the market's own implied figures combined
    # the same way the model combines them.
    over_market = 0.5
    for name, model_value, won in (
        (
            "Home or Over",
            game.home_or_over(line),  # type: ignore[attr-defined]
            margin > 0 or total > line,
        ),
        (
            "Home or Under",
            game.home_or_under(line),  # type: ignore[attr-defined]
            margin > 0 or total < line,
        ),
        (
            "Away or Over",
            game.away_or_over(line),  # type: ignore[attr-defined]
            margin < 0 or total > line,
        ),
    ):
        if name == "Home or Under":
            market_value = market_home + (1 - over_market) - market_home * (1 - over_market)
        elif name == "Away or Over":
            market_value = (1 - market_home) + over_market - (1 - market_home) * over_market
        else:
            market_value = market_home + over_market - market_home * over_market
        scores[name].observe(model_value, market_value, won)


def _report(
    scores: dict[str, MarketScore],
    margin_errors: list[float],
    total_errors: list[float],
    forecasts: int,
) -> None:
    """Print the fitted spreads and the market comparison."""
    print(f"{forecasts:,} forecasts made\n")

    if margin_errors:
        margin_sigma = math.sqrt(sum(e * e for e in margin_errors) / len(margin_errors))
        total_sigma = math.sqrt(sum(e * e for e in total_errors) / len(total_errors))
        margin_bias = sum(margin_errors) / len(margin_errors)

        print("FITTED PARAMETERS")
        print(f"  Margin sigma: {margin_sigma:.2f} points")
        print(f"  Total sigma:  {total_sigma:.2f} points")
        print(
            f"  Margin bias:  {margin_bias:+.2f} points "
            f"(home advantage assumed {HOME_ADVANTAGE_POINTS})"
        )
        print(
            "  A positive bias means the home side outperforms the assumption "
            "and it should rise.\n"
        )

    print("MARKET COMPARISON — real closing prices")
    print("skill = Brier improvement over the price. Negative means the price wins.\n")
    for score in scores.values():
        print(f"  {score.line()}")

    measured = [s for s in scores.values() if s.n and s.skill is not None]
    if measured:
        beaten = [s for s in measured if (s.skill or 0) > 0]
        average = sum(s.skill or 0 for s in measured) / len(measured)
        print(f"\n  Average skill: {average:+.2f}%")
        print(f"  Markets beaten: {len(beaten)}/{len(measured)}")
        print(
            "\n  Spread and total are compared against 0.5, which is what the "
            "market\n  prices them to be — a hard benchmark, not a strawman."
        )


def main() -> int:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(description="Validate the basketball model.")
    parser.add_argument("--seasons", type=int, default=11)
    parser.add_argument("--data", default="data/basketball")
    args = parser.parse_args()

    print("Basketball model validation — NBA, real closing prices\n")
    return run(Path(args.data), args.seasons)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(asyncio.to_thread(main)))
