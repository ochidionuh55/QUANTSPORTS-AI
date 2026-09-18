#!/usr/bin/env python3
"""Fit and validate Wave 1 competitions using production mathematics.

**One probability engine.** Every number here comes from
``app.quant.grid.build_match_probabilities`` — the same call the live scanner
makes — fed by ``team_strength`` and ``expected_goals`` from
``app.quant.poisson``. A validation harness that reimplements the model
measures the reimplementation, which is how an earlier audit reported a
fourteen-point home bias that did not exist in production.

**Strictly chronological.** For every fixture, team strengths and the
competition's rho are estimated from matches that finished before it. Nothing
downstream of a fixture's date informs its forecast. There is no random split
and no holdout drawn from the middle.

**The gate is written before the results are seen.** ``GATE`` below states the
criteria in the file, not in a decision made after looking at which competition
performed best. A competition that fails is withheld with its numbers attached;
it is not re-examined against a softer rule.

**Services are evaluated at the thresholds already in production.** Nothing is
tuned against these eight competitions. A service that qualifies rarely here
qualifies rarely; that is information, not a problem to fix.

**It does not activate anything.** Validation produces evidence and a verdict.
Adding a competition to the live scan is a separate, deliberate step.

Run::

    railway ssh "python scripts/validate_wave1.py --leagues 382"
    railway ssh "python scripts/validate_wave1.py --all"
    railway ssh "python scripts/validate_wave1.py --summary"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models import HistoricalMatch
from app.database.models.team import Competition as CompetitionRow
from app.infrastructure.database import Database
from app.quant.dixon_coles import estimate_rho
from app.quant.grid import build_grid, build_match_probabilities, grid_version
from app.quant.markets import derive_markets, settles_won
from app.quant.poisson import LeagueAverages, expected_goals, team_strength
from app.services.best_of_day import SERVICES

RESULTS_PATH = Path("/tmp/quantsport_wave1_validation.json")

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
"""League matches required before any forecast is scored.

The same warm-up the existing audit uses. Below it the league baseline is not
estimated from enough football to mean anything.
"""

MIN_TEAM_MATCHES = 5

OUTCOMES = ("home", "draw", "away")


@dataclass(frozen=True)
class Gate:
    """PASS criteria, fixed before any competition is examined.

    Deliberately stated as data in the file rather than decided from results.
    The bar is calibration, not accuracy: a model that says 40% and is right
    40% of the time is doing its job, and one that beats a coin flip by luck
    on a small sample is not.
    """

    min_forecasts: int = 800
    """Below this the metrics are too noisy to distinguish skill from chance."""

    max_brier: float = 0.68
    """Multiclass Brier. Uninformative 1/3-1/3-1/3 guessing scores 0.667, so
    this admits a model no worse than uninformative and rejects one that is
    actively misleading."""

    max_log_loss: float = 1.15
    """Uniform guessing gives ln(3) = 1.0986. A model above this is assigning
    confident probabilities to things that do not happen."""

    max_ece: float = 0.06
    """Expected calibration error. The published number must mean what it says."""

    max_outcome_bias: float = 0.06
    """Largest permitted gap between predicted and observed rate for home,
    draw or away. A systematic six-point bias would propagate into every
    derived market."""

    max_window_spread: float = 0.08
    """Brier range across chronological quarters. A model that works in one
    period and not another has not been shown to work."""


GATE = Gate()


@dataclass
class Forecast:
    """One walk-forward forecast and the result that followed."""

    match_date: date
    probabilities: dict[str, float]
    actual: str
    home_goals: int
    away_goals: int
    lambda_home: float
    lambda_away: float


@dataclass
class ServiceResult:
    """One service's behaviour at its production threshold."""

    key: str
    label: str
    threshold: float
    opportunities: int = 0
    qualifying: int = 0
    won: int = 0
    predicted_sum: float = 0.0

    @property
    def hit_rate(self) -> float | None:
        return self.won / self.qualifying if self.qualifying else None

    @property
    def predicted(self) -> float | None:
        return self.predicted_sum / self.qualifying if self.qualifying else None

    @property
    def calibration_gap(self) -> float | None:
        hit = self.hit_rate
        predicted = self.predicted
        if hit is None or predicted is None:
            return None
        return hit - predicted


