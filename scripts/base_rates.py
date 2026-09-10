#!/usr/bin/env python3
"""Recompute outcome base rates from the historical record.

Statistical leans are ranked by how much a forecast departs from how often an
outcome normally happens, so those frequencies have to be measured rather than
assumed. Guessing them was the original defect: "Goals" was scored against 0.5
when Over 1.5 lands 74% of the time, and every lean in the first day's top five
was a goals line as a result.

    python scripts/base_rates.py

Prints a dict ready to paste into app/services/leans.py.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.paths import data_dir


def main() -> int:
    """Count outcomes across every season file and print the rates."""
    parser = argparse.ArgumentParser(description="Compute outcome base rates.")
    parser.add_argument("--data", type=Path, default=None)
    args = parser.parse_args()

    directory = args.data or data_dir()
    if not directory.is_dir():
        print(f"No CSV directory at {directory}", file=sys.stderr)
        return 2

    total = 0
    counts: Counter[str] = Counter()

    for path in sorted(directory.glob("*.csv")):
        with path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    home, away = int(row["FTHG"]), int(row["FTAG"])
                except (KeyError, TypeError, ValueError):
                    continue
                total += 1
                goals = home + away
                counts["Home"] += home > away
                counts["Draw"] += home == away
                counts["Away"] += home < away
                for line in ("0.5", "1.5", "2.5", "3.5"):
                    counts[f"Over {line}"] += goals > float(line)
                counts["Yes"] += home > 0 and away > 0

    if not total:
        print("No matches found.", file=sys.stderr)
        return 1

    rates = {
        "Home": counts["Home"] / total,
        "Draw": counts["Draw"] / total,
        "Away": counts["Away"] / total,
        "Home win": counts["Home"] / total,
        "Away win": counts["Away"] / total,
        "1X (home or draw)": (counts["Home"] + counts["Draw"]) / total,
        "12 (home or away)": (counts["Home"] + counts["Away"]) / total,
        "X2 (draw or away)": (counts["Draw"] + counts["Away"]) / total,
    }
    for line in ("0.5", "1.5", "2.5", "3.5"):
        over = counts[f"Over {line}"] / total
        rates[f"Over {line}"] = over
        rates[f"Under {line}"] = 1 - over
    rates["Yes"] = counts["Yes"] / total
    rates["No"] = 1 - rates["Yes"]

    print(f"# Measured across {total:,} matches.")
    print("BASE_RATES: Final[dict[str, float]] = {")
    for name, rate in rates.items():
        print(f'    "{name}": {rate:.4f},')
    print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
