#!/usr/bin/env python3
"""Fit the Dixon-Coles correlation parameter per competition.

**Why per competition.** A single global rho of -0.13 was measured across 38
competitions in ``docs/VALIDATION.md`` and found to be roughly right for
leagues that were badly under-predicting draws and too aggressive for leagues
that were already close. Serie B moved from -6.4% to -3.4%; the Premier League
moved from -1.0% to +1.6%, trading a small under-prediction for a larger
over-prediction. One parameter cannot serve both.

**Fitted on a training split only.** Matches are split chronologically. Rho is
estimated on the earlier portion and never sees the later one, which is where
the audit evaluates. Fitting on everything and then reporting the improvement
would be measuring how well a parameter fits the data it was fitted to.

**Rates come from the production model.** ``team_strength`` and
``expected_goals``, walked forward exactly as the live scanner does, so the
observations rho is fitted against are the ones production would produce.

**Thin competitions keep the default.** ``estimate_rho`` already refuses to
fit below 200 matches. A rho fitted to a hundred games is noise wearing a
decimal point, and the published default is the better estimate.

Run::

    python scripts/fit_rho.py
    python scripts/fit_rho.py --train-fraction 0.7 --out app/quant/rho_fitted.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.quant.dixon_coles import (
    DEFAULT_RHO,
    MIN_MATCHES_FOR_RHO,
    estimate_rho,
)
from app.quant.poisson import (
    LeagueAverages,
    expected_goals,
    team_strength,
)


def _parse_date(raw: str) -> object | None:
    """Read the CSV date field, which appears in two formats."""
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def observations_for(
    paths: list[Path], min_history: int, min_team_matches: int
) -> dict[str, list[tuple[int, int, float, float]]]:
    """Return ``(home_goals, away_goals, lambda_home, lambda_away)`` per competition.

    Walked forward: the rates attached to each match are estimated only from
    matches already played, so an observation is never informed by its own
    result.
    """
    collected: dict[str, list[tuple[int, int, float, float]]] = defaultdict(list)

    for path in sorted(paths):
        competition = path.stem.split("_")[0]
        rows: list[tuple[object, str, str, int, int]] = []
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

        rows.sort(key=lambda item: item[0])  # type: ignore[arg-type,return-value]
        if len(rows) < min_history:
            continue

        ids: dict[str, int] = {}
        home_record: dict[int, list[tuple[int, int]]] = defaultdict(list)
        away_record: dict[int, list[tuple[int, int]]] = defaultdict(list)
        league_home = league_away = played = 0

        for _, home, away, home_goals, away_goals in rows:
            home_id = ids.setdefault(home, len(ids))
            away_id = ids.setdefault(away, len(ids))

            seen_home = len(home_record[home_id]) + len(away_record[home_id])
            seen_away = len(home_record[away_id]) + len(away_record[away_id])
            if (
                played >= min_history
                and seen_home >= min_team_matches
                and seen_away >= min_team_matches
            ):
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
                        collected[competition].append(
                            (home_goals, away_goals, lambda_home, lambda_away)
                        )

            home_record[home_id].append((home_goals, away_goals))
            away_record[away_id].append((away_goals, home_goals))
            league_home += home_goals
            league_away += away_goals
            # Counts matches folded into the baseline, not the loop index.
            played += 1  # noqa: SIM113

    return collected


def main() -> int:
    """Fit rho per competition and write the table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data")
    parser.add_argument("--min-history", type=int, default=60)
    parser.add_argument("--min-team-matches", type=int, default=5)
    parser.add_argument(
        "--train-fraction",
        type=float,
        default=0.6,
        help="Chronological share used for fitting; the remainder is never seen.",
    )
    parser.add_argument("--out", default="app/quant/rho_fitted.json")
    args = parser.parse_args()

    directory = Path(args.data)
    paths = [p for p in directory.glob("*.csv") if not p.stem.startswith("basketball")]
    if not paths:
        print("No competition files found.", file=sys.stderr)
        return 1

    collected = observations_for(paths, args.min_history, args.min_team_matches)

    print("=" * 72)
    print("Per-competition rho, fitted on the training split only")
    print("=" * 72)
    print(f"Train fraction : {args.train_fraction:.0%}")
    print(f"Default rho    : {DEFAULT_RHO}")
    print(f"Fit floor      : {MIN_MATCHES_FOR_RHO} matches")
    print()
    print(f"{'Comp':<8}{'train n':>9}{'rho':>9}{'fitted':>9}   note")
    print("-" * 72)

    table: dict[str, float] = {}
    fitted_count = 0
    for competition in sorted(collected):
        observations = collected[competition]
        split = int(len(observations) * args.train_fraction)
        train = observations[:split]

        fit = estimate_rho(train)
        note = ""
        if not fit.fitted:
            # Too little data to fit. The published default is a better
            # estimate than a number derived from a hundred games.
            note = "below floor - default kept"
        else:
            # Every fitted value is stored, including ones near zero.
            #
            # An earlier version of this script treated "rho near zero" as a
            # failed fit and fell back to the default of -0.13 — applying the
            # strongest available correction to precisely the competitions
            # whose data says apply none. A fitted rho of -0.005 is not a
            # missing answer; it is the answer, and it says these leagues
            # score close enough to independently that the correction should
            # do almost nothing.
            table[competition] = round(fit.rho, 4)
            fitted_count += 1
            if not fit.improves_on_independence:
                note = "near zero - correction will be negligible here"
            elif fit.rho > 0:
                note = "positive - corrects opposite to the default"

        print(
            f"{competition:<8}{len(train):>9,}{fit.rho:>9.4f}"
            f"{fit.fitted!s:>9}   {note}"
        )

    print()
    print(
        f"Fitted for {fitted_count} of {len(collected)} competitions; "
        f"the rest fall back to {DEFAULT_RHO}."
    )
    if table:
        values = sorted(table.values())
        median = values[len(values) // 2]
        print(f"Fitted range: {values[0]:+.4f} to {values[-1]:+.4f}, median {median:+.4f}")
        print(f"Default for comparison: {DEFAULT_RHO:+.4f}")

    payload = {
        "_comment": (
            "Per-competition Dixon-Coles rho, fitted by maximum likelihood on a "
            "chronological training split. Competitions absent from this table "
            "use the default. Regenerate with scripts/fit_rho.py."
        ),
        "_default": DEFAULT_RHO,
        "_train_fraction": args.train_fraction,
        "_fitted_at": datetime.now().strftime("%Y-%m-%d"),
        "rho": table,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
