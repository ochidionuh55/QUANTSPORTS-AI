"""Health status aggregation.

Distinguishes three probe types, because conflating them causes container
orchestrators to restart healthy processes:

* **Liveness** - the process is running. Never depends on other services.
* **Readiness** - the process can serve traffic. Depends on Postgres and Redis.
* **Health** - a detailed human/monitoring view, including sibling processes.
"""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum

from pydantic import BaseModel, Field

from app.core.config import ServiceRole, Settings
from app.core.version import APP_PHASE, APP_VERSION, build_sha
from app.infrastructure.database import Database
from app.infrastructure.heartbeat import read_heartbeat
from app.infrastructure.redis import RedisClient


class ComponentStatus(StrEnum):
    """Status of a single dependency."""

    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class ComponentHealth(BaseModel):
    """Health of one dependency."""

    name: str
    status: ComponentStatus
    latency_ms: float | None = None
    detail: str | None = None
    required: bool = True


class HealthReport(BaseModel):
    """Aggregate health of this process and the dependencies it can see."""

    status: ComponentStatus
    service: ServiceRole
    environment: str
    version: str = APP_VERSION
    phase: str = APP_PHASE
    build: str = Field(default_factory=build_sha)
    components: list[ComponentHealth] = Field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        """True when every required component is up."""
        return all(c.status is ComponentStatus.UP for c in self.components if c.required)


async def _timed_probe(name: str, probe: object, required: bool = True) -> ComponentHealth:
    """Run an async boolean probe and record its latency.

    Args:
        name: Component name for the report.
        probe: A zero-argument awaitable returning ``bool``.
        required: Whether failure should make the process unready.
    """
    started = time.perf_counter()
    try:
        ok = bool(await probe())  # type: ignore[operator]
        elapsed = (time.perf_counter() - started) * 1000
        return ComponentHealth(
            name=name,
            status=ComponentStatus.UP if ok else ComponentStatus.DOWN,
            latency_ms=round(elapsed, 2),
            detail=None if ok else "probe returned false",
            required=required,
        )
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        elapsed = (time.perf_counter() - started) * 1000
        return ComponentHealth(
            name=name,
            status=ComponentStatus.DOWN,
            latency_ms=round(elapsed, 2),
            detail=f"{type(exc).__name__}: {exc}",
            required=required,
        )


async def check_dependencies(
    settings: Settings,
    database: Database,
    redis: RedisClient,
) -> list[ComponentHealth]:
    """Probe the required infrastructure dependencies concurrently."""
    return list(
        await asyncio.gather(
            _timed_probe("postgres", database.ping),
            _timed_probe("redis", redis.ping),
        )
    )


async def check_sibling_services(settings: Settings, redis: RedisClient) -> list[ComponentHealth]:
    """Report heartbeat freshness for the bot and worker processes.

    Siblings are reported as ``required=False``: the API remains ready and able
    to serve health traffic even when the worker is restarting.
    """
    results: list[ComponentHealth] = []
    for role in (ServiceRole.BOT, ServiceRole.WORKER):
        record = await read_heartbeat(redis, role)
        results.append(
            ComponentHealth(
                name=f"{role}-process",
                status=ComponentStatus.UP if record else ComponentStatus.UNKNOWN,
                detail=None if record else "no recent heartbeat",
                required=False,
            )
        )
    return results


async def build_health_report(
    settings: Settings,
    database: Database,
    redis: RedisClient,
    include_siblings: bool = True,
) -> HealthReport:
    """Assemble a full health report for this process."""
    components = await check_dependencies(settings, database, redis)
    if include_siblings and settings.service_role is ServiceRole.API:
        components.extend(await check_sibling_services(settings, redis))

    required_down = any(c.status is not ComponentStatus.UP for c in components if c.required)
    optional_down = any(c.status is not ComponentStatus.UP for c in components if not c.required)

    if required_down:
        overall = ComponentStatus.DOWN
    elif optional_down:
        overall = ComponentStatus.DEGRADED
    else:
        overall = ComponentStatus.UP

    return HealthReport(
        status=overall,
        service=settings.service_role,
        environment=str(settings.environment),
        components=components,
    )
