"""Structured logging configuration.

Every log line carries the service role, environment and — where one exists —
a correlation ID, so that a single user action can be traced across the API,
bot and worker processes.

Production emits JSON. Development emits coloured console output.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

from app.core.config import Settings
from app.core.version import APP_VERSION, build_sha

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_NOISY_LOGGERS = (
    "aiogram.event",
    "apscheduler.executors.default",
    "asyncio",
    "httpx",
    "httpcore",
)


def new_correlation_id() -> str:
    """Generate a fresh correlation ID."""
    return uuid.uuid4().hex


def set_correlation_id(value: str | None) -> str:
    """Bind a correlation ID to the current context, generating one if absent.

    Args:
        value: An inbound correlation ID, or ``None`` to generate a new one.

    Returns:
        The correlation ID now bound to the context.
    """
    resolved = value or new_correlation_id()
    _correlation_id.set(resolved)
    return resolved


def get_correlation_id() -> str | None:
    """Return the correlation ID bound to the current context, if any."""
    return _correlation_id.get()


def clear_correlation_id() -> None:
    """Unbind the current correlation ID."""
    _correlation_id.set(None)


def _inject_correlation_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor that copies the context correlation ID onto the event."""
    correlation_id = _correlation_id.get()
    if correlation_id is not None:
        event_dict.setdefault("correlation_id", correlation_id)
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib logging bridge.

    Safe to call more than once; later calls replace the configuration.

    Args:
        settings: Loaded application settings.
    """
    level = getattr(logging, settings.observability.log_level)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _inject_correlation_id,
    ]

    renderer: structlog.typing.Processor
    if settings.observability.log_format == "json":
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, aiogram) through the same sink.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    structlog.contextvars.bind_contextvars(
        service=str(settings.service_role),
        environment=str(settings.environment),
        version=APP_VERSION,
        build=build_sha(),
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger.

    Args:
        name: Logger name, conventionally ``__name__``.
    """
    logger = structlog.get_logger()
    return logger.bind(logger=name) if name else logger  # type: ignore[no-any-return]
