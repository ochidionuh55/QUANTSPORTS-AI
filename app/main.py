"""FastAPI application factory and entrypoint for the API service.

Run with::

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.middleware import CorrelationIdMiddleware, RequestLoggingMiddleware
from app.api.routes import health, public, system
from app.core.config import ServiceRole, Settings, get_settings
from app.core.logging import configure_logging, get_correlation_id, get_logger
from app.core.version import APP_PHASE, APP_VERSION
from app.infrastructure.database import Database
from app.infrastructure.migrations import ensure_schema
from app.infrastructure.redis import RedisClient

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop infrastructure alongside the application.

    Connection *setup* is done eagerly so failures surface at boot, but the
    process does not refuse to start when Postgres or Redis is temporarily
    unavailable: readiness reports that instead, letting the orchestrator hold
    traffic without a crash loop.
    """
    settings: Settings = app.state.settings
    database = Database(settings)
    redis = RedisClient(settings)

    await database.connect()
    await redis.connect()

    # The schema is the application's own responsibility. Leaving it to a
    # deployment step means a missed migration fails silently: the service
    # starts, reports healthy, and every screen touching a new column breaks
    # with nothing shown to the user.
    await ensure_schema(settings, database, redis, required=True)
    app.state.database = database
    app.state.redis = redis

    db_ok = await database.ping()
    redis_ok = await redis.ping()
    logger.info(
        "api.startup",
        version=APP_VERSION,
        phase=APP_PHASE,
        environment=str(settings.environment),
        postgres_reachable=db_ok,
        redis_reachable=redis_ok,
        value_detection_enabled=settings.features.value_detection_enabled,
    )

    try:
        yield
    finally:
        await redis.disconnect()
        await database.disconnect()
        logger.info("api.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Args:
        settings: Optional settings override, used by tests.
    """
    resolved = settings or get_settings()
    resolved.service_role = ServiceRole.API
    configure_logging(resolved)
    resolved.validate_runtime()

    app = FastAPI(
        title=resolved.app_name,
        version=APP_VERSION,
        summary="Quantitative sports market analysis platform",
        description=(
            "Internal API. Probability outputs are statistical estimates, not "
            "predictions of outcomes, and carry no guarantee of any return."
        ),
        root_path=resolved.api_root_path,
        docs_url=None if resolved.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if resolved.is_production else "/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = resolved

    # Middleware runs bottom-up: correlation ID is bound before logging reads it.
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    app.include_router(health.router)
    app.include_router(system.router)
    app.include_router(public.router)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Return a generic error body; never leak stack traces to clients."""
        logger.exception(
            "api.unhandled_exception",
            path=request.url.path,
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error.",
                "correlation_id": get_correlation_id(),
            },
        )

    return app


app = create_app()
