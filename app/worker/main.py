"""Background worker service entrypoint.

The worker owns all heavy and scheduled work: market scanning, historical
ingestion, odds capture, quantitative computation and backtests. None of that
exists yet — Phase 1 registers only an infrastructure self-check job, which
proves the scheduler runs, holds a distributed lock, and shuts down cleanly.

Run with::

    python -m app.worker.main
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import ServiceRole, Settings, get_settings
from app.core.health import build_health_report
from app.core.logging import configure_logging, get_logger, set_correlation_id
from app.infrastructure.database import Database
from app.infrastructure.heartbeat import HeartbeatWriter
from app.infrastructure.redis import RedisClient
from app.providers.live import live_odds_provider
from app.services.daily_scan import DailyScanService
from app.services.highlights import HighlightService
from app.services.settlement import SettlementService
from app.utils.lifecycle import run_until_shutdown

logger = get_logger(__name__)

SELF_CHECK_INTERVAL_SECONDS = 60

SETTLEMENT_INTERVAL_SECONDS = 60 * 60
"""How often to settle finished fixtures.

Hourly. Settlement costs one batched request per twenty fixtures, so it is
cheap, and a match finishing just after a run should not wait three hours to be
recorded.
"""

SCAN_INTERVAL_SECONDS = 3 * 60 * 60
"""How often to re-analyse the fixture card.

Three hours balances freshness against the provider's 100-request daily
allowance: eight scans a day, each spending a handful of requests for fixtures
plus one per priced fixture.
"""
_LOCK_TTL_SECONDS = 30


async def with_lock(redis: RedisClient, name: str, job: Callable[[], Awaitable[None]]) -> None:
    """Run ``job`` only if this process wins the named Redis lock.

    Scheduled jobs must not double-execute when more than one worker replica is
    running. Established in Phase 1 so later jobs inherit the pattern rather
    than retrofitting it.

    Args:
        redis: Connected Redis client.
        name: Lock name, namespaced automatically.
        job: Coroutine factory to run while holding the lock.
    """
    key = redis.key("lock", name)
    acquired = await redis.client.set(key, "1", nx=True, ex=_LOCK_TTL_SECONDS)
    if not acquired:
        logger.debug("worker.job_skipped_locked", job=name)
        return
    try:
        await job()
    finally:
        await redis.client.delete(key)


def build_scheduler(settings: Settings, database: Database, redis: RedisClient) -> AsyncIOScheduler:
    """Create the scheduler and register Phase 1 jobs."""
    scheduler = AsyncIOScheduler(timezone="UTC")

    async def infrastructure_self_check() -> None:
        """Log a health report so worker-side outages are visible in logs."""
        set_correlation_id(None)
        report = await build_health_report(settings, database, redis, include_siblings=False)
        logger.info(
            "worker.self_check",
            status=str(report.status),
            components={c.name: str(c.status) for c in report.components},
        )

    async def daily_scan() -> None:
        """Analyse every upcoming fixture and store the results.

        Runs on a schedule so the bot serves precomputed analyses instantly.
        Failures are logged, never raised: a scan that dies must not stop the
        scheduler, and yesterday's stored analyses remain readable.
        """
        set_correlation_id(None)
        provider = live_odds_provider()
        if provider is None:
            logger.info("scan.skipped_no_provider")
            return
        try:
            async with database.session() as session:
                report = await DailyScanService(session).scan(provider)
            logger.info("scan.stored", summary=report.summary())

            # Highlights are chosen straight after the scan, from fixtures that
            # have not kicked off. Recording them at selection time is what
            # makes the track record trustworthy — a selection written after
            # the result is not a prediction.
            async with database.session() as session:
                chosen = await HighlightService(session).record_daily()
            logger.info("highlight.recorded", summary=chosen.summary())
        except Exception as exc:
            logger.exception("scan.failed", error_type=type(exc).__name__)

    scheduler.add_job(
        with_lock,
        trigger=IntervalTrigger(seconds=SCAN_INTERVAL_SECONDS),
        args=[redis, "daily-fixture-scan", daily_scan],
        id="daily_fixture_scan",
        name="Daily fixture scan",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        replace_existing=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=20),
    )

    async def settle_finished() -> None:
        """Compare finished fixtures against what we predicted.

        Runs more often than the scan: settlement is cheap, and a result that
        arrives late should still be recorded rather than waiting hours.
        """
        set_correlation_id(None)
        provider = live_odds_provider()
        if provider is None or not hasattr(provider, "get_results"):
            logger.info("settlement.skipped_no_results_source")
            return
        try:
            async with database.session() as session:
                report = await SettlementService(session).settle(provider)
                settled = await HighlightService(session).settle()
            logger.info("settlement.stored", summary=report.summary(), highlights=settled)
        except Exception as exc:
            logger.exception("settlement.failed", error_type=type(exc).__name__)

    scheduler.add_job(
        with_lock,
        trigger=IntervalTrigger(seconds=SETTLEMENT_INTERVAL_SECONDS),
        args=[redis, "settle-finished-fixtures", settle_finished],
        id="settle_finished_fixtures",
        name="Settle finished fixtures",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        replace_existing=True,
        next_run_time=datetime.now(UTC) + timedelta(seconds=45),
    )

    scheduler.add_job(
        with_lock,
        trigger=IntervalTrigger(seconds=SELF_CHECK_INTERVAL_SECONDS),
        args=[redis, "infrastructure-self-check", infrastructure_self_check],
        id="infrastructure_self_check",
        name="Infrastructure self-check",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
        replace_existing=True,
    )
    return scheduler


async def run_worker(settings: Settings) -> None:
    """Run the worker until a shutdown signal is received."""
    database = Database(settings)
    redis = RedisClient(settings)
    heartbeat = HeartbeatWriter(redis, ServiceRole.WORKER, settings)
    scheduler: AsyncIOScheduler | None = None

    async def startup() -> None:
        nonlocal scheduler
        await database.connect()
        await redis.connect()
        if not await redis.ping():
            raise RuntimeError("Redis is unreachable; the worker cannot start.")
        await heartbeat.start()
        scheduler = build_scheduler(settings, database, redis)
        scheduler.start()
        logger.info(
            "worker.scheduler_started",
            jobs=[job.id for job in scheduler.get_jobs()],
        )

    async def shutdown() -> None:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=True)
        await heartbeat.stop()
        await redis.disconnect()
        await database.disconnect()

    await run_until_shutdown("worker", startup, shutdown)


def main() -> None:
    """Configure the process and run the worker."""
    settings = get_settings()
    settings.service_role = ServiceRole.WORKER
    configure_logging(settings)
    settings.validate_runtime()
    asyncio.run(run_worker(settings))


if __name__ == "__main__":
    main()
