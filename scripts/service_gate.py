#!/usr/bin/env python3
"""Every service, every Wave 1 competition, one capability per pair.

**Why per service and not per competition.** The goals diagnosis found the
model well calibrated across all forecasts and over-confident only in the
qualifying tail — every gap positive, across eight competitions and six
markets. A competition-wide verdict would either publish a badly calibrated
market or discard a well calibrated one alongside it. Capability belongs at
``competition x service``.

**The statistical method is fixed here, before any result is seen.** A raw
calibration gap cannot distinguish evidence from noise: 35% off on 15
selections and 5% off on 1,000 are not comparable claims. Each tail hit rate
therefore carries a Wilson score interval, and a service is judged on whether
the interval excludes its predicted probability — not on the point estimate.

Three states, deliberately distinct:

* ``ACTIVE`` — the interval contains the predicted probability, so the
  published number is consistent with what happened.
* ``WITHHELD`` — the interval excludes it. The model is saying something the
  record contradicts.
* ``INSUFFICIENT_EVIDENCE`` — the interval is too wide to distinguish either
  way. Not a failure, and never collapsed into one.

**Combination markets earn their own state.** A weak BTTS tail does not imply
a weak "Home or BTTS" tail: the joint predicate is evaluated on the grid
directly, never approximated by adding component probabilities.

**Nothing is tuned and nothing is activated.** Production thresholds are used
exactly as they stand. Unknown capability fails closed.

Run::

    railway ssh "python scripts/service_gate.py --all"
    railway ssh "python scripts/service_gate.py --matrix"
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
from app.quant.grid import build_grid, grid_version
from app.quant.markets import derive_markets, settles_won
from app.quant.poisson import LeagueAverages, expected_goals, team_strength
from app.services.best_of_day import SERVICES

RESULTS_PATH = Path("/tmp/quantsport_service_gate.json")  # noqa: S108

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

# Competition-level outcomes from the 1X2 validation, carried forward. A
# service cannot publish for a competition the competition gate withheld,
# however well that service calibrates — the directional bias and the sample
# shortage are properties of the competition, not of one market.
COMPETITION_WITHHELD: dict[int, str] = {
    399: "1X2 directional bias: home 57.4% predicted against 66.7% observed",
    243: "insufficient validation sample: 686 forecasts against an 800 minimum",
}

MIN_HISTORY = 60
MIN_TEAM_MATCHES = 5

# ── Sufficiency methodology, fixed before results ────────────────────────────

CONFIDENCE_Z = 1.96
"""Two-sided 95% interval. Stated here so the bar cannot be adjusted later."""

MAX_INTERVAL_HALF_WIDTH = 0.08
"""How precise the tail hit rate must be before a verdict is possible.

A Wilson interval wider than ±8 points cannot separate a well calibrated
service from a badly calibrated one, so the honest answer is that we do not
know. This bound decides INSUFFICIENT_EVIDENCE and is independent of which
side of the prediction the point estimate falls on.

Eight points corresponds to roughly 130 qualifying selections at a 70%
hit rate — enough to matter, and reachable within a season or two for a
service that qualifies regularly.
"""

MIN_QUALIFYING = 30
"""Absolute floor. Below this the Wilson interval is unreliable regardless of
its width, because the normal approximation it rests on has not taken hold."""

MIN_WINDOW_SAMPLE = 20
"""Qualifying selections a chronological window needs to count toward
stability. Below it the window is reported but not used for a verdict."""

MAX_WINDOW_SPREAD = 0.20
"""Permitted range of hit rate across chronological windows.

