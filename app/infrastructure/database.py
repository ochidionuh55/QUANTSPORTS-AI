"""PostgreSQL connectivity.

Phase 1 provides the engine, the session factory and a connectivity probe only.
ORM models, the declarative base and Alembic migrations arrive in Phase 2.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Database:
    """Owns the async engine and session factory for one process.

    One instance per process, created during startup and disposed during
    shutdown. Holding it on the application state (rather than in a module
    global) keeps tests able to build isolated instances.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        """Return the engine, raising if the database has not been started."""
        if self._engine is None:
            raise RuntimeError("Database.connect() has not been called.")
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the session factory, raising if not yet started."""
        if self._session_factory is None:
            raise RuntimeError("Database.connect() has not been called.")
        return self._session_factory

    async def connect(self) -> None:
        """Create the engine and session factory.

        Does not open a connection; pooling is lazy. Use :meth:`ping` to verify
        that the database is actually reachable.
        """
        pg = self._settings.postgres
        self._engine = create_async_engine(
            pg.dsn,
            echo=pg.echo_sql or self._settings.observability.log_sql_queries,
            pool_size=pg.pool_size,
            max_overflow=pg.max_overflow,
            pool_timeout=pg.pool_timeout_seconds,
            pool_recycle=pg.pool_recycle_seconds,
            pool_pre_ping=True,
            connect_args={"timeout": pg.connect_timeout_seconds},
        )
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        logger.info("database.configured", dsn=pg.safe_dsn, pool_size=pg.pool_size)

    async def disconnect(self) -> None:
        """Dispose of the engine and release pooled connections."""
        if self._engine is not None:
            await self._engine.dispose()
            logger.info("database.disconnected")
        self._engine = None
        self._session_factory = None

    async def ping(self, timeout: float | None = None) -> bool:
        """Return ``True`` if a trivial query succeeds within ``timeout``.

        Args:
            timeout: Seconds to wait. Defaults to the readiness timeout.
        """
        if self._engine is None:
            return False
        limit = timeout or self._settings.observability.readiness_timeout_seconds
        try:
            async with asyncio.timeout(limit):
                async with self._engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            # TimeoutError is an Exception subclass, so this also covers the
            # asyncio.timeout() expiry above.
            logger.warning("database.ping_failed", error=str(exc), error_type=type(exc).__name__)
            return False

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, committing on success and rolling back on error."""
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
