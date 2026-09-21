#!/usr/bin/env python3
"""Wave-1 capability re-validation against the frozen V3 engine.

The statistical methodology is **not re-implemented here** — it is imported and
run unchanged:

* Layer 1, the competition-level 1X2 gate, is ``validate_wave1.Validation`` /
  ``Gate`` (Brier, log-loss, ECE, outcome bias, window spread, ≥800 forecasts).
* Layer 2, the per-service tail calibration, is ``service_gate.Capability``
  (Wilson interval contains the predicted probability, ≥30 qualifying, interval
  ≤±8pts, window spread ≤20pts), with its verdict logic byte-for-byte intact.

The **only** thing that changes is the probability engine: every fixture is
forecast with the canonical V3 grid — ``v3_stack_norm`` recency + opponent-
adjusted, totals-anchored lambdas, corrected Dixon-Coles with per-competition
rho — exactly as production serves it. Walk-forward and the train/test split are
preserved, so nothing sees its own future and the scored sample matches the V2
run's basis.

Nothing is inherited from V2. The competition-level withheld set is re-derived
from V3 layer-1 verdicts (never copied), then fed to layer 2 by replacing the
module's ``COMPETITION_WITHHELD`` data — the verdict code is untouched. Decisions
persist under the V3 model version, so V2 and V3 capability history stay
independently auditable. Clean-sheet services settle through the unchanged
canonical ``settles_won`` (``home == 0 or away == 0``).

    railway ssh "python scripts/service_gate_v3.py"          # evaluate + report + transition matrix
    railway ssh "python scripts/service_gate_v3.py --apply"  # also persist V3 capability rows
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import scripts.service_gate as sg
import scripts.validate_wave1 as vw
from app.core.competitions import code_for_name
from app.core.config import get_settings
from app.database.models import HistoricalMatch, ServiceCapability
from app.database.models.team import Competition as CompetitionRow
from app.infrastructure.database import Database
from app.quant import v3_stack_norm as v3
from app.quant.grid import build_match_probabilities
from app.quant.markets import derive_markets, settles_won
from app.services.best_of_day import SERVICES, model_only_version
from app.services.capability_gate import CapabilityGate
from app.services.v3_pipeline import HISTORY_WINDOW_DAYS, V3_VERSION
from scripts.seed_capabilities import LEAGUE_CODES

RULE = "=" * 100
VALIDATION_VERSION = "wave1-walkforward-v3"
V2_MATRIX = Path(__file__).resolve().parents[1] / "data" / "wave1_capabilities.json"

# Layer-1 (competition) and layer-2 (service) constants come straight from the
# frozen methodology modules — imported, never restated.
MIN_HISTORY = vw.MIN_HISTORY
MIN_TEAM_MATCHES = vw.MIN_TEAM_MATCHES
OUTCOMES = vw.OUTCOMES
WAVE_1 = vw.WAVE_1


async def _walk_v3(
    session: AsyncSession, league_id: int, label: str
) -> tuple[vw.Validation, list[sg.Capability]]:
    """One chronological walk of a competition, forecasting with the V3 grid.

    Populates both an imported layer-1 ``Validation`` and the imported layer-2
    ``Capability`` objects from the *same* per-fixture V3 grid. Every gating and
    scoring rule is the frozen one; only the grid is V3.
    """
    code = code_for_name(label)
    validation = vw.Validation(league_id=league_id, label=label, grid_version=V3_VERSION)
    capabilities: dict[str, sg.Capability] = {
        service.key: sg.Capability(
            league_id=league_id,
            competition=label,
            service_key=service.key,
            service_label=service.label,
            market=service.market,
            outcome=service.outcome,
            threshold=service.min_probability,
            model_version=V3_VERSION,
        )
        for service in SERVICES
    }

    found = await session.execute(
        select(CompetitionRow.id).where(CompetitionRow.canonical_name == label)
    )
    competition_id = found.scalar_one_or_none()
    if competition_id is None:
        return validation, list(capabilities.values())

    rows = await session.execute(
        select(HistoricalMatch)
        .where(HistoricalMatch.competition_id == competition_id)
        .order_by(HistoricalMatch.match_date)
    )
    matches = list(rows.scalars().all())
    validation.training_rows = len(matches)
    if len(matches) < MIN_HISTORY * 2:
        return validation, list(capabilities.values())

    split = len(matches) // 2

    # Eligibility counters (identical to the frozen scripts) and the growing V3
    # pool of prior results within the production history window.
    scored_for: dict[int, list[tuple[int, int]]] = defaultdict(list)
    scored_against: dict[int, list[tuple[int, int]]] = defaultdict(list)
    pool: list[tuple[int, tuple[int, int, int, int, int]]] = []  # (ordinal, match tuple)

    for index, match in enumerate(matches):
        home_id, away_id = match.home_team_id, match.away_team_id
        home_goals, away_goals = match.home_goals, match.away_goals
        ordinal = match.match_date.toordinal()

        # ``index`` == matches processed so far == the frozen scripts' ``played``.
        seen_home = len(scored_for[home_id]) + len(scored_against[home_id])
        seen_away = len(scored_for[away_id]) + len(scored_against[away_id])
        eligible = (
            index >= MIN_HISTORY
            and seen_home >= MIN_TEAM_MATCHES
            and seen_away >= MIN_TEAM_MATCHES
        )

        if eligible and index >= split:
            cutoff = ordinal - HISTORY_WINDOW_DAYS
            window = [t for (o, t) in pool if o >= cutoff]
            lambdas = v3.v3_expected_goals(window, home_id, away_id, ordinal)
            if lambdas is not None:
                mp = build_match_probabilities(
                    lambdas[0], lambdas[1], corrected=True, competition=code
                )
                grid = mp.scoreline
                markets = derive_markets(grid)

                actual = (
                    "home"
                    if home_goals > away_goals
                    else ("away" if away_goals > home_goals else "draw")
                )
                validation.forecasts.append(
                    vw.Forecast(
                        match_date=match.match_date,
                        probabilities={
                            "home": float(mp.home_win),
                            "draw": float(mp.draw),
                            "away": float(mp.away_win),
                        },
                        actual=actual,
                        home_goals=home_goals,
                        away_goals=away_goals,
                        lambda_home=lambdas[0],
                        lambda_away=lambdas[1],
                    )
                )

                for service in SERVICES:
                    capability = capabilities[service.key]
                    probability = float(
                        markets.get(service.market, {}).get(service.outcome, 0)
                    )
                    hit = bool(
                        settles_won(service.market, service.outcome, home_goals, away_goals)
                    )
                    capability.total += 1
                    capability.all_predicted += probability
                    capability.all_won += 1 if hit else 0
                    if probability >= service.min_probability:
                        capability.qualifying += 1
                        capability.tail_predicted += probability
                        capability.tail_won += 1 if hit else 0
                        capability.brier_total += (probability - (1.0 if hit else 0.0)) ** 2
                        capability.window_dates.append((match.match_date, probability, hit))
                        for band_index, (low, high) in enumerate(sg.BANDS):
                            if low <= probability < high:
                                capability.bands[band_index].append((probability, hit))
                                break

        scored_for[home_id].append((home_goals, away_goals))
        scored_against[away_id].append((away_goals, home_goals))
        pool.append((ordinal, (home_id, away_id, home_goals, away_goals, ordinal)))

    for capability in capabilities.values():
        capability.compute_windows()
    return validation, list(capabilities.values())


def _load_v2() -> dict[str, dict[str, object]]:
    """The frozen V2 matrix, for the transition comparison only."""
    try:
        return dict(json.loads(V2_MATRIX.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="persist V3 ServiceCapability rows")
    args = parser.parse_args()

    version = model_only_version()
    print(RULE)
    print("V3 WAVE-1 CAPABILITY VALIDATION")
    print(RULE)
    print(f"  serving model version : {version}")
    if version != V3_VERSION:
        print(f"  REFUSING: active engine is not {V3_VERSION}. Activate V3 before validating it.")
        print(RULE)
        return 1
    print(f"  validation version    : {VALIDATION_VERSION}")
    print("  methodology           : imported unchanged (validate_wave1 + service_gate)")
    print("  probability engine    : canonical V3 grid (v3_stack_norm)")
    print(f"  expected decisions    : {len(SERVICES)} services x {len(WAVE_1)} competitions"
          f" = {len(SERVICES) * len(WAVE_1)}")
    print(RULE)

    database = Database(get_settings())
    await database.connect()
    walked: dict[int, tuple[vw.Validation, list[sg.Capability]]] = {}
    try:
        async with database.session() as session:
            for league_id, label in WAVE_1.items():
                print(f"  evaluating {label} ...", flush=True)
                walked[league_id] = await _walk_v3(session, league_id, label)
    finally:
        pass

    # ── Layer 1: re-derive the competition-withheld set from V3 (not inherited) ──
    v3_withheld: dict[int, str] = {}
    comp_state: dict[int, tuple[str, list[str]]] = {}
    for league_id, (validation, _) in walked.items():
        state, reasons = validation.verdict()
        comp_state[league_id] = (state, reasons)
        if state == "WITHHELD":
            v3_withheld[league_id] = "; ".join(reasons) or "competition gate"
    # Feed layer 2 by replacing the data the frozen verdict reads — code untouched.
    sg.COMPETITION_WITHHELD = v3_withheld

    # ── Layer 2: per-service verdicts under V3 ──
    all_caps: list[sg.Capability] = []
    for _, caps in walked.values():
        all_caps.extend(caps)

    states: dict[str, int] = defaultdict(int)
    reasons: dict[str, int] = defaultdict(int)
    decisions: dict[tuple[int, str], tuple[str, str]] = {}
    for cap in all_caps:
        state, reason = cap.verdict()
        states[state] += 1
        reasons[reason] += 1
        decisions[(cap.league_id, cap.service_key)] = (state, reason)

    evaluated = len(all_caps)
    expected = len(SERVICES) * len(WAVE_1)

    print("\n" + RULE)
    print("COMPETITION-LEVEL 1X2 GATE (V3, re-derived — layer 1)")
    print(RULE)
    for league_id, label in WAVE_1.items():
        state, rs = comp_state[league_id]
        val = walked[league_id][0]
        tag = "WITHHELD" if state == "WITHHELD" else "PASS"
        detail = f"  ({'; '.join(rs)})" if rs else ""
        print(f"  {label:<22} {tag:<9} forecasts={val.count:<5} "
              f"Brier={val.brier:.4f} bias={val.max_bias:.1%}{detail}")

    print("\n" + RULE)
    print("PER-SERVICE CAPABILITY DECISIONS (V3, methodology unchanged — layer 2)")
    print(RULE)
    print(f"  {expected} expected / {evaluated} evaluated")
    print(f"  ACTIVE                 : {states['ACTIVE']}")
    print(f"  WITHHELD               : {states['WITHHELD']}")
    print(f"  INSUFFICIENT_EVIDENCE  : {states['INSUFFICIENT_EVIDENCE']}")
    print("\n  reason codes:")
    for reason in (
        "VALIDATED",
        "COMPETITION_WITHHELD",
        "CALIBRATION_FAIL",
        "INTERVAL_TOO_WIDE",
        "INSUFFICIENT_SAMPLE",
        "INSTABILITY",
    ):
        print(f"    {reason:<24}{reasons.get(reason, 0):>5}")

    # ── V2 → V3 transition matrix ──
    v2 = _load_v2()
    v2_state: dict[tuple[int, str], str] = {}
    for key, row in v2.items():
        try:
            lid = int(str(key).split(":", 1)[0])
        except ValueError:
            continue
        v2_state[(lid, str(row.get("service_key")))] = str(row.get("state"))

    stayed = became = lost = remained = uncompared = 0
    became_list: list[str] = []
    lost_list: list[str] = []
    for (lid, skey), (v3_state, _) in decisions.items():
        prior = v2_state.get((lid, skey))
        if prior is None:
            uncompared += 1
            continue
        v2_active = prior == "ACTIVE"
        v3_active = v3_state == "ACTIVE"
        if v2_active and v3_active:
            stayed += 1
        elif not v2_active and v3_active:
            became += 1
            became_list.append(f"{WAVE_1.get(lid, lid)} / {skey}  (V2 {prior})")
        elif v2_active and not v3_active:
            lost += 1
            lost_list.append(f"{WAVE_1.get(lid, lid)} / {skey}  (V3 {v3_state})")
        else:
            remained += 1

    print("\n" + RULE)
    print("V2 → V3 TRANSITION MATRIX")
    print(RULE)
    print(f"  stayed ACTIVE          : {stayed}")
    print(f"  became ACTIVE          : {became}")
    print(f"  lost ACTIVE            : {lost}")
    print(f"  remained withheld/insuf: {remained}")
    if uncompared:
        print(f"  (no V2 record to compare: {uncompared})")
    if became_list:
        print("\n  newly ACTIVE under V3:")
        for line in sorted(became_list):
            print(f"    + {line}")
    if lost_list:
        print("\n  lost ACTIVE under V3:")
        for line in sorted(lost_list):
            print(f"    - {line}")

    reconciles = evaluated == expected
    print("\n" + RULE)
    verdict = "PASS" if reconciles else "FAIL"
    print(f"  V3 WAVE-1 CAPABILITY VALIDATION — {verdict}")
    print(f"  {expected} expected / {evaluated} evaluated")
    if not reconciles:
        print("  DOES NOT RECONCILE — nothing will be written. Fail closed.")
        print(RULE)
        await database.disconnect()
        return 1

    if args.apply:
        now = datetime.now(UTC)
        async with database.session() as session:
            created = updated = 0
            for cap in all_caps:
                mapping = LEAGUE_CODES.get(str(cap.league_id))
                if mapping is None:
                    continue
                code, name = mapping
                state, reason = cap.verdict()
                low, high = cap.interval
                existing = await session.execute(
                    select(ServiceCapability).where(
                        ServiceCapability.competition_code == code,
                        ServiceCapability.service_key == cap.service_key,
                        ServiceCapability.model_version == version,
                    )
                )
                row = existing.scalar_one_or_none()
                if row is None:
                    row = ServiceCapability(
                        competition_code=code,
                        service_key=cap.service_key,
                        model_version=version,
                        competition_name=name,
                        service_label=cap.service_label,
                        validation_version=VALIDATION_VERSION,
                        state=state,
                        reason=reason,
                        validated_at=now,
                    )
                    session.add(row)
                    created += 1
                else:
                    updated += 1
                row.provider_league_id = cap.league_id
                row.competition_name = name
                row.service_label = cap.service_label
                row.validation_version = VALIDATION_VERSION
                row.state = state
                row.reason = reason
                row.threshold = cap.threshold
                row.qualifying_sample = cap.qualifying
                row.total_sample = cap.total
                row.predicted_rate = cap.tail_mean_predicted if cap.qualifying else None
                row.observed_rate = cap.tail_observed if cap.qualifying else None
                row.calibration_gap = cap.tail_gap if cap.qualifying else None
                row.interval_low = low if cap.qualifying else None
                row.interval_high = high if cap.qualifying else None
                row.brier = cap.brier if cap.qualifying else None
                row.window_spread = cap.window_spread
                row.validated_at = now
            await session.commit()
        print(f"  APPLIED — {created} created, {updated} updated, under {version}.")
        print(f"  Publishable (ACTIVE) : {states['ACTIVE']}.  Everything else fails closed.")

        # ── Dry publication check: read the rows back through the REAL gate ──
        # This is exactly the decision tomorrow's publish() makes per fixture.
        print("\n" + RULE)
        print("DRY PUBLICATION CHECK — via the live CapabilityGate (V3 rows just written)")
        print(RULE)
        allowed = 0
        allow_list: list[str] = []
        async with database.session() as session:
            gate = CapabilityGate(session)
            for league_id, label in WAVE_1.items():
                mapping = LEAGUE_CODES.get(str(league_id))
                if mapping is None:
                    continue
                code, _ = mapping
                for service in SERVICES:
                    decision = await gate.may_publish(code, service.key, version)
                    if decision:
                        allowed += 1
                        allow_list.append(f"{label} / {service.key}")
        print(f"  gate model version    : {version}")
        print(f"  publishable pairs      : {allowed}   (should equal ACTIVE = {states['ACTIVE']})")
        match = "MATCH" if allowed == states["ACTIVE"] else "MISMATCH"
        print(f"  reconciliation         : {match}")
        for line in sorted(allow_list):
            print(f"    ✓ {line}")
        print("\n  Every other Wave-1 pair returns a blocked decision — fail-closed.")
        print("  Established (ungated) competitions are unaffected and publish under V3 as before.")
    else:
        print("  DRY RUN — no rows written. Re-run with --apply to persist.")
    print(RULE)
    await database.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