A service that hits 80% in one period and 50% in another has not been shown
to work, even if the pooled figure looks acceptable.
"""

BANDS: tuple[tuple[float, float], ...] = (
    (0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
    (0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 1.01),
)
MIN_BAND_SAMPLE = 25
"""Below this a band is merged into its neighbour rather than reported as if
a handful of selections told us something."""


def wilson(successes: int, trials: int) -> tuple[float, float]:
    """Wilson score interval for a proportion.

    Chosen over the normal approximation because it behaves at the extremes:
    a service hitting 28 of 30 does not get an interval running above 1.0.
    """
    if trials == 0:
        return (0.0, 1.0)
    proportion = successes / trials
    z = CONFIDENCE_Z
    denominator = 1 + z * z / trials
    centre = (proportion + z * z / (2 * trials)) / denominator
    spread = (
        z
        * math.sqrt(proportion * (1 - proportion) / trials + z * z / (4 * trials * trials))
        / denominator
    )
    return (max(0.0, centre - spread), min(1.0, centre + spread))


@dataclass
class Capability:
    """One competition x service x model version."""

    league_id: int
    competition: str
    service_key: str
    service_label: str
    market: str
    outcome: str
    threshold: float
    model_version: str

    total: int = 0
    all_predicted: float = 0.0
    all_won: int = 0

    qualifying: int = 0
    tail_predicted: float = 0.0
    tail_won: int = 0
    brier_total: float = 0.0

    bands: dict[int, list[tuple[float, bool]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    windows: list[tuple[str, int, float, float]] = field(default_factory=list)
    window_dates: list[tuple[date, float, bool]] = field(default_factory=list)

    @property
    def qualification_rate(self) -> float:
        return self.qualifying / self.total if self.total else 0.0

    @property
    def all_mean_predicted(self) -> float:
        return self.all_predicted / self.total if self.total else 0.0

    @property
    def all_observed(self) -> float:
        return self.all_won / self.total if self.total else 0.0

    @property
    def all_gap(self) -> float:
        return self.all_mean_predicted - self.all_observed

    @property
    def tail_mean_predicted(self) -> float:
        return self.tail_predicted / self.qualifying if self.qualifying else 0.0

    @property
    def tail_observed(self) -> float:
        return self.tail_won / self.qualifying if self.qualifying else 0.0

    @property
    def tail_gap(self) -> float:
        return self.tail_mean_predicted - self.tail_observed

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.tail_won, self.qualifying)

    @property
    def half_width(self) -> float:
        low, high = self.interval
        return (high - low) / 2

    @property
    def brier(self) -> float:
        return self.brier_total / self.qualifying if self.qualifying else 0.0

    def compute_windows(self, count: int = 4) -> None:
        """Split the qualifying selections into chronological quarters."""
        ordered = sorted(self.window_dates, key=lambda row: row[0])
        if len(ordered) < count:
            return
        size = len(ordered) // count
        for index in range(count):
            start = index * size
            chunk = ordered[start : start + size] if index < count - 1 else ordered[start:]
            if not chunk:
                continue
            won = sum(1 for _, _, hit in chunk if hit)
            self.windows.append(
                (
                    f"{chunk[0][0]:%Y-%m}..{chunk[-1][0]:%Y-%m}",
                    len(chunk),
                    sum(p for _, p, _ in chunk) / len(chunk),
                    won / len(chunk),
                )
            )

    @property
    def window_spread(self) -> float:
        usable = [row[3] for row in self.windows if row[1] >= MIN_WINDOW_SAMPLE]
        if len(usable) < 2:
            return 0.0
        return max(usable) - min(usable)

    @property
    def usable_windows(self) -> int:
        return sum(1 for row in self.windows if row[1] >= MIN_WINDOW_SAMPLE)

    def band_rows(self) -> list[tuple[str, int, float, float]]:
        """Calibration by probability band, sparse bands merged upward."""
        rows: list[tuple[str, int, float, float]] = []
        carried: list[tuple[float, bool]] = []
        carried_from: int | None = None
        for index, (low, high) in enumerate(BANDS):
            entries = list(self.bands.get(index, [])) + carried
            if not entries:
                continue
            if len(entries) < MIN_BAND_SAMPLE and index < len(BANDS) - 1:
                carried = entries
                if carried_from is None:
                    carried_from = index
                continue
            start = BANDS[carried_from][0] if carried_from is not None else low
            rows.append(
                (
                    f"{start:.0%}-{high:.0%}",
                    len(entries),
                    sum(p for p, _ in entries) / len(entries),
                    sum(1 for _, hit in entries if hit) / len(entries),
                )
            )
            carried = []
            carried_from = None
        return rows

    def verdict(self) -> tuple[str, str]:
        """Apply the fixed methodology. Returns state and reason code."""
        withheld = COMPETITION_WITHHELD.get(self.league_id)
        if withheld:
            return ("WITHHELD", "COMPETITION_WITHHELD")
        if self.qualifying < MIN_QUALIFYING:
            return ("INSUFFICIENT_EVIDENCE", "INSUFFICIENT_SAMPLE")
        if self.half_width > MAX_INTERVAL_HALF_WIDTH:
            return ("INSUFFICIENT_EVIDENCE", "INTERVAL_TOO_WIDE")

        low, high = self.interval
        if not (low <= self.tail_mean_predicted <= high):
            return ("WITHHELD", "CALIBRATION_FAIL")
        if self.usable_windows >= 2 and self.window_spread > MAX_WINDOW_SPREAD:
            return ("WITHHELD", "INSTABILITY")
        return ("ACTIVE", "VALIDATED")


async def evaluate(
    session: AsyncSession, league_id: int, label: str
) -> list[Capability]:
    """Walk one competition forward, scoring every service on every fixture."""
    version = grid_version()
    capabilities: dict[str, Capability] = {
        service.key: Capability(
            league_id=league_id,
            competition=label,
            service_key=service.key,
            service_label=service.label,
            market=service.market,
            outcome=service.outcome,
            threshold=service.min_probability,
            model_version=version,
        )
        for service in SERVICES
    }

    found = await session.execute(
        select(CompetitionRow.id).where(CompetitionRow.canonical_name == label)
    )
    competition_id = found.scalar_one_or_none()
    if competition_id is None:
        return list(capabilities.values())

    rows = await session.execute(
        select(HistoricalMatch)
        .where(HistoricalMatch.competition_id == competition_id)
        .order_by(HistoricalMatch.match_date)
    )
    matches = list(rows.scalars().all())
    if len(matches) < MIN_HISTORY * 2:
        return list(capabilities.values())

    split = len(matches) // 2
    training: list[tuple[int, int, float, float]] = []
    rho: float | None = None
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
                        if rho is None:
                            rho = estimate_rho(training).rho
                        grid = build_grid(lambda_home, lambda_away, rho=rho)
                        markets = derive_markets(grid)

                        for service in SERVICES:
                            capability = capabilities[service.key]
                            probability = float(
                                markets.get(service.market, {}).get(
                                    service.outcome, 0
                                )
                            )
                            # The joint predicate on the grid, never a sum of
                            # component probabilities.
                            hit = bool(
                                settles_won(
                                    service.market,
                                    service.outcome,
                                    home_goals,
                                    away_goals,
                                )
                            )
                            capability.total += 1
                            capability.all_predicted += probability
                            capability.all_won += 1 if hit else 0

                            if probability >= service.min_probability:
                                capability.qualifying += 1
                                capability.tail_predicted += probability
                                capability.tail_won += 1 if hit else 0
                                capability.brier_total += (
                                    probability - (1.0 if hit else 0.0)
                                ) ** 2
                                capability.window_dates.append(
                                    (match.match_date, probability, hit)
                                )
                                for band_index, (low, high) in enumerate(BANDS):
                                    if low <= probability < high:
                                        capability.bands[band_index].append(
                                            (probability, hit)
                                        )
                                        break

        scored_for[home_id].append((home_goals, away_goals))
        scored_against[away_id].append((away_goals, home_goals))
        league_home += home_goals
        league_away += away_goals
        played += 1

    for capability in capabilities.values():
        capability.compute_windows()
    return list(capabilities.values())


def _persist(capabilities: list[Capability]) -> None:
    """Keep the matrix; a later model change is compared, not overwritten."""
    try:
        store: dict[str, Any] = {}
        if RESULTS_PATH.exists():
            store = dict(json.loads(RESULTS_PATH.read_text(encoding="utf-8")))
        for capability in capabilities:
            state, reason = capability.verdict()
            low, high = capability.interval
            key = f"{capability.league_id}:{capability.service_key}"
            store[key] = {
                "competition": capability.competition,
                "service": capability.service_label,
                "service_key": capability.service_key,
                "model_version": capability.model_version,
                "threshold": capability.threshold,
                "total": capability.total,
                "qualifying": capability.qualifying,
                "qualification_rate": capability.qualification_rate,
                "all_predicted": capability.all_mean_predicted,
                "all_observed": capability.all_observed,
                "all_gap": capability.all_gap,
                "tail_predicted": capability.tail_mean_predicted,
                "tail_observed": capability.tail_observed,
                "tail_gap": capability.tail_gap,
                "interval": [low, high],
                "half_width": capability.half_width,
                "brier": capability.brier,
                "window_spread": capability.window_spread,
                "usable_windows": capability.usable_windows,
                "state": state,
                "reason": reason,
            }
        RESULTS_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")
    except OSError as error:
        print(f"  (could not persist: {error})", flush=True)


def report(capabilities: list[Capability]) -> None:
    """Print one competition's service matrix."""
    if not capabilities:
        return
    competition = capabilities[0].competition
    withheld = COMPETITION_WITHHELD.get(capabilities[0].league_id)

    print("\n" + "=" * 112)
    print(f"{competition}" + ("  [COMPETITION WITHHELD — research only]" if withheld else ""))
    if withheld:
        print(f"  reason: {withheld}")
    print("=" * 112)
    print(
        f"\n  {'service':<30}{'qual':>7}{'rate':>7}{'allgap':>8}"
        f"{'pred':>7}{'obs':>7}{'gap':>7}{'95% interval':>17}{'±':>6}  state"
    )
    for capability in sorted(capabilities, key=lambda c: -c.qualifying):
        state, reason = capability.verdict()
        low, high = capability.interval
        if not capability.qualifying:
            print(
                f"  {capability.service_label[:29]:<30}{0:>7}"
                f"{'-':>7}{'-':>8}{'-':>7}{'-':>7}{'-':>7}{'-':>17}{'-':>6}"
                f"  INSUFFICIENT_EVIDENCE"
            )
            continue
        print(
            f"  {capability.service_label[:29]:<30}{capability.qualifying:>7,}"
            f"{capability.qualification_rate:>7.0%}{capability.all_gap:>+8.1%}"
            f"{capability.tail_mean_predicted:>7.1%}{capability.tail_observed:>7.1%}"
            f"{capability.tail_gap:>+7.1%}"
            f"{f'[{low:.1%}, {high:.1%}]':>17}{capability.half_width:>6.1%}"
            f"  {state}"
        )

    for capability in sorted(capabilities, key=lambda c: -c.qualifying):
        rows = capability.band_rows()
        if len(rows) < 2 or capability.qualifying < MIN_QUALIFYING:
            continue
        state, _ = capability.verdict()
        print(f"\n  bands — {capability.service_label}  ({state})")
        for label, count, predicted, observed in rows:
            print(
                f"    {label:<14}{count:>6,}  predicted {predicted:>6.1%}"
                f"  observed {observed:>6.1%}  gap {predicted - observed:>+6.1%}"
            )


