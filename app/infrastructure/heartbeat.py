"""Cross-process liveness heartbeats.

The bot and worker have no HTTP surface, so Kubernetes-style probes cannot
reach them directly. Instead each writes a short-lived key to Redis, and the
API reports on those keys. If a process dies, its key expires and the API's
``/health`` response degrades.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime

from app.core.config import ServiceRole, Settings
from app.core.logging import get_logger
from app.core.version import APP_VERSION, build_sha
from app.infrastructure.redis import RedisClient

logger = get_logger(__name__)

_HEARTBEAT_NAMESPACE = "heartbeat"


def heartbeat_key(redis: RedisClient, role: ServiceRole) -> str:
    """Return the Redis key holding the heartbeat for ``role``."""
    return redis.key(_HEARTBEAT_NAMESPACE, str(role))


async def write_heartbeat(redis: RedisClient, role: ServiceRole, settings: Settings) -> None:
    """Write a single heartbeat record for ``role``."""
    payload = json.dumps(
        {
            "service": str(role),
            "version": APP_VERSION,
            "build": build_sha(),
            "timestamp": datetime.now(UTC).isoformat(),
            "monotonic": time.monotonic(),
        }
    )
    await redis.client.set(
        heartbeat_key(redis, role),
        payload,
        ex=settings.observability.heartbeat_ttl_seconds,
    )


async def read_heartbeat(redis: RedisClient, role: ServiceRole) -> dict[str, object] | None:
    """Return the heartbeat record for ``role``, or ``None`` if it has expired."""
    try:
        raw = await redis.client.get(heartbeat_key(redis, role))
    except Exception as exc:  # noqa: BLE001 - probe must not raise
        logger.warning("heartbeat.read_failed", role=str(role), error=str(exc))
        return None
    if raw is None:
        return None
    try:
        return dict(json.loads(raw))
    except (TypeError, ValueError):
        logger.warning("heartbeat.malformed", role=str(role))
        return None


class HeartbeatWriter:
    """Background task that refreshes this process's heartbeat on an interval."""

    def __init__(self, redis: RedisClient, role: ServiceRole, settings: Settings) -> None:
        self._redis = redis
        self._role = role
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        """Write an immediate heartbeat, then begin the refresh loop."""
        await write_heartbeat(self._redis, self._role, self._settings)
        self._task = asyncio.create_task(self._loop(), name=f"heartbeat-{self._role}")
        logger.info(
            "heartbeat.started",
            role=str(self._role),
            interval=self._settings.observability.heartbeat_interval_seconds,
        )

    async def stop(self) -> None:
        """Stop the refresh loop and delete the heartbeat key."""
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        with contextlib.suppress(Exception):
            await self._redis.client.delete(heartbeat_key(self._redis, self._role))
        logger.info("heartbeat.stopped", role=str(self._role))

    async def _loop(self) -> None:
        """Refresh the heartbeat until cancelled, tolerating transient failures."""
        interval = self._settings.observability.heartbeat_interval_seconds
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(interval)
                await write_heartbeat(self._redis, self._role, self._settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                logger.warning("heartbeat.write_failed", role=str(self._role), error=str(exc))
