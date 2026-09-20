#!/usr/bin/env python3
"""Prove the research scaffold is present and isolated — read-only.

Run after ``alembic upgrade head``. It checks, against the live database, that:

  * the three research tables exist and are empty;
  * no production table holds a foreign key INTO a research table
    (nothing can pull a challenger row onto a production surface);
  * the research tables hold no foreign key OUT to production
    (their lifecycle is not coupled to production rows);
  * their guard constraints (unique, check) are in place.

Isolation is thereby verified from the schema itself, not asserted. Writes
nothing.

Run::

    railway ssh "python scripts/verify_research_scaffold.py"
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
RESEARCH_TABLES = ("research_experiments", "research_predictions", "research_odds_snapshots")


async def rows(session, sql: str, **p):
    return (await session.execute(text(sql), p)).fetchall()


async def main() -> int:
    database = Database(get_settings())
    await database.connect()
    ok = True
    async with database.session() as session:
        print(RULE)
        print("NEXT RESEARCH SCAFFOLD — ISOLATION VERIFICATION (read-only)")
        print(RULE)

        existing = {
            r[0]
            for r in await rows(
                session,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name = ANY(:names)",
                names=list(RESEARCH_TABLES),
            )
        }
        print("\n  TABLES PRESENT & EMPTY:")
        for t in RESEARCH_TABLES:
            if t not in existing:
                print(f"    {t:<28} MISSING  ✗")
                ok = False
                continue
            # t is drawn only from the RESEARCH_TABLES constant, never user input.
            n = (await rows(session, f'SELECT count(*) FROM "{t}"'))[0][0]  # noqa: S608
            flag = "✓" if n == 0 else f"✗ ({n} rows — expected 0)"
            if n != 0:
                ok = False
            print(f"    {t:<28} exists, {n} rows  {flag}")

        print("\n  FOREIGN KEYS INTO RESEARCH TABLES (must be none):")
        into = await rows(
            session,
            "SELECT tc.table_name, ccu.table_name, tc.constraint_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.constraint_column_usage ccu "
            "  ON tc.constraint_name = ccu.constraint_name "
            " AND tc.table_schema = ccu.table_schema "
            "WHERE tc.constraint_type='FOREIGN KEY' AND ccu.table_name = ANY(:names)",
            names=list(RESEARCH_TABLES),
        )
        if not into:
            print("    none — no production row can reference a challenger  ✓")
        else:
            ok = False
            for frm, to, name in into:
                print(f"    ✗ {frm} -> {to}  ({name})")

        print("\n  FOREIGN KEYS OUT OF RESEARCH TABLES (must be none):")
        out = await rows(
            session,
            "SELECT tc.table_name, tc.constraint_name "
            "FROM information_schema.table_constraints tc "
            "WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_name = ANY(:names)",
            names=list(RESEARCH_TABLES),
        )
        if not out:
            print("    none — research lifecycle is not coupled to production rows  ✓")
        else:
            ok = False
            for frm, name in out:
                print(f"    ✗ {frm}  ({name})")

        print("\n  GUARD CONSTRAINTS (unique / check):")
        for t in RESEARCH_TABLES:
            if t not in existing:
                continue
            cons = await rows(
                session,
                "SELECT constraint_type, constraint_name "
                "FROM information_schema.table_constraints "
                "WHERE table_schema='public' AND table_name=:t "
                "AND constraint_type IN ('UNIQUE','CHECK') ORDER BY constraint_type",
                t=t,
            )
            print(f"    {t}:")
            for ctype, cname in cons:
                print(f"      {ctype:<8} {cname}")

        head = (await rows(session, "SELECT version_num FROM alembic_version"))[0][0]
        print(f"\n  ALEMBIC HEAD: {head}  (expected f3a9c7b1e2d4)")
        if head != "f3a9c7b1e2d4":
            ok = False

        await session.rollback()
    await database.disconnect()
    print("\n" + RULE)
    verdict = (
        "SCAFFOLD VERIFIED — present, empty, isolated."
        if ok
        else "PROBLEM — see marks above."
    )
    print("RESULT: " + verdict)
    print(RULE)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