def matrix() -> int:
    """Print the full capability matrix across every competition."""
    try:
        store = dict(json.loads(RESULTS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        print("No service gate results stored yet.")
        return 1

    states: dict[str, int] = defaultdict(int)
    by_competition: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in store.values():
        state = str(row.get("state"))
        states[state] += 1
        by_competition[str(row.get("competition"))][state].append(
            str(row.get("service"))
        )

    print("=" * 112)
    print("WAVE 1 COMPLETE SERVICE VALIDATION")
    print("=" * 112)
    print(f"\n  competition x service capabilities tested : {len(store)}")
    print(f"  ACTIVE                                   : {states['ACTIVE']}")
    print(f"  WITHHELD                                 : {states['WITHHELD']}")
    print(f"  INSUFFICIENT_EVIDENCE                    : {states['INSUFFICIENT_EVIDENCE']}")

    print("\n" + "-" * 112)
    print("METHODOLOGY (fixed before results)")
    print("-" * 112)
    print(f"  confidence               : {CONFIDENCE_Z} (95% two-sided, Wilson)")
    print(f"  max interval half-width  : {MAX_INTERVAL_HALF_WIDTH:.0%}")
    print(f"  minimum qualifying       : {MIN_QUALIFYING}")
    print(f"  max window spread        : {MAX_WINDOW_SPREAD:.0%}")
    print("  ACTIVE requires the interval to contain the predicted probability.")

    for competition in sorted(by_competition):
        rows = by_competition[competition]
        print("\n" + "-" * 112)
        print(competition)
        print("-" * 112)
        for state in ("ACTIVE", "WITHHELD", "INSUFFICIENT_EVIDENCE"):
            names = rows.get(state, [])
            print(f"  {state:<24}{len(names):>3}")
            for name in sorted(names):
                print(f"      {name}")

    print("\n" + "-" * 112)
    print("REASONS")
    print("-" * 112)
    reasons: dict[str, int] = defaultdict(int)
    for row in store.values():
        reasons[str(row.get("reason"))] += 1
    for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
        print(f"  {reason:<28}{count:>5}")

    print("\n" + "=" * 112)
    print("Capability is stored at competition x service x model_version.")
    print("Publication requires competition permitted AND capability ACTIVE AND")
    print("the fixture passing existing production rules. Unknown fails closed.")
    print("=" * 112)
    return 0


async def main() -> int:
    """Run the gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leagues", default="")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--matrix", action="store_true")
    args = parser.parse_args()

    if args.matrix:
        return matrix()

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

    print("=" * 112)
    print("SERVICE GATE METHODOLOGY (fixed before any result was seen)")
    print("=" * 112)
    print(f"  Wilson interval          : {CONFIDENCE_Z} z, 95% two-sided")
    print(f"  max interval half-width  : {MAX_INTERVAL_HALF_WIDTH:.0%}"
          "   (wider -> INSUFFICIENT_EVIDENCE)")
    print(f"  minimum qualifying       : {MIN_QUALIFYING}")
    print(f"  max window spread        : {MAX_WINDOW_SPREAD:.0%}")
    print(f"  services evaluated       : {len(SERVICES)}")
    print("  ACTIVE requires the interval to contain the predicted probability.")

    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        for league_id, label in chosen.items():
            print(f"\n  evaluating {label} ...", flush=True)
            capabilities = await evaluate(session, league_id, label)
            report(capabilities)
            _persist(capabilities)
        await session.rollback()
    await database.disconnect()

    print("\n" + "=" * 112)
    print("READ-ONLY. No capability activated, no threshold changed.")
    print("=" * 112)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
