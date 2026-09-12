"""Bring the database schema up to date automatically on startup.

**Why this is not a deployment step.** A schema migration that lives in a start
command or a console session is a step someone can forget, run against the
wrong service, or lose when a platform setting is edited. When it is missed the
failure is silent and confusing: the application starts, answers healthily, and
then every screen touching a new column fails with nothing shown to the user.
Making the application responsible for its own schema removes the entire class
of problem.

**Only one process migrates.** Three services start at once, so a Redis lock
decides which of them runs Alembic; the others wait for it to finish and then
continue. Without that, concurrent upgrades would race on the version table.

**Failure is loud but not fatal for readers.** If the migration cannot run, the
worker and API refuse to continue, because writing against a stale schema
corrupts data. The bot logs the problem and still starts, so users get a
working conversation rather than silence while the operator is alerted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.core.config import Settings
from app.core.logging import get_logger
from app.infrastructure.database import Database
from app.infrastructure.redis import RedisClient

logger = get_logger(__name__)

LOCK_KEY = "quantsport:schema:migrate"
LOCK_TIMEOUT_SECONDS = 600
"""How long a migration may hold the lock.

Long enough for a large upgrade, short enough that a crashed process does not
block every future deployment.
"""

WAIT_POLL_SECONDS = 2.0
WAIT_TIMEOUT_SECONDS = 300


def _config(settings: Settings) -> Config:
    """Build an Alembic configuration pointing at this project."""
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.postgres.dsn.replace("%", "%%"))
    return config


async def current_revision(database: Database) -> str | None:
    """Return the revision the database is currently at."""

    def _read(connection: object) -> str | None:
        context = MigrationContext.configure(connection)  # type: ignore[arg-type]
        return context.get_current_revision()

    async with database.engine.begin() as connection:
        return await connection.run_sync(_read)


def head_revision(settings: Settings) -> str | None:
    """Return the newest revision available in the codebase."""
    script = ScriptDirectory.from_config(_config(settings))
    return script.get_current_head()


async def schema_is_current(settings: Settings, database: Database) -> bool:
    """Whether the database already matches the code."""
    try:
        current = await current_revision(database)
    except Exception as exc:  # noqa: BLE001 - an unreachable database is not our error
        logger.warning("schema.check_failed", error=str(exc))
        return False
    return current is not None and current == head_revision(settings)


async def ensure_schema(
    settings: Settings,
    database: Database,
    redis: RedisClient | None = None,
    required: bool = True,
) -> bool:
    """Upgrade the database to the newest revision if it is behind.

    Args:
        settings: Application settings, for the database URL.
        database: Connected database, used to read the current revision.
        redis: Used to ensure only one service migrates. Without it the upgrade
            still runs, which is correct for single-process use.
        required: When true, a failure is raised so the service refuses to run
            against a schema it does not understand.

    Returns:
        Whether the schema is current once this call completes.
    """
    if await schema_is_current(settings, database):
        logger.info("schema.current", revision=head_revision(settings))
        return True

    if redis is None:
        return await _upgrade(settings, database, required)

    acquired = await _acquire(redis)
    if acquired:
        try:
            return await _upgrade(settings, database, required)
        finally:
            await _release(redis)

    # Another service is migrating. Wait for it rather than racing it.
    logger.info("schema.waiting_for_peer")
    waited = 0.0
    while waited < WAIT_TIMEOUT_SECONDS:
        await asyncio.sleep(WAIT_POLL_SECONDS)
        waited += WAIT_POLL_SECONDS
        if await schema_is_current(settings, database):
            logger.info("schema.peer_completed", waited_seconds=waited)
            return True

    logger.error("schema.peer_timeout", waited_seconds=waited)
    if required:
        raise SchemaOutOfDateError("Another service began migrating but did not finish in time.")
    return False


async def _upgrade(settings: Settings, database: Database, required: bool) -> bool:
    """Run Alembic to the newest revision."""
    target = head_revision(settings)

    # Reading the current revision can itself fail on an unreachable database,
    # so the whole sequence is guarded rather than only the upgrade call.
    try:
        before = await current_revision(database)
        logger.info("schema.upgrading", current=before, target=target)

        # Alembic is synchronous and opens its own connection, so it runs in a
        # worker thread to avoid blocking the event loop.
        await asyncio.to_thread(command.upgrade, _config(settings), "head")
        after = await current_revision(database)
    except Exception as exc:
        logger.exception("schema.upgrade_failed", error_type=type(exc).__name__)
        if required:
            raise SchemaOutOfDateError(f"Could not upgrade the database schema: {exc}") from exc
        return False

    logger.info("schema.upgraded", before=before, after=after)
    return after == target


async def _acquire(redis: RedisClient) -> bool:
    """Take the migration lock, if it is free."""
    try:
        client = redis.client
        return bool(await client.set(LOCK_KEY, "1", nx=True, ex=LOCK_TIMEOUT_SECONDS))
    except Exception as exc:  # noqa: BLE001 - a lock failure must not block startup
        logger.warning("schema.lock_unavailable", error=str(exc))
        return True


async def _release(redis: RedisClient) -> None:
    """Release the migration lock."""
    try:
        await redis.client.delete(LOCK_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("schema.unlock_failed", error=str(exc))


async def pending_revisions(settings: Settings, database: Database) -> list[str]:
    """Return revisions the database has not applied yet."""
    script = ScriptDirectory.from_config(_config(settings))
    current = await current_revision(database)
    return [
        revision.revision for revision in script.walk_revisions() if revision.revision != current
    ]


async def table_exists(database: Database, table: str) -> bool:
    """Whether a table is present, for health reporting."""
    async with database.engine.begin() as connection:
        result = await connection.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = :name"),
            {"name": table},
        )
        return result.first() is not None


class SchemaOutOfDateError(RuntimeError):
    """Raised when the database schema cannot be brought up to date.

    Writing against a schema the code does not understand corrupts data, so
    services that write refuse to start rather than continuing hopefully.
    """
