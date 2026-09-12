#!/usr/bin/env python3
"""Validate every Best of the Day service against history.

Replays the past day by day. On each date the engine sees only fixtures that
had not yet kicked off and only results that had already finished, picks its
selection for each service exactly as it would live, and the outcome is then
scored. Nothing is chosen with the result in view.

    python scripts/validate_services.py --divisions E0,E1,SP1,I1,D1 --seasons 6

Reports, per service: how often the selection won, what the model said it
would, the gap between them, and the sample size behind it.

**The gap is the number that matters.** A service winning 78% of the time is
meaningless on its own — if the model claimed 85%, it is overconfident and
users will lose money following it. A service winning 52% while claiming 50%
is more trustworthy, because it knows what it is.

No claim of edge is made or tested here. The models were already shown not to
beat bookmaker prices. This measures something narrower and still useful:
whether each service's own numbers can be believed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
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
from app.quant.poisson import score_matrix
from app.services.best_of_day import (
    SERVICES,
    BestOfDayEngine,
    ModelForecast,
    settles,
)
from scripts.backtest import load


@dataclass
class ServiceRecord:
    """One service's measured performance."""

    key: str
    label: str
    days: int = 0
    selections: int = 0
    won: int = 0
    expected: float = 0.0
    by_band: dict[int, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))

    def observe(self, probability: float, won: bool) -> None:
        """Record one settled selection."""
        self.selections += 1
        self.won += int(won)
        self.expected += probability
        band = min(9, int(probability * 10))
        self.by_band[band][0] += int(won)
        self.by_band[band][1] += 1

    @property
    def actual_rate(self) -> float | None:
        """Share of selections that won."""
        if not self.selections:
            return None
        return self.won / self.selections

    @property
    def expected_rate(self) -> float | None:
        """What the model said would happen."""
        if not self.selections:
            return None
        return self.expected / self.selections

    @property
    def gap(self) -> float | None:
        """Actual minus expected.

        Near zero means the service knows itself. Strongly negative means it
        promises more than it delivers, which is the dangerous direction.
        """
        actual = self.actual_rate
        expected = self.expected_rate
        if actual is None or expected is None:
            return None
        return actual - expected

    @property
    def verdict(self) -> str:
        """A plain reading of the evidence so far."""
        if self.selections < 100:
            return "INSUFFICIENT SAMPLE"
        gap = self.gap or 0.0
        if gap < -0.05:
            return "OVERCONFIDENT"
        if gap > 0.05:
            return "UNDERCONFIDENT"
        return "CALIBRATED"

    def line(self) -> str:
        """Return one report row."""
        if not self.selections:
            return f"{self.label:32} never qualified"
        actual = self.actual_rate or 0.0
        expected = self.expected_rate or 0.0
        gap = self.gap or 0.0
        return (
            f"{self.label:32} {self.won:4}/{self.selections:<5} "
            f"({actual:5.1%})  said {expected:5.1%}  "
            f"gap {gap:+5.1%}  {self.verdict}"
        )


async def run(divisions: list[str], seasons: list[str], top: int) -> int:
    """Replay history and measure every service."""
    directory = data_dir()
    records = {service.key: ServiceRecord(service.key, service.label) for service in SERVICES}
    engine = BestOfDayEngine()

    # All divisions walked together, so a day's selection competes across every
    # competition exactly as it will in production.
    everything: list[tuple[object, str]] = []
    for code in divisions:
        if code not in CSV_COMPETITIONS:
            continue
        matches = await load(directory, code, seasons)
        everything.extend((match, code) for match in matches)
        print(f"  {code}: {len(matches)} matches")

    if not everything:
        print("no data")
        return 1

    everything.sort(key=lambda pair: pair[0].match_date)  # type: ignore[attr-defined]

    # Per-division history: a team's record belongs to its own competition.
    history: dict[str, _TeamHistory] = defaultdict(_TeamHistory)
    elo: dict[str, EloEngine] = defaultdict(EloEngine)

    by_day: dict[date, list[tuple[object, str]]] = defaultdict(list)
    for match, code in everything:
        by_day[match.match_date].append((match, code))  # type: ignore[attr-defined]

    days_run = 0
    for day in sorted(by_day):
        fixtures = by_day[day]

        # Forecast the whole day before recording any of its results.
        forecasts: list[ModelForecast] = []
        for match, code in fixtures:
            forecast = _forecast(match, code, history[code], elo[code])
            if forecast is not None:
                forecasts.append(forecast)

        if forecasts:
            days_run += 1
            results = {
                _fixture_id(match, code): (match.home_goals, match.away_goals)  # type: ignore[attr-defined]
                for match, code in fixtures
            }
            ranked = engine.rank(forecasts, limit=top)
            for key, selections in ranked.items():
                if selections:
                    records[key].days += 1
                for selection in selections:
                    score = results.get(selection.forecast.fixture_id)
                    if score is None:
                        continue
                    won = settles(key, *score)
                    if won is None:
                        continue
                    records[key].observe(selection.probability, won)

        # Only now does the day's history become visible.
        for match, code in fixtures:
            kickoff = _kickoff(match)
            history[code].record(match)  # type: ignore[arg-type]
            elo[code].record(
                _team_id(match.home_team.source_name),  # type: ignore[attr-defined]
                _team_id(match.away_team.source_name),  # type: ignore[attr-defined]
                match.home_goals,  # type: ignore[attr-defined]
                match.away_goals,  # type: ignore[attr-defined]
                kickoff,
            )

    _report(records, days_run, top)
    return 0


