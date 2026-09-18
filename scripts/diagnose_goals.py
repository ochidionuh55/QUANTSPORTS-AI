#!/usr/bin/env python3
"""Why the goals markets are over-confident. Diagnosis, not correction.

**The finding this investigates.** Every competition that passed the 1X2 gate
shows negative service calibration gaps on goals markets. Liga Alef's
``Under 2.5`` predicted 63.9% and hit 53.2%; ``Draw or Under 2.5`` 67.3%
against 58.1%. The pattern repeats across all six, which makes it structural
rather than one league's noise.

**Rho is not assumed to be the cause.** The Dixon-Coles correction touches four
scorelines and cannot move a total-goals distribution far. This walks the
chain instead — expected goals, then the realised total, then the scoreline
distribution, then the market — and reports where predicted and observed first
part company.

**Two views, because selection conditions the answer.** A market can look
calibrated across all forecasts and be over-confident in the high-probability
tail, which is exactly where Best of the Day draws its selections from. Both
are reported: every forecast, and only those that would have qualified at the
current production threshold.

**Chronological throughout.** Same walk-forward construction as the validation
run, same production mathematics. Nothing is fitted here and nothing is
changed; this only measures.

Run::

    railway ssh "python scripts/diagnose_goals.py --leagues 496"
    railway ssh "python scripts/diagnose_goals.py --all"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models import HistoricalMatch
from app.database.models.team import Competition as CompetitionRow
from app.infrastructure.database import Database
from app.quant.dixon_coles import estimate_rho
from app.quant.grid import build_grid
from app.quant.markets import derive_markets
from app.quant.poisson import LeagueAverages, expected_goals, team_strength

WAVE_1: dict[int, str] = {
    382: "Liga Leumit",
    291: "Azadegan League",
    287: "Prva Liga",
    240: "Primera B",
    496: "Liga Alef",
    399: "NPFL",
    317: "1st League - RS",
    243: "Liga Pro Serie B",
}

MIN_HISTORY = 60
MIN_TEAM_MATCHES = 5

# The goals markets showing the gaps, with their production thresholds.
WATCHED: tuple[tuple[str, str, float], ...] = (
    ("Goals", "Over 1.5", 0.80),
    ("Goals", "Over 2.5", 0.62),
    ("Goals", "Under 2.5", 0.58),
    ("Goals", "Under 3.5", 0.78),
    ("Both teams to score", "Yes", 0.62),
    ("Both teams to score", "No", 0.58),
)


@dataclass
class Observation:
    """One walk-forward forecast, kept with everything needed to diagnose it."""

    lambda_home: float
    lambda_away: float
    home_goals: int
    away_goals: int
    market_probabilities: dict[tuple[str, str], float]

    @property
    def expected_total(self) -> float:
        return self.lambda_home + self.lambda_away

    @property
    def actual_total(self) -> int:
        return self.home_goals + self.away_goals


@dataclass
class Diagnosis:
    """Where predicted and observed diverge for one competition."""

    league_id: int
    label: str
    observations: list[Observation] = field(default_factory=list)
    rho: float = 0.0

    @property
    def count(self) -> int:
        return len(self.observations)

    @property
    def expected_goals_mean(self) -> float:
        if not self.observations:
            return 0.0
        return sum(o.expected_total for o in self.observations) / self.count

    @property
    def actual_goals_mean(self) -> float:
        if not self.observations:
            return 0.0
        return sum(o.actual_total for o in self.observations) / self.count

    def goal_distribution(self) -> list[tuple[str, float, float]]:
        """Predicted against observed share of matches by total goals.

        The Poisson grid's own total-goals distribution, summed from the same
        grid the markets come from. If this is wrong, every goals market
        inherits it.
        """
        if not self.observations:
            return []
        predicted: dict[int, float] = defaultdict(float)
        observed: dict[int, int] = defaultdict(int)
        for observation in self.observations:
            grid = build_grid(
                observation.lambda_home, observation.lambda_away, rho=self.rho
            )
            for (home, away), probability in grid.items():
                total = min(home + away, 5)
                predicted[total] += float(probability)
            observed[min(observation.actual_total, 5)] += 1
        rows = []
        for total in range(6):
            label = f"{total}+" if total == 5 else str(total)
            rows.append(
                (
                    label,
                    predicted[total] / self.count,
                    observed[total] / self.count,
                )
            )
        return rows

    def market_calibration(
        self, market: str, outcome: str, threshold: float | None = None
    ) -> tuple[int, float, float]:
        """Return sample, mean predicted and observed rate for one market.

        With a threshold, only forecasts that would have qualified are counted
        — the tail Best of the Day actually publishes from.
        """
        from app.quant.markets import settles_won

        sample = 0
        predicted = 0.0
        won = 0
        for observation in self.observations:
            probability = observation.market_probabilities.get((market, outcome))
            if probability is None:
                continue
            if threshold is not None and probability < threshold:
                continue
            sample += 1
            predicted += probability
            if settles_won(market, outcome, observation.home_goals, observation.away_goals):
                won += 1
        if not sample:
            return (0, 0.0, 0.0)
        return (sample, predicted / sample, won / sample)


async def diagnose(session: AsyncSession, league_id: int, label: str) -> Diagnosis:
    """Walk one competition forward, keeping the inputs to every forecast."""
    result = Diagnosis(league_id=league_id, label=label)

    found = await session.execute(
        select(CompetitionRow.id).where(CompetitionRow.canonical_name == label)
    )
    competition_id = found.scalar_one_or_none()
    if competition_id is None:
        return result

    rows = await session.execute(
        select(HistoricalMatch)
        .where(HistoricalMatch.competition_id == competition_id)
        .order_by(HistoricalMatch.match_date)
    )
    matches = list(rows.scalars().all())
    if len(matches) < MIN_HISTORY * 2:
        return result

    split = len(matches) // 2
    training: list[tuple[int, int, float, float]] = []
    scored_for: dict[int, list[tuple[int, int]]] = defaultdict(list)
    scored_against: dict[int, list[tuple[int, int]]] = defaultdict(list)
    league_home = league_away = played = 0

    for index, match in enumerate(matches):
        home_id, away_id = match.home_team_id, match.away_team_id
        home_goals, away_goals = match.home_goals, match.away_goals

        seen_home = len(scored_for[home_id]) + len(scored_against[home_id])
        seen_away = len(scored_for[away_id]) + len(scored_against[away_id])
        if (
            played >= MIN_HISTORY
            and seen_home >= MIN_TEAM_MATCHES
            and seen_away >= MIN_TEAM_MATCHES
        ):
            averages = LeagueAverages(
                home_goals=league_home / played,
                away_goals=league_away / played,
                matches=played,
            )
            if averages.is_reliable:
                home_strength = team_strength(
                    home_id, scored_for[home_id], scored_against[home_id], averages
                )
                away_strength = team_strength(
                    away_id, scored_for[away_id], scored_against[away_id], averages
                )
                if home_strength.is_reliable and away_strength.is_reliable:
                    lambda_home, lambda_away = expected_goals(
                        home_strength, away_strength, averages
                    )
                    if index < split:
                        training.append(
                            (home_goals, away_goals, lambda_home, lambda_away)
                        )
                    else:
                        if not result.observations:
                            result.rho = estimate_rho(training).rho
                        grid = build_grid(lambda_home, lambda_away, rho=result.rho)
                        markets = derive_markets(grid)
                        probabilities: dict[tuple[str, str], float] = {}
                        for market, outcome, _ in WATCHED:
                            value = markets.get(market, {}).get(outcome, Decimal(0))
                            probabilities[(market, outcome)] = float(value)
                        result.observations.append(
                            Observation(
                                lambda_home=lambda_home,
                                lambda_away=lambda_away,
                                home_goals=home_goals,
                                away_goals=away_goals,
                                market_probabilities=probabilities,
                            )
                        )

        scored_for[home_id].append((home_goals, away_goals))
        scored_against[away_id].append((away_goals, home_goals))
        league_home += home_goals
        league_away += away_goals
        played += 1

    return result


def report(result: Diagnosis) -> None:
    """Print where predicted and observed part company."""
    print("\n" + "=" * 96)
    print(f"{result.label} — goals-market diagnosis")
    print("=" * 96)

    if not result.observations:
        print("\n  No observations.")
        return

    expected = result.expected_goals_mean
    actual = result.actual_goals_mean
    print(f"\n  forecasts            : {result.count:,}")
    print(f"  fitted rho           : {result.rho:+.3f}")
    print("\n  STEP 1 — EXPECTED GOALS AGAINST REALISED GOALS")
    print(f"    mean expected total : {expected:.3f}")
    print(f"    mean actual total   : {actual:.3f}")
    print(f"    difference          : {expected - actual:+.3f}")
    if abs(expected - actual) > 0.10:
        print("    *** The rate model itself is off. Every goals market")
        print("    *** inherits this before any distribution is built.")
    else:
        print("    The rate model is close. The error is downstream of it.")

    print("\n  STEP 2 — TOTAL-GOALS DISTRIBUTION")
    print(f"    {'goals':<8}{'predicted':>11}{'observed':>11}{'gap':>9}")
    worst = 0.0
    for label, predicted, observed in result.goal_distribution():
        gap = predicted - observed
        worst = max(worst, abs(gap))
        print(f"    {label:<8}{predicted:>10.1%}{observed:>11.1%}{gap:>+9.1%}")
    if worst > 0.03:
        print(f"    *** Largest bucket gap {worst:.1%}. The shape of the")
        print("    *** distribution is wrong, not just its mean.")

    print("\n  STEP 3 — MARKETS, ALL FORECASTS")
    print(f"    {'market':<26}{'n':>7}{'pred':>8}{'obs':>8}{'gap':>8}")
    for market, outcome, _ in WATCHED:
        sample, predicted, observed = result.market_calibration(market, outcome)
        if not sample:
            continue
        print(
            f"    {f'{market} {outcome}'[:25]:<26}{sample:>7,}"
            f"{predicted:>8.1%}{observed:>8.1%}{predicted - observed:>+8.1%}"
        )

    print("\n  STEP 4 — MARKETS, ONLY WHERE THEY WOULD HAVE QUALIFIED")
    print("    The tail Best of the Day publishes from.")
    print(f"    {'market':<26}{'thr':>6}{'n':>7}{'pred':>8}{'obs':>8}{'gap':>8}")
    for market, outcome, threshold in WATCHED:
        sample, predicted, observed = result.market_calibration(
            market, outcome, threshold
        )
        if not sample:
            print(f"    {f'{market} {outcome}'[:25]:<26}{threshold:>6.2f}{0:>7}"
                  "     none qualified")
            continue
        flag = "  <<<" if abs(predicted - observed) > 0.05 else ""
        print(
            f"    {f'{market} {outcome}'[:25]:<26}{threshold:>6.2f}{sample:>7,}"
            f"{predicted:>8.1%}{observed:>8.1%}{predicted - observed:>+8.1%}{flag}"
        )

    print("\n  STEP 5 — WHERE THE ERROR ENTERS")
    if abs(expected - actual) > 0.10:
        print("    The rate model. Expected and realised goals disagree before")
        print("    any distribution is constructed, so correcting a market would")
        print("    be patching a symptom.")
    elif worst > 0.03:
        print("    The scoreline distribution. The rates are right and the shape")
        print("    is not — a variance assumption rather than a mean one.")
    else:
        all_gaps = [
            abs(result.market_calibration(m, o)[1] - result.market_calibration(m, o)[2])
            for m, o, _ in WATCHED
        ]
        tail_gaps = []
        for market, outcome, threshold in WATCHED:
            sample, predicted, observed = result.market_calibration(
                market, outcome, threshold
            )
            if sample >= 50:
                tail_gaps.append(abs(predicted - observed))
        if tail_gaps and max(tail_gaps) > 2 * (max(all_gaps) if all_gaps else 0):
            print("    Selection, not the model. Calibration holds across all")
            print("    forecasts and degrades in the qualifying tail, which means")
            print("    the threshold selects the cases the model overestimates.")
        else:
            print("    No single component dominates on this competition's evidence.")


async def main() -> int:
    """Run the diagnosis."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leagues", default="")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass

    chosen = dict(WAVE_1)
    if args.leagues:
        wanted = {int(x.strip()) for x in args.leagues.split(",") if x.strip().isdigit()}
        chosen = {k: v for k, v in WAVE_1.items() if k in wanted}
    elif not args.all:
        chosen = {496: WAVE_1[496]}

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        for league_id, label in chosen.items():
            print(f"\n  diagnosing {label} ...", flush=True)
            report(await diagnose(session, league_id, label))
        await session.rollback()
    await database.disconnect()

    print("\n" + "=" * 96)
    print("READ-ONLY. Diagnosis only: nothing fitted, corrected or activated.")
    print("No calibrator has been applied and no threshold changed.")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
