#!/usr/bin/env python3
"""Production smoke test.

Proves a deployment is genuinely working rather than merely running. Checks the
database, Redis, migrations, scheduled jobs, the live provider, stored analyses
and the Telegram connection, then reports each with a reason.

    docker compose -f docker-compose.prod.yml exec api python scripts/smoke_test.py

Exits non-zero if any required check fails, so it can gate a deployment or run
from cron.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, text

from app.core.config import ServiceRole, get_settings
from app.core.logging import configure_logging
from app.database.models import (
    HistoricalMatch,
    SettledPrediction,
    StoredAnalysis,
    Team,
)
from app.infrastructure.database import Database
from app.infrastructure.heartbeat import read_heartbeat
from app.infrastructure.redis import RedisClient
from app.providers.live import live_odds_provider

OK = "ok"
FAIL = "fail"
WARN = "warn"


class Report:
    """Collects check results."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str) -> None:
        """Record one check."""
        self.rows.append((status, name, detail))

    @property
    def failed(self) -> bool:
        """Whether any required check failed."""
        return any(status == FAIL for status, _, _ in self.rows)

    def render(self) -> str:
        """Return the report as text."""
        width = max(len(name) for _, name, _ in self.rows) + 2
        lines = ["", "QUANTSPORT SMOKE TEST", "=" * 72]
        for status, name, detail in self.rows:
            mark = {OK: "  ok  ", FAIL: " FAIL ", WARN: " warn "}[status]
            lines.append(f"[{mark}] {name.ljust(width)} {detail}")
        lines.append("=" * 72)
        lines.append("DEPLOYMENT UNHEALTHY" if self.failed else "DEPLOYMENT HEALTHY")
        return "\n".join(lines)


async def main() -> int:
    """Run every check and print the report."""
    settings = get_settings()
    settings.observability.log_level = "ERROR"
    configure_logging(settings)
    report = Report()
    moment = datetime.now(UTC)

    database = Database(settings)
    redis = RedisClient(settings)
    await database.connect()
    await redis.connect()

    try:
        # --- infrastructure ------------------------------------------------
        if await database.ping():
            report.add(OK, "PostgreSQL", "reachable")
        else:
            report.add(FAIL, "PostgreSQL", "unreachable")
            print(report.render())
            return 1

        report.add(
            OK if await redis.ping() else FAIL,
            "Redis",
            "reachable" if await redis.ping() else "unreachable",
        )

        async with database.session() as session:
            # --- migrations ------------------------------------------------
            revision = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
            report.add(
                OK if revision else FAIL,
                "Migrations",
                f"at {revision}" if revision else "alembic_version is empty",
            )

            # --- data ------------------------------------------------------
            matches = int(
                (
                    await session.execute(select(func.count()).select_from(HistoricalMatch))
                ).scalar_one()
            )
            report.add(
                OK if matches > 1000 else FAIL,
                "Historical data",
                f"{matches:,} matches" + ("" if matches > 1000 else " — run scripts/ingest.py"),
            )

            teams = int(
                (await session.execute(select(func.count()).select_from(Team))).scalar_one()
            )
            report.add(OK if teams else FAIL, "Canonical teams", f"{teams:,} teams")

            # --- worker output ---------------------------------------------
            latest = (
                await session.execute(
                    select(StoredAnalysis.computed_at)
                    .order_by(StoredAnalysis.computed_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest is None:
                report.add(WARN, "Scheduled scan", "no analyses stored yet")
            else:
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=UTC)
                age = moment - latest
                # The scan runs every three hours, so anything older than six
                # means two consecutive runs were missed.
                report.add(
                    OK if age < timedelta(hours=6) else FAIL,
                    "Scheduled scan",
                    f"last ran {age.total_seconds() / 3600:.1f}h ago",
                )

            upcoming = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(StoredAnalysis)
                        .where(StoredAnalysis.kickoff >= moment)
                    )
                ).scalar_one()
            )
            report.add(
                OK if upcoming else WARN,
                "Upcoming analyses",
                f"{upcoming} fixture(s) ready to serve",
            )

            settled = int(
                (
                    await session.execute(select(func.count()).select_from(SettledPrediction))
                ).scalar_one()
            )
            report.add(
                OK if settled else WARN,
                "Settled predictions",
                f"{settled:,} scored",
            )

        # --- sibling processes ---------------------------------------------
        for role in (ServiceRole.BOT, ServiceRole.WORKER):
            beat = await read_heartbeat(redis, role)
            report.add(
                OK if beat else FAIL,
                f"{str(role).title()} process",
                "heartbeat present" if beat else "no heartbeat — process is down",
            )

        # --- external provider ----------------------------------------------
        provider = live_odds_provider()
        if provider is None:
            report.add(FAIL, "Live provider", "API_FOOTBALL_KEY is not set")
        else:
            health = await provider.health_check()
            report.add(
                OK if health.healthy else WARN,
                "Live provider",
                health.detail or "reachable",
            )

        # --- telegram --------------------------------------------------------
        if not settings.telegram.is_configured:
            report.add(FAIL, "Telegram", "TELEGRAM__BOT_TOKEN is not set")
        else:
            from aiogram import Bot
            from aiogram.client.default import DefaultBotProperties

            bot = Bot(
                token=settings.telegram.bot_token.get_secret_value(),
                default=DefaultBotProperties(parse_mode="HTML"),
            )
            try:
                identity = await bot.get_me()
                report.add(OK, "Telegram", f"authenticated as @{identity.username}")
            except Exception as exc:  # noqa: BLE001 - report, never raise
                report.add(FAIL, "Telegram", f"{type(exc).__name__}: {exc}")
            finally:
                await bot.session.close()

        # --- safety ----------------------------------------------------------
        flags = settings.features
        report.add(
            OK if not flags.value_detection_enabled else FAIL,
            "Value gate",
            "disabled, as required until a model is promoted"
            if not flags.value_detection_enabled
            else "ENABLED — verify a model actually passed validation",
        )
    finally:
        await redis.disconnect()
        await database.disconnect()

    print(report.render())
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
