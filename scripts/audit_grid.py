#!/usr/bin/env python3
"""Before/after audit: independent Poisson against the Dixon-Coles grid.

**What this answers.** Not "did the numbers change" — they certainly did — but
whether the change is an improvement on out-of-sample probability quality, and
specifically whether the measured draw under-prediction actually narrows.

**Method.** Strictly chronological walk-forward. For each competition file,
matches are read in date order; team scoring rates are estimated from matches
already played and used to forecast the next one. A fixture is never scored by
a model that has seen it. Both engines forecast the *same* fixture from the
*same* rates, so every difference between them is the correction and nothing
else.

**Why not the existing backtest harness.** ``scripts/validate.py`` runs against
the database and needs a populated Postgres. This runs against the committed
CSVs, so the result is reproducible by anyone with the repository and no
infrastructure. The two are complementary; this one is the portable record.

**What it deliberately does not do.** It does not compare against bookmaker
prices and makes no statement about edge. It measures calibration only.

Run::

    python scripts/audit_grid.py
    python scripts/audit_grid.py --leagues E0,D1,SP1,I1 --min-history 60
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.quant.grid import build_match_probabilities
from app.quant.poisson import (
    LeagueAverages,
    expected_goals,
    team_strength,
)

OUTCOMES = ("home", "draw", "away")


@dataclass
class Forecast:
    """One fixture forecast by one engine, with the outcome that followed."""

    competition: str
    match_date: date
    probabilities: dict[str, float]
    actual: str
    home_goals: int
    away_goals: int

    @property
    def assigned(self) -> float:
        """Probability this engine gave to what actually happened."""
        return self.probabilities[self.actual]


@dataclass
class Bucket:
    """A calibration bin."""

    predicted: float = 0.0
    observed: int = 0
    count: int = 0


@dataclass
class Report:
    """Everything measured for one engine."""

    label: str
    forecasts: list[Forecast] = field(default_factory=list)

    # ── headline scores ────────────────────────────────────────────────
    @property
    def brier(self) -> float:
        """Multiclass Brier score. Lower is better."""
        total = 0.0
        for f in self.forecasts:
            for outcome in OUTCOMES:
                actual = 1.0 if f.actual == outcome else 0.0
                total += (f.probabilities[outcome] - actual) ** 2
        return total / len(self.forecasts) if self.forecasts else 0.0

    @property
    def log_loss(self) -> float:
        """Mean negative log likelihood. Lower is better."""
        total = 0.0
        for f in self.forecasts:
            total -= math.log(max(f.assigned, 1e-15))
        return total / len(self.forecasts) if self.forecasts else 0.0

    def bias(self, outcome: str) -> tuple[float, float, float]:
        """Return (predicted rate, observed rate, difference) for one outcome."""
        if not self.forecasts:
            return (0.0, 0.0, 0.0)
        predicted = sum(f.probabilities[outcome] for f in self.forecasts) / len(self.forecasts)
        observed = sum(1 for f in self.forecasts if f.actual == outcome) / len(self.forecasts)
        return (predicted, observed, predicted - observed)

    def ece(self, bins: int = 10) -> float:
        """Expected calibration error across all three outcomes."""
        buckets: dict[int, Bucket] = defaultdict(Bucket)
        total = 0
        for f in self.forecasts:
            for outcome in OUTCOMES:
                p = f.probabilities[outcome]
                index = min(int(p * bins), bins - 1)
                bucket = buckets[index]
                bucket.predicted += p
                bucket.observed += 1 if f.actual == outcome else 0
                bucket.count += 1
                total += 1
        if not total:
            return 0.0
        error = 0.0
        for bucket in buckets.values():
            if not bucket.count:
                continue
            mean_p = bucket.predicted / bucket.count
            mean_o = bucket.observed / bucket.count
            error += (bucket.count / total) * abs(mean_p - mean_o)
        return error

    def bands(self, bins: int = 10) -> list[tuple[str, int, float, float]]:
        """Return calibration by probability band."""
        buckets: dict[int, Bucket] = defaultdict(Bucket)
        for f in self.forecasts:
            for outcome in OUTCOMES:
                p = f.probabilities[outcome]
                index = min(int(p * bins), bins - 1)
                bucket = buckets[index]
                bucket.predicted += p
                bucket.observed += 1 if f.actual == outcome else 0
                bucket.count += 1
        rows = []
        for index in sorted(buckets):
            bucket = buckets[index]
            if not bucket.count:
                continue
            low = index / bins
            high = (index + 1) / bins
            rows.append(
                (
                    f"{low:.0%}-{high:.0%}",
                    bucket.count,
                    bucket.predicted / bucket.count,
                    bucket.observed / bucket.count,
                )
            )
        return rows

    def by_competition(self) -> dict[str, Report]:
        """Split into one report per competition."""
        groups: dict[str, Report] = {}
        for f in self.forecasts:
            groups.setdefault(f.competition, Report(f.competition)).forecasts.append(f)
        return groups

    def by_period(self, count: int = 4) -> list[Report]:
        """Split chronologically into equal windows.

        An overall improvement driven by one favourable stretch is not an
        improvement. Per-window results show whether it holds repeatedly.
        """
        ordered = sorted(self.forecasts, key=lambda f: f.match_date)
        if not ordered:
            return []
        size = max(1, len(ordered) // count)
        windows = []
        for index in range(0, len(ordered), size):
            chunk = ordered[index : index + size]
            if not chunk:
                continue
            label = f"{chunk[0].match_date:%Y-%m} to {chunk[-1].match_date:%Y-%m}"
            windows.append(Report(label, chunk))
        return windows[:count] if len(windows) > count else windows


def bootstrap_brier_difference(
    baseline: Report, candidate: Report, samples: int = 2000, seed: int = 20260917
) -> tuple[float, float, float]:
    """Paired bootstrap on per-forecast Brier differences.

    Paired because both engines score the identical fixture list: the pairing
    removes fixture difficulty from the comparison, which is most of the
    variance. Returns (mean difference, 2.5th percentile, 97.5th percentile),
    where a negative difference favours the candidate.
    """
    paired: list[float] = []
    for base, cand in zip(baseline.forecasts, candidate.forecasts, strict=True):
        base_error = sum(
            (base.probabilities[o] - (1.0 if base.actual == o else 0.0)) ** 2 for o in OUTCOMES
        )
        cand_error = sum(
            (cand.probabilities[o] - (1.0 if cand.actual == o else 0.0)) ** 2 for o in OUTCOMES
        )
        paired.append(cand_error - base_error)

    if not paired:
        return (0.0, 0.0, 0.0)

    rng = random.Random(seed)
    size = len(paired)
    means: list[float] = []
    for _ in range(samples):
        total = 0.0
        for _ in range(size):
            total += paired[rng.randrange(size)]
        means.append(total / size)
    means.sort()
    mean = sum(paired) / size
    return (mean, means[int(0.025 * samples)], means[int(0.975 * samples)])


def _parse_date(raw: str) -> date | None:
    """Read the CSV's date field, which comes in two formats."""
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def walk_forward(
    paths: list[Path],
    min_history: int,
    min_team_matches: int,
    global_rho: bool = False,
    evaluate_from: float = 0.0,
) -> tuple[Report, Report]:
    """Forecast every eligible fixture with both engines.

    Both engines receive identical scoring rates for identical fixtures, so
    the only difference between the two reports is the correction.
    """
    independent = Report("Independent Poisson")
    corrected = Report("Dixon-Coles")

    for path in sorted(paths):
        competition = path.stem.split("_")[0]
        rows: list[tuple[date, str, str, int, int]] = []
        with path.open(newline="", encoding="utf-8", errors="ignore") as handle:
            for row in csv.DictReader(handle):
                parsed = _parse_date(str(row.get("Date", "")))
                if parsed is None:
                    continue
                try:
                    rows.append(
                        (
                            parsed,
                            row["HomeTeam"],
                            row["AwayTeam"],
                            int(row["FTHG"]),
                            int(row["FTAG"]),
                        )
                    )
                except (KeyError, ValueError, TypeError):
                    continue

        rows.sort(key=lambda item: item[0])
        if len(rows) < min_history:
            continue

        # Rates are always warmed on the full history; only *scoring* is
        # withheld. Skipping the early matches entirely would hand the holdout
        # a colder model than production ever runs.
        score_from = int(len(rows) * evaluate_from)

        # The production rate model, not a simplified stand-in. An earlier
        # version of this script used a symmetric attack/defence ratio with no
        # home-advantage term and no home/away split in the league baseline.
        # It reported a 14-point home-win bias that does not exist in
        # production — an artefact of the harness, not a finding about the
        # model. Anything measured here must come from the same functions the
        # live scanner calls.
        ids: dict[str, int] = {}
        home_record: dict[int, list[tuple[int, int]]] = defaultdict(list)
        away_record: dict[int, list[tuple[int, int]]] = defaultdict(list)
        league_home = league_away = played = 0

        for index, (match_date, home, away, home_goals, away_goals) in enumerate(rows):
            home_id = ids.setdefault(home, len(ids))
            away_id = ids.setdefault(away, len(ids))

            seen_home = len(home_record[home_id]) + len(away_record[home_id])
            seen_away = len(home_record[away_id]) + len(away_record[away_id])
            eligible = (
                played >= min_history
                and seen_home >= min_team_matches
                and seen_away >= min_team_matches
            )
            if eligible and index >= score_from:
                averages = LeagueAverages(
                    home_goals=league_home / played,
                    away_goals=league_away / played,
                    matches=played,
                )
                if averages.is_reliable:
                    home_strength = team_strength(
                        home_id, home_record[home_id], away_record[home_id], averages
                    )
                    away_strength = team_strength(
                        away_id, home_record[away_id], away_record[away_id], averages
                    )
                    if home_strength.is_reliable and away_strength.is_reliable:
                        lambda_home, lambda_away = expected_goals(
                            home_strength, away_strength, averages
                        )
                        actual = (
                            "home"
                            if home_goals > away_goals
                            else ("away" if away_goals > home_goals else "draw")
                        )
                        for report, use_correction in ((independent, False), (corrected, True)):
                            probabilities = build_match_probabilities(
                                lambda_home,
                                lambda_away,
                                corrected=use_correction,
                                competition=None if global_rho else competition,
                            )
                            report.forecasts.append(
                                Forecast(
                                    competition=competition,
                                    match_date=match_date,
                                    probabilities={
                                        "home": float(probabilities.home_win),
                                        "draw": float(probabilities.draw),
                                        "away": float(probabilities.away_win),
                                    },
                                    actual=actual,
                                    home_goals=home_goals,
                                    away_goals=away_goals,
                                )
                            )

            home_record[home_id].append((home_goals, away_goals))
            away_record[away_id].append((away_goals, home_goals))
            league_home += home_goals
            league_away += away_goals
            # Not enumerate(): this counts matches folded into the baseline,
            # which is the loop index only by coincidence.
            played += 1  # noqa: SIM113

    return independent, corrected