@dataclass
class Validation:
    """Everything measured for one competition."""

    league_id: int
    label: str
    forecasts: list[Forecast] = field(default_factory=list)
    rho: float | None = None
    rho_fitted: bool = False
    training_rows: int = 0
    services: dict[str, ServiceResult] = field(default_factory=dict)
    grid_version: str = ""

    @property
    def count(self) -> int:
        return len(self.forecasts)

    @property
    def brier(self) -> float:
        if not self.forecasts:
            return 0.0
        total = 0.0
        for forecast in self.forecasts:
            for outcome in OUTCOMES:
                actual = 1.0 if forecast.actual == outcome else 0.0
                total += (forecast.probabilities[outcome] - actual) ** 2
        return total / len(self.forecasts)

    @property
    def log_loss(self) -> float:
        if not self.forecasts:
            return 0.0
        total = 0.0
        for forecast in self.forecasts:
            total -= math.log(max(forecast.probabilities[forecast.actual], 1e-15))
        return total / len(self.forecasts)

    def rates(self, outcome: str) -> tuple[float, float, float]:
        """Return predicted, observed and the gap for one outcome."""
        if not self.forecasts:
            return (0.0, 0.0, 0.0)
        predicted = sum(f.probabilities[outcome] for f in self.forecasts) / self.count
        observed = sum(1 for f in self.forecasts if f.actual == outcome) / self.count
        return (predicted, observed, predicted - observed)

    @property
    def max_bias(self) -> float:
        return max(abs(self.rates(outcome)[2]) for outcome in OUTCOMES)

    def bands(self, bins: int = 10) -> list[tuple[str, int, float, float]]:
        """Calibration by probability band."""
        buckets: dict[int, list[float]] = defaultdict(list)
        hits: dict[int, int] = defaultdict(int)
        for forecast in self.forecasts:
            for outcome in OUTCOMES:
                probability = forecast.probabilities[outcome]
                index = min(int(probability * bins), bins - 1)
                buckets[index].append(probability)
                hits[index] += 1 if forecast.actual == outcome else 0
        rows = []
        for index in sorted(buckets):
            values = buckets[index]
            rows.append(
                (
                    f"{index / bins:.0%}-{(index + 1) / bins:.0%}",
                    len(values),
                    sum(values) / len(values),
                    hits[index] / len(values),
                )
            )
        return rows

    @property
    def ece(self) -> float:
        total = sum(row[1] for row in self.bands())
        if not total:
            return 0.0
        return sum(row[1] / total * abs(row[2] - row[3]) for row in self.bands())

    def windows(self, count: int = 4) -> list[tuple[str, int, float]]:
        """Brier per chronological quarter."""
        ordered = sorted(self.forecasts, key=lambda f: f.match_date)
        if len(ordered) < count:
            return []
        size = len(ordered) // count
        rows = []
        for index in range(count):
            start = index * size
            chunk = ordered[start : start + size] if index < count - 1 else ordered[start:]
            if not chunk:
                continue
            total = 0.0
            for forecast in chunk:
                for outcome in OUTCOMES:
                    actual = 1.0 if forecast.actual == outcome else 0.0
                    total += (forecast.probabilities[outcome] - actual) ** 2
            rows.append(
                (
                    f"{chunk[0].match_date:%Y-%m} to {chunk[-1].match_date:%Y-%m}",
                    len(chunk),
                    total / len(chunk),
                )
            )
        return rows

    @property
    def window_spread(self) -> float:
        rows = self.windows()
        if len(rows) < 2:
            return 0.0
        scores = [row[2] for row in rows]
        return max(scores) - min(scores)

    def verdict(self) -> tuple[str, list[str]]:
        """Apply the gate. Returns the state and every reason it failed."""
        reasons: list[str] = []
        if self.count < GATE.min_forecasts:
            reasons.append(
                f"{self.count} forecasts, below the {GATE.min_forecasts} minimum"
            )
        if self.brier > GATE.max_brier:
            reasons.append(f"Brier {self.brier:.4f} above {GATE.max_brier}")
        if self.log_loss > GATE.max_log_loss:
            reasons.append(f"log loss {self.log_loss:.4f} above {GATE.max_log_loss}")
        if self.ece > GATE.max_ece:
            reasons.append(f"ECE {self.ece:.4f} above {GATE.max_ece}")
        if self.max_bias > GATE.max_outcome_bias:
            reasons.append(
                f"outcome bias {self.max_bias:.1%} above {GATE.max_outcome_bias:.0%}"
            )
        if self.window_spread > GATE.max_window_spread:
            reasons.append(
                f"Brier varies {self.window_spread:.4f} across windows, "
                f"above {GATE.max_window_spread}"
            )
        return ("ACTIVE" if not reasons else "WITHHELD", reasons)