def _kickoff(match: object) -> datetime:
    """Return a usable kickoff time."""
    existing = getattr(match, "kickoff", None)
    if existing is not None:
        return existing
    return datetime.combine(match.match_date, time(12, 0), tzinfo=UTC)  # type: ignore[attr-defined]


def _fixture_id(match: object, code: str) -> str:
    """Return a stable identifier for a historical fixture."""
    return f"{code}:{match.match_date}:{match.home_team.source_name}"  # type: ignore[attr-defined]


def _forecast(
    match: object, code: str, history: _TeamHistory, elo: EloEngine
) -> ModelForecast | None:
    """Build one model-only forecast, or ``None`` if the data will not support it."""
    home = match.home_team.source_name  # type: ignore[attr-defined]
    away = match.away_team.source_name  # type: ignore[attr-defined]
    kickoff = _kickoff(match)

    components, goals = components_and_goals(history, elo, home, away, kickoff)
    if not components or goals is None:
        return None

    blended = {
        key: sum((c.probabilities[key] for c in components), Decimal(0)) / len(components)
        for key in ("home", "draw", "away")
    }
    total = sum(blended.values())
    if total <= 0:
        return None
    result = {k: float(v / total) for k, v in blended.items()}

    grid = _tilted(score_matrix(goals.lambda_home, goals.lambda_away), result)
    return ModelForecast(
        fixture_id=_fixture_id(match, code),
        home_name=home,
        away_name=away,
        competition=CSV_COMPETITIONS[code][0],
        kickoff=kickoff,
        grid=grid,
        components=tuple(c.name for c in components),
        component_results=tuple(c.probabilities for c in components),
        home_matches=history.played(home),
        away_matches=history.played(away),
        coverage="fully_modelled" if len(components) >= 3 else "partially_modelled",
    )


def _tilted(
    grid: dict[tuple[int, int], Decimal], result: dict[str, float]
) -> dict[tuple[int, int], Decimal]:
    """Reweight a scoreline grid so its result matches the blended estimate.

    Keeps Elo and form in the distribution that every market derives from,
    rather than letting Poisson alone decide the goals markets while the others
    decide the result.
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
    return {k: v / total for k, v in tilted.items()} if total > 0 else grid


def _report(records: dict[str, ServiceRecord], days: int, top: int) -> None:
    """Print per-service performance."""
    print(f"\n{days:,} days replayed, top {top} selection(s) per service per day\n")
    print("BEST OF THE DAY — SERVICE PERFORMANCE")
    print("gap = actual minus what the model said. Near zero is the goal.\n")

    for record in records.values():
        print(f"  {record.line()}")

    measured = [r for r in records.values() if r.selections >= 100]
    if not measured:
        print("\nNo service reached a hundred selections; nothing can be judged yet.")
        return

    calibrated = [r for r in measured if r.verdict == "CALIBRATED"]
    overconfident = [r for r in measured if r.verdict == "OVERCONFIDENT"]

    print(f"\n{len(measured)} services with a usable sample.")
    print(f"  Calibrated (within 5 points): {len(calibrated)}")
    if calibrated:
        print("    " + ", ".join(r.label for r in calibrated))
    print(f"  Overconfident: {len(overconfident)}")
    if overconfident:
        print("    " + ", ".join(f"{r.label} ({r.gap:+.1%})" for r in overconfident))

    print(
        "\nCalibration means the numbers are honest, not that following them "
        "is profitable.\nA service winning at exactly its stated rate still "
        "loses money at market prices."
    )


async def main() -> int:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(description="Validate Best of the Day services.")
    parser.add_argument("--divisions", default="E0,E1,SP1,I1,D1")
    parser.add_argument("--seasons", type=int, default=6)
    parser.add_argument("--top", type=int, default=1)
    args = parser.parse_args()

    seasons = [f"{y}/{y + 1}" for y in range(2026 - args.seasons, 2026)]
    divisions = [d.strip() for d in args.divisions.split(",") if d.strip()]
    print(f"Validating {len(SERVICES)} services over {len(seasons)} seasons\n")
    return await run(divisions, seasons, args.top)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
