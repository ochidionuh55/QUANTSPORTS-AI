"""Graceful shutdown helper for the non-HTTP services.

The bot and worker are long-running asyncio processes. Docker sends SIGTERM on
``docker compose stop``; without a handler the container is killed after the
grace period, which can orphan Redis reservations and in-flight jobs. This
helper turns those signals into a clean shutdown event.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable

from app.core.logging import get_logger

logger = get_logger(__name__)

_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def install_shutdown_handlers(event: asyncio.Event, service: str) -> None:
    """Set ``event`` when the process receives SIGINT or SIGTERM.

    Args:
        event: Event to set on signal receipt.
        service: Service name, for logging.
    """
    loop = asyncio.get_running_loop()

    def _handle(sig: signal.Signals) -> None:
        logger.info("service.shutdown_signal", service=service, signal=sig.name)
        event.set()

    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, _handle, sig)
        except NotImplementedError:  # pragma: no cover - Windows dev machines
            signal.signal(sig, lambda *_: event.set())


async def run_until_shutdown(
    service: str,
    startup: Callable[[], Awaitable[None]],
    shutdown: Callable[[], Awaitable[None]],
) -> None:
    """Run ``startup``, wait for a shutdown signal, then run ``shutdown``.

    ``shutdown`` always runs, including when ``startup`` raises, so partially
    initialised resources are still released.

    Args:
        service: Service name, for logging.
        startup: Coroutine factory that starts the service.
        shutdown: Coroutine factory that stops the service.
    """
    stop_event = asyncio.Event()
    install_shutdown_handlers(stop_event, service)
    try:
        await startup()
        logger.info("service.running", service=service)
        await stop_event.wait()
    finally:
        logger.info("service.stopping", service=service)
        await shutdown()
        logger.info("service.stopped", service=service)