async def _competition_id(session: AsyncSession, label: str) -> int | None:
    """Resolve our competition row for a Wave 1 label."""
    found = await session.execute(
        select(CompetitionRow.id).where(CompetitionRow.canonical_name == label)
    )
    return found.scalar_one_or_none()


async def validate(session: AsyncSession, league_id: int, label: str) -> Validation:
    """Walk one competition's history forward, forecasting each fixture."""
    result = Validation(league_id=league_id, label=label, grid_version=grid_version())

    competition_id = await _competition_id(session, label)
    if competition_id is None:
        return result

    rows = await session.execute(
        select(HistoricalMatch)
        .where(HistoricalMatch.competition_id == competition_id)
        .order_by(HistoricalMatch.match_date)
    )
    matches = list(rows.scalars().all())
    result.training_rows = len(matches)
    if len(matches) < MIN_HISTORY * 2:
        return result

    # Rho is fitted on the first half only, then applied to the second. Fitting
    # it on everything and validating on the same matches would let the
    # parameter see its own test set.
    split = len(matches) // 2
    observations: list[tuple[int, int, float, float]] = []

    scored_for: dict[int, list[tuple[int, int]]] = defaultdict(list)
    scored_against: dict[int, list[tuple[int, int]]] = defaultdict(list)
    league_home = league_away = played = 0

    for index, match in enumerate(matches):
        home_id = match.home_team_id
        away_id = match.away_team_id
        home_goals = match.home_goals
        away_goals = match.away_goals

        seen_home = len(scored_for[home_id]) + len(scored_against[home_id])
        seen_away = len(scored_for[away_id]) + len(scored_against[away_id])
        eligible = (
            played >= MIN_HISTORY
            and seen_home >= MIN_TEAM_MATCHES
            and seen_away >= MIN_TEAM_MATCHES
        )

        if eligible:
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
                        # Training half: contribute to the rho fit, do not score.
                        observations.append(
                            (home_goals, away_goals, lambda_home, lambda_away)
                        )
                    else:
                        if result.rho is None:
                            fit = estimate_rho(observations)
                            result.rho = fit.rho
                            result.rho_fitted = bool(fit.fitted)

                        # Production mathematics, and the competition's own rho.
                        probabilities = build_match_probabilities(
                            lambda_home,
                            lambda_away,
                            rho=result.rho,
                            corrected=result.rho_fitted,
                        )
                        actual = (
                            "home"
                            if home_goals > away_goals
                            else ("away" if away_goals > home_goals else "draw")
                        )
                        result.forecasts.append(
                            Forecast(
                                match_date=match.match_date,
                                probabilities={
                                    "home": float(probabilities.home_win),
                                    "draw": float(probabilities.draw),
                                    "away": float(probabilities.away_win),
                                },
                                actual=actual,
                                home_goals=home_goals,
                                away_goals=away_goals,
                                lambda_home=lambda_home,
                                lambda_away=lambda_away,
                            )
                        )

                        # Services, from the same grid, at production thresholds.
                        grid = build_grid(
                            lambda_home,
                            lambda_away,
                            rho=result.rho,
                            corrected=result.rho_fitted,
                        )
                        markets = derive_markets(grid)
                        for service in SERVICES:
                            entry = result.services.setdefault(
                                service.key,
                                ServiceResult(
                                    key=service.key,
                                    label=service.label,
                                    threshold=service.min_probability,
                                ),
                            )
                            entry.opportunities += 1
                            probability = float(
                                markets[service.market][service.outcome]
                            )
                            if probability >= service.min_probability:
                                entry.qualifying += 1
                                entry.predicted_sum += probability
                                if settles_won(
                                    service.market,
                                    service.outcome,
                                    home_goals,
                                    away_goals,
                                ):
                                    entry.won += 1

        scored_for[home_id].append((home_goals, away_goals))
        scored_against[away_id].append((away_goals, home_goals))
        league_home += home_goals
        league_away += away_goals
        played += 1

    return result


