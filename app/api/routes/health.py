"""Health and readiness endpoints.

``/health/live``  - process liveness, no dependencies touched.
``/health/ready`` - returns 503 when a required dependency is unreachable.
``/health``       - detailed report including sibling process heartbeats.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.dependencies.common import DatabaseDep, RedisDep, SettingsDep
from app.core.health import (
    ComponentStatus,
    HealthReport,
    build_health_report,
)
from app.core.version import APP_PHASE, APP_VERSION, build_sha

router = APIRouter(tags=["health"])


@router.get("/health/live", summary="Liveness probe")
async def liveness(settings: SettingsDep) -> dict[str, str]:
    """Return 200 whenever the process is running.

    Deliberately touches no dependency: a database outage must not cause the
    orchestrator to restart otherwise-healthy containers.
    """
    return {
        "status": ComponentStatus.UP,
        "service": str(settings.service_role),
        "version": APP_VERSION,
        "phase": APP_PHASE,
        "build": build_sha(),
    }


@router.get("/health/ready", summary="Readiness probe")
async def readiness(
    settings: SettingsDep,
    database: DatabaseDep,
    redis: RedisDep,
    response: Response,
) -> HealthReport:
    """Return 200 only when every required dependency is reachable."""
    report = await build_health_report(settings, database, redis, include_siblings=False)
    if not report.is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report


@router.get("/health", summary="Detailed health report")
async def health(
    settings: SettingsDep,
    database: DatabaseDep,
    redis: RedisDep,
    response: Response,
) -> HealthReport:
    """Return a detailed report, including bot and worker heartbeats."""
    report = await build_health_report(settings, database, redis, include_siblings=True)
    if not report.is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report
