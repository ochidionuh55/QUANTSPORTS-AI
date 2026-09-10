"""HTTP middleware.

Attaches a correlation ID to every request and logs one structured line per
request. Health probes are logged at debug level so they do not drown the logs.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import clear_correlation_id, get_logger, set_correlation_id

logger = get_logger(__name__)

CORRELATION_HEADER = "X-Correlation-ID"
_QUIET_PATHS = frozenset({"/health", "/health/live", "/health/ready", "/metrics"})


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind an inbound or freshly generated correlation ID to the request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = set_correlation_id(request.headers.get(CORRELATION_HEADER))
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        finally:
            clear_correlation_id()
        response.headers[CORRELATION_HEADER] = correlation_id
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Emit one structured log line per request, with duration and status."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        quiet = request.url.path in _QUIET_PATHS
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "http.request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
                error_type=type(exc).__name__,
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        log = logger.debug if quiet else logger.info
        log(
            "http.request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response