def _persist(result: Validation) -> None:
    """Keep validation evidence; a later model change is compared, not lost."""
    try:
        store: dict[str, Any] = {}
        if RESULTS_PATH.exists():
            store = dict(json.loads(RESULTS_PATH.read_text(encoding="utf-8")))
        state, reasons = result.verdict()
        store[str(result.league_id)] = {
            "label": result.label,
            "training_rows": result.training_rows,
            "forecasts": result.count,
            "rho": result.rho,
            "rho_fitted": result.rho_fitted,
            "grid_version": result.grid_version,
            "brier": result.brier,
            "log_loss": result.log_loss,
            "ece": result.ece,
            "max_bias": result.max_bias,
            "window_spread": result.window_spread,
            "state": state,
            "reasons": reasons,
            "rates": {o: result.rates(o) for o in OUTCOMES},
            "services": {
                key: {
                    "label": service.label,
                    "threshold": service.threshold,
                    "opportunities": service.opportunities,
                    "qualifying": service.qualifying,
                    "won": service.won,
                    "hit_rate": service.hit_rate,
                    "predicted": service.predicted,
                }
                for key, service in result.services.items()
            },
        }
        RESULTS_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")
    except OSError as error:
        print(f"  (could not persist: {error})", flush=True)


def report(result: Validation) -> None:
    """Print one competition's evidence."""
    state, reasons = result.verdict()
    print("\n" + "=" * 96)
    print(f"{result.label} — {state}")
    print("=" * 96)
    print(f"\n  training rows   : {result.training_rows:,}")
    print(f"  forecasts       : {result.count:,}  (second half, walk-forward)")
    print(f"  fitted rho      : {result.rho if result.rho is not None else '-'}"
          f"  (fitted on the first half only)")
    print(f"  grid version    : {result.grid_version}")

    if not result.forecasts:
        print("\n  No forecasts produced. Nothing to validate.")
        return

    print(f"\n  Brier           : {result.brier:.6f}   (gate {GATE.max_brier})")
    print(f"  log loss        : {result.log_loss:.6f}   (gate {GATE.max_log_loss})")
    print(f"  ECE             : {result.ece:.6f}   (gate {GATE.max_ece})")
    print(f"  window spread   : {result.window_spread:.6f}   (gate {GATE.max_window_spread})")

    print("\n  OUTCOME CALIBRATION")
    print(f"  {'outcome':<8}{'predicted':>11}{'observed':>11}{'bias':>9}")
    for outcome in OUTCOMES:
        predicted, observed, bias = result.rates(outcome)
        print(f"  {outcome:<8}{predicted:>10.1%}{observed:>11.1%}{bias:>+9.1%}")

    print("\n  CHRONOLOGICAL WINDOWS")
    for label, count, brier in result.windows():
        print(f"    {label:<22}{count:>7,}  Brier {brier:.5f}")

    qualifying = [s for s in result.services.values() if s.qualifying]
    if qualifying:
        print("\n  SERVICES AT PRODUCTION THRESHOLDS (unchanged)")
        print(f"  {'service':<32}{'thr':>6}{'qual':>7}{'pred':>8}{'actual':>8}{'gap':>8}")
        for service in sorted(qualifying, key=lambda s: -s.qualifying):
            predicted = service.predicted or 0.0
            hit = service.hit_rate or 0.0
            gap = service.calibration_gap or 0.0
            print(
                f"  {service.label[:31]:<32}{service.threshold:>6.2f}"
                f"{service.qualifying:>7,}{predicted:>8.1%}{hit:>8.1%}{gap:>+8.1%}"
            )

    if reasons:
        print("\n  WITHHELD BECAUSE:")
        for reason in reasons:
            print(f"    - {reason}")
    else:
        print("\n  PASSES EVERY GATE CRITERION.")