def main() -> int:
    """Run the audit and print a reproducible report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data", help="Directory of competition CSVs.")
    parser.add_argument(
        "--leagues",
        default="",
        help="Comma-separated competition codes; all available when omitted.",
    )
    parser.add_argument("--min-history", type=int, default=60)
    parser.add_argument("--min-team-matches", type=int, default=5)
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--json", default="", help="Write machine-readable results here.")
    parser.add_argument(
        "--global-rho",
        action="store_true",
        help="Use the single default rho instead of the fitted per-competition table.",
    )
    parser.add_argument(
        "--evaluate-from",
        type=float,
        default=0.0,
        help=(
            "Skip this chronological fraction of each competition before scoring. "
            "Set to the training fraction used by fit_rho.py to evaluate only on "
            "fixtures the fit never saw."
        ),
    )
    args = parser.parse_args()

    directory = Path(args.data)
    codes = [code.strip() for code in args.leagues.split(",") if code.strip()]
    if codes:
        paths = [p for p in directory.glob("*.csv") if p.stem.split("_")[0] in codes]
    else:
        paths = [p for p in directory.glob("*.csv") if not p.stem.startswith("basketball")]

    if not paths:
        print("No competition files found.", file=sys.stderr)
        return 1

    independent, corrected = walk_forward(
        paths,
        args.min_history,
        args.min_team_matches,
        global_rho=args.global_rho,
        evaluate_from=args.evaluate_from,
    )
    if not independent.forecasts:
        print("No fixtures were eligible.", file=sys.stderr)
        return 1

    print("=" * 74)
    print("QUANTSPORT — grid audit: independent Poisson vs Dixon-Coles")
    print("=" * 74)
    print(f"Competition files : {len(paths)}")
    print(f"Forecasts         : {len(independent.forecasts):,} per engine")
    dates = [f.match_date for f in independent.forecasts]
    print(f"Date range        : {min(dates)} to {max(dates)}")
    print(
        f"Walk-forward      : min {args.min_history} league matches, "
        f"{args.min_team_matches} per side"
    )
    print()

    print("HEADLINE".ljust(24) + "Poisson".rjust(14) + "Dixon-Coles".rjust(14) + "Change".rjust(14))
    print("-" * 74)
    for name, base, cand, better_low in (
        ("Brier score", independent.brier, corrected.brier, True),
        ("Log loss", independent.log_loss, corrected.log_loss, True),
        ("ECE", independent.ece(), corrected.ece(), True),
    ):
        delta = cand - base
        verdict = "better" if (delta < 0) == better_low and delta != 0 else "worse"
        print(f"{name:<24}{base:>14.6f}{cand:>14.6f}{delta:>+13.6f} {verdict}")
    print()

    print("OUTCOME BIAS (predicted - observed; negative = under-predicted)")
    print("-" * 74)
    print(
        "Outcome".ljust(12) + "Observed".rjust(12) + "Poisson".rjust(12)
        + "bias".rjust(10) + "  Dixon-Coles".rjust(14) + "bias".rjust(10)
    )
    for outcome in OUTCOMES:
        b_pred, observed, b_bias = independent.bias(outcome)
        c_pred, _, c_bias = corrected.bias(outcome)
        print(
            f"{outcome:<12}{observed:>11.1%}{b_pred:>12.1%}{b_bias:>+10.1%}"
            f"{c_pred:>13.1%}{c_bias:>+10.1%}"
        )
    print()

    mean, low, high = bootstrap_brier_difference(independent, corrected, args.bootstrap)
    print(f"PAIRED BOOTSTRAP on Brier difference ({args.bootstrap:,} resamples)")
    print("-" * 74)
    print(f"  mean difference : {mean:+.6f}   (negative favours Dixon-Coles)")
    print(f"  95% interval    : [{low:+.6f}, {high:+.6f}]")
    distinguishable = high < 0 or low > 0
    print(
        f"  verdict         : "
        f"{'distinguishable from chance' if distinguishable else 'straddles zero'}"
    )
    print()

    print("CALIBRATION BY BAND")
    print("-" * 74)
    print(
        "Band".ljust(12) + "n".rjust(9) + "Poisson pred".rjust(15)
        + "obs".rjust(9) + "  DC pred".rjust(12) + "obs".rjust(9)
    )
    base_bands = {row[0]: row for row in independent.bands()}
    cand_bands = {row[0]: row for row in corrected.bands()}
    for band in sorted(set(base_bands) | set(cand_bands)):
        base_row = base_bands.get(band)
        cand_row = cand_bands.get(band)
        if base_row is None or cand_row is None:
            continue
        print(f"{band:<12}{base_row[1]:>9,}{base_row[2]:>15.1%}{base_row[3]:>9.1%}"
              f"{cand_row[2]:>12.1%}{cand_row[3]:>9.1%}")
    print()

    print("PER-COMPETITION (Brier)")
    print("-" * 74)
    base_groups = independent.by_competition()
    cand_groups = corrected.by_competition()
    improved = 0
    for code in sorted(base_groups):
        base_group = base_groups[code]
        cand_group = cand_groups.get(code)
        if cand_group is None or len(base_group.forecasts) < 200:
            continue
        delta = cand_group.brier - base_group.brier
        improved += delta < 0
        _, _, base_draw = base_group.bias("draw")
        _, _, cand_draw = cand_group.bias("draw")
        print(f"{code:<8}{len(base_group.forecasts):>8,}{base_group.brier:>12.5f}"
              f"{cand_group.brier:>12.5f}{delta:>+12.5f}"
              f"   draw bias {base_draw:+.1%} -> {cand_draw:+.1%}")
    eligible = sum(1 for k in base_groups if len(base_groups[k].forecasts) >= 200)
    print(f"\n  improved in {improved} of {eligible} competitions")
    print()

    print("CHRONOLOGICAL WINDOWS (Brier)")
    print("-" * 74)
    for base_window, cand_window in zip(
        independent.by_period(), corrected.by_period(), strict=False
    ):
        delta = cand_window.brier - base_window.brier
        print(f"{base_window.label:<24}{len(base_window.forecasts):>8,}"
              f"{base_window.brier:>12.5f}{cand_window.brier:>12.5f}{delta:>+12.5f}")
    print()

    if args.json:
        payload = {
            "forecasts": len(independent.forecasts),
            "date_range": [str(min(dates)), str(max(dates))],
            "independent": {
                "brier": independent.brier,
                "log_loss": independent.log_loss,
                "ece": independent.ece(),
                "bias": {o: independent.bias(o) for o in OUTCOMES},
            },
            "corrected": {
                "brier": corrected.brier,
                "log_loss": corrected.log_loss,
                "ece": corrected.ece(),
                "bias": {o: corrected.bias(o) for o in OUTCOMES},
            },
            "bootstrap": {"mean": mean, "low": low, "high": high},
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
