#!/usr/bin/env python3
"""Read-only research data inventory for QUANTSPORT NEXT — Phase 1.

**Verify actual data, never infer from the schema or provider docs.** This
script only ever reads: every statement is a ``SELECT`` (or an
``information_schema`` lookup), the session is rolled back, and nothing is
written. It exists to answer one question with evidence — *what do we actually
possess, and how much* — so the research map is built on rows, not promises.

It touches no modelling code and no production state. ``model-only-v2-dc``
and every published prediction are read, never altered.

Run::

    railway ssh "python scripts/research_inventory.py"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.config import get_settings
from app.infrastructure.database import Database

RULE = "=" * 92
SUB = "-" * 92


def h(title: str) -> None:
    print("\n" + RULE)
    print(title)
    print(RULE)


async def scalar(session, sql: str, **params):
    result = await session.execute(text(sql), params)
    return result.scalar()


async def rows(session, sql: str, **params):
    result = await session.execute(text(sql), params)
    return result.fetchall()


async def table_names(session) -> set[str]:
    found = await rows(
        session,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name",
    )
    return {r[0] for r in found}


async def columns_of(session, table: str) -> set[str]:
    found = await rows(
        session,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t",
        t=table,
    )
    return {r[0] for r in found}


async def count(session, table: str) -> int:
    return int(await scalar(session, f'SELECT count(*) FROM "{table}"') or 0)


async def non_null(session, table: str, column: str) -> int:
    return int(
        await scalar(
            session,
            f'SELECT count(*) FILTER (WHERE "{column}" IS NOT NULL) FROM "{table}"',
        )
        or 0
    )


async def main() -> int:
    database = Database(get_settings())
    await database.connect()
    async with database.session() as session:
        tables = await table_names(session)

        # ── 0. Ground truth: every table and its row count ───────────────────
        h("0. RAW SCHEMA — every public table and its row count")
        widths = max((len(t) for t in tables), default=20)
        for t in sorted(tables):
            print(f"  {t:<{widths}}  {await count(session, t):>12,}")

        # ── 1. Feature-column census: what feature families exist ANYWHERE ───
        h("1. FEATURE-COLUMN CENSUS — does a column for this feature exist at all?")
        families = {
            "xG": ["%xg%", "%expected_goal%", "%npxg%"],
            "shots": ["%shot%"],
            "possession": ["%possession%"],
            "corners/cards": ["%corner%", "%card%", "%yellow%", "%red%"],
            "lineups": ["%lineup%", "%line_up%", "%starting%"],
            "injuries/suspensions": ["%injur%", "%suspen%", "%unavailable_player%"],
            "rest/congestion": ["%rest%", "%congest%", "%days_since%"],
            "elo": ["%elo%"],
            "form/recency": ["%form%", "%recency%", "%streak%"],
            "odds/price": ["%odd%", "%price%", "%implied%"],
            "half-time": ["%half_time%", "%ht_%"],
        }
        # NOTE: 'shot' also matches 'odds_snapshot' — read table.column, not the label.
        for label, patterns in families.items():
            clause = " OR ".join("column_name ILIKE :p%d" % i for i in range(len(patterns)))
            params = {f"p{i}": p for i, p in enumerate(patterns)}
            hits = await rows(
                session,
                "SELECT table_name, column_name FROM information_schema.columns "
                f"WHERE table_schema='public' AND ({clause}) ORDER BY table_name, column_name",
                **params,
            )
            print(f"\n  {label}:")
            if not hits:
                print("    (no column found — feature NOT stored)")
            for tbl, col in hits:
                print(f"    {tbl}.{col}")

        # ── 2. Historical corpus ─────────────────────────────────────────────
        if "historical_matches" in tables:
            h("2. HISTORICAL CORPUS — historical_matches")
            cols = await columns_of(session, "historical_matches")
            total = await count(session, "historical_matches")
            print(f"  total rows                     : {total:,}")
            print(
                "  distinct competitions          : "
                f"{await scalar(session, 'SELECT count(DISTINCT competition_id) FROM historical_matches WHERE competition_id IS NOT NULL')}"
            )
            print(
                "  rows with NULL competition_id  : "
                f"{await scalar(session, 'SELECT count(*) FROM historical_matches WHERE competition_id IS NULL'):,}"
            )
            if "season" in cols:
                print(
                    "  distinct seasons               : "
                    f"{await scalar(session, 'SELECT count(DISTINCT season) FROM historical_matches')}"
                )
            if "match_date" in cols:
                span = (await rows(session, "SELECT min(match_date), max(match_date) FROM historical_matches"))[0]
                print(f"  date span                      : {span[0]} → {span[1]}")
            for c in ("half_time_home_goals", "half_time_away_goals"):
                if c in cols:
                    print(f"  non-null {c:<22}: {await non_null(session, 'historical_matches', c):,} / {total:,}")
            if "data_version" in cols:
                print("\n  by data_version:")
                for dv, n in await rows(session, "SELECT data_version, count(*) FROM historical_matches GROUP BY data_version ORDER BY count(*) DESC"):
                    print(f"    {str(dv)[:40]:<42} {n:>10,}")
            print("\n  rows per competition (all):")
            for cid, n in await rows(session, "SELECT competition_id, count(*) FROM historical_matches GROUP BY competition_id ORDER BY count(*) DESC"):
                print(f"    competition_id={str(cid):<8} {n:>10,}")

        # ── 3. Teams / identity ──────────────────────────────────────────────
        for t in ("teams", "team_aliases", "aliases"):
            if t in tables:
                h(f"3. IDENTITY — {t}")
                print(f"  rows: {await count(session, t):,}")
                cols = await columns_of(session, t)
                if "country" in cols:
                    n_countries = await scalar(session, f'SELECT count(DISTINCT country) FROM "{t}"')
                    print(f"  distinct countries: {n_countries}")

        # ── 4. Live analysis store (features we persist per fixture) ──────────
        if "stored_analyses" in tables:
            h("4. STORED ANALYSES — per-fixture computed output (features we persist)")
            cols = await columns_of(session, "stored_analyses")
            total = await count(session, "stored_analyses")
            print(f"  total rows                     : {total:,}")
            if "kickoff" in cols:
                span = (await rows(session, "SELECT min(kickoff), max(kickoff) FROM stored_analyses"))[0]
                print(f"  kickoff span                   : {span[0]} → {span[1]}")
            for c in ("expected_home_goals", "expected_away_goals", "market_odds",
                      "market_probabilities", "model_probabilities", "component_views",
                      "home_stats", "away_stats", "coverage"):
                if c in cols:
                    print(f"  non-null {c:<22}: {await non_null(session, 'stored_analyses', c):,} / {total:,}")
            if "coverage" in cols:
                print("\n  by coverage grade:")
                for cov, n in await rows(session, "SELECT coverage, count(*) FROM stored_analyses GROUP BY coverage ORDER BY count(*) DESC"):
                    print(f"    {str(cov):<24} {n:>8,}")

        # ── 5. Odds infrastructure (historical vs prospective) ───────────────
        h("5. BOOKMAKER ODDS — what price data actually exists")
        for t in ("matches", "markets", "outcomes", "odds_snapshots"):
            if t in tables:
                print(f"  {t:<16} rows: {await count(session, t):,}")
        if "odds_snapshots" in tables:
            cols = await columns_of(session, "odds_snapshots")
            if "captured_at" in cols:
                span = (await rows(session, "SELECT min(captured_at), max(captured_at) FROM odds_snapshots"))[0]
                print(f"  odds_snapshots captured span: {span[0]} → {span[1]}")
            for c in ("provider_role", "snapshot_type", "provider_name"):
                if c in cols:
                    print(f"\n  odds_snapshots by {c}:")
                    for v, n in await rows(session, f'SELECT "{c}", count(*) FROM odds_snapshots GROUP BY "{c}" ORDER BY count(*) DESC'):
                        print(f"    {str(v):<28} {n:>10,}")
        if "outcomes" in tables:
            oc = await columns_of(session, "outcomes")
            if "odds" in oc:
                print(f"\n  outcomes with a price      : {await non_null(session, 'outcomes', 'odds'):,}")

        # ── 6. Prediction & settlement history (for research) ────────────────
        for t in ("service_selections", "highlight_selections"):
            if t in tables:
                h(f"6. PREDICTION/SETTLEMENT HISTORY — {t}")
                cols = await columns_of(session, t)
                total = await count(session, t)
                print(f"  total rows                     : {total:,}")
                if "status" in cols:
                    print("  by status:")
                    for st, n in await rows(session, f'SELECT status, count(*) FROM "{t}" GROUP BY status ORDER BY count(*) DESC'):
                        print(f"    {str(st):<20} {n:>8,}")
                if "model_version" in cols:
                    print("  by model_version:")
                    for mv, n in await rows(session, f'SELECT model_version, count(*) FROM "{t}" GROUP BY model_version ORDER BY count(*) DESC'):
                        print(f"    {str(mv):<28} {n:>8,}")
                date_col = "selection_date" if "selection_date" in cols else ("kickoff" if "kickoff" in cols else None)
                if date_col:
                    span = (await rows(session, f'SELECT min("{date_col}"), max("{date_col}") FROM "{t}"'))[0]
                    print(f"  {date_col} span: {span[0]} → {span[1]}")

        # ── 7. Model lineage ─────────────────────────────────────────────────
        if "model_versions" in tables:
            h("7. MODEL LINEAGE — model_versions")
            cols = await columns_of(session, "model_versions")
            select_cols = [c for c in ("name", "version", "status", "margin_method", "ensemble_method", "training_start", "training_end", "validation_start", "validation_end", "promoted_at") if c in cols]
            for r in await rows(session, f"SELECT {', '.join(select_cols)} FROM model_versions ORDER BY id"):
                print("  " + " | ".join(f"{c}={v}" for c, v in zip(select_cols, r)))
            for c in ("configuration", "feature_set", "validation_metrics"):
                if c in cols:
                    print(f"  non-null {c:<20}: {await non_null(session, 'model_versions', c)} / {await count(session, 'model_versions')}")

        # ── 8. Capability validation metrics (already-measured evidence) ─────
        if "service_capabilities" in tables:
            h("8. CAPABILITY VALIDATION — service_capabilities (measured metrics we already hold)")
            total = await count(session, "service_capabilities")
            print(f"  total rows: {total:,}")
            print("  by state:")
            for st, n in await rows(session, "SELECT state, count(*) FROM service_capabilities GROUP BY state ORDER BY count(*) DESC"):
                print(f"    {str(st):<24} {n:>6,}")
            cols = await columns_of(session, "service_capabilities")
            for c in ("brier", "calibration_gap", "observed_rate", "predicted_rate", "interval_low", "interval_high"):
                if c in cols:
                    print(f"  non-null {c:<18}: {await non_null(session, 'service_capabilities', c):,} / {total:,}")

        # ── 9. Scan telemetry — the candidate funnel (rejection reasons) ─────
        scan_tables = [t for t in tables if "scan" in t or "decision" in t or "telemetry" in t]
        if scan_tables:
            h("9. SCAN TELEMETRY — candidate funnel")
            for t in scan_tables:
                print(f"\n  {t}: {await count(session, t):,} rows")
                cols = await columns_of(session, t)
                for rc in ("rejection_code", "rejection_reason", "reason", "code", "qualified"):
                    if rc in cols:
                        print(f"    by {rc}:")
                        for v, n in await rows(session, f'SELECT "{rc}", count(*) FROM "{t}" GROUP BY "{rc}" ORDER BY count(*) DESC LIMIT 25'):
                            print(f"      {str(v):<40} {n:>8,}")
                        break

        await session.rollback()  # read-only: undo nothing, write nothing
    await database.disconnect()
    print("\n" + RULE)
    print("READ-ONLY INVENTORY COMPLETE — nothing was written.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