def summary() -> int:
    """Print every competition validated so far."""
    try:
        store = dict(json.loads(RESULTS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        print("No validation results stored yet.")
        return 1

    print("=" * 104)
    print(f"WAVE 1 VALIDATION — {len(store)} competitions")
    print("=" * 104)
    print(
        f"\n  {'competition':<24}{'rows':>8}{'fcasts':>8}{'rho':>8}{'Brier':>10}"
        f"{'log loss':>10}{'ECE':>8}{'bias':>8}  state"
    )
    active = 0
    for row in sorted(store.values(), key=lambda r: str(r.get("label"))):
        if row.get("state") == "ACTIVE":
            active += 1
        rho = row.get("rho")
        print(
            f"  {str(row.get('label'))[:23]:<24}{int(row.get('training_rows', 0)):>8,}"
            f"{int(row.get('forecasts', 0)):>8,}"
            f"{(f'{rho:+.3f}' if rho is not None else '-'):>8}"
            f"{float(row.get('brier', 0)):>10.5f}{float(row.get('log_loss', 0)):>10.5f}"
            f"{float(row.get('ece', 0)):>8.4f}{float(row.get('max_bias', 0)):>8.1%}"
            f"  {row.get('state')}"
        )

    print(f"\n  ACTIVE: {active}/{len(store)}   WITHHELD: {len(store) - active}/{len(store)}")

    for row in store.values():
        reasons = list(row.get("reasons") or [])
        if reasons:
            print(f"\n  {row.get('label')} withheld because:")
            for reason in reasons:
                print(f"    - {reason}")

    print("\n" + "=" * 104)
    print("Validation only. A competition reading ACTIVE here has passed the gate;")
    print("adding it to the live scan is a separate, deliberate step.")
    print("=" * 104)
    return 0


async def main() -> int:
    """Fit and validate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leagues", default="")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    if args.summary:
        return summary()

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
        chosen = {382: WAVE_1[382]}

    print("=" * 96)
    print("VALIDATION GATE (fixed before any result was seen)")
    print("=" * 96)
    print(f"  minimum forecasts : {GATE.min_forecasts}")
    print(f"  maximum Brier     : {GATE.max_brier}")
    print(f"  maximum log loss  : {GATE.max_log_loss}")
    print(f"  maximum ECE       : {GATE.max_ece}")
    print(f"  maximum bias      : {GATE.max_outcome_bias:.0%}")
    print(f"  max window spread : {GATE.max_window_spread}")

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        for league_id, label in chosen.items():
            print(f"\n  validating {label} ...", flush=True)
            result = await validate(session, league_id, label)
            report(result)
            _persist(result)
        await session.rollback()
    await database.disconnect()

    print("\n" + "=" * 96)
    print("READ-ONLY. No competition was activated and no threshold changed.")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
