"""Redis connectivity.

Redis is a mandatory dependency. It backs caching, rate limiting, job state,
distributed locks and the cross-process heartbeats used by readiness probes.
"""

from __future__ import annotations

import asyncio

from redis.asyncio import ConnectionPool, Redis

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class RedisClient:
    """Owns the Redis connection pool for one process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: ConnectionPool | None = None
        self._client: Redis | None = None

    @property
    def client(self) -> Redis:
        """Return the Redis client, raising if not yet connected."""
        if self._client is None:
            raise RuntimeError("RedisClient.connect() has not been called.")
        return self._client

    def key(self, *parts: str) -> str:
        """Build an environment-prefixed Redis key."""
        return self._settings.redis.namespaced(*parts)

    async def connect(self) -> None:
        """Create the connection pool and client."""
        cfg = self._settings.redis
        self._pool = ConnectionPool.from_url(
            cfg.dsn,
            max_connections=cfg.max_connections,
            socket_timeout=cfg.socket_timeout_seconds,
            socket_connect_timeout=cfg.socket_connect_timeout_seconds,
            decode_responses=True,
        )
        self._client = Redis(connection_pool=self._pool)
        logger.info("redis.configured", dsn=cfg.safe_dsn, max_connections=cfg.max_connections)

    async def disconnect(self) -> None:
        """Close the client and release pooled connections."""
        if self._client is not None:
            await self._client.aclose()
        if self._pool is not None:
            await self._pool.disconnect()
        self._client = None
        self._pool = None
        logger.info("redis.disconnected")

    async def ping(self, timeout: float | None = None) -> bool:
        """Return ``True`` if Redis responds to PING within ``timeout``."""
        if self._client is None:
            return False
        limit = timeout or self._settings.observability.readiness_timeout_seconds
        try:
            async with asyncio.timeout(limit):
                return bool(await self._client.ping())
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            # TimeoutError is an Exception subclass, so this also covers the
            # asyncio.timeout() expiry above.
            logger.warning("redis.ping_failed", error=str(exc), error_type=type(exc).__name__)
            return False
