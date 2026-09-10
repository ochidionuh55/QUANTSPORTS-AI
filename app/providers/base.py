"""Provider contracts.

Split by capability rather than expressed as one large interface. A sharp-odds
feed has no booking codes; a booking service has no fixtures. Forcing both into
one base class means every implementation carries methods that raise
``NotImplementedError``, and callers cannot tell which are real.

::

    OddsProvider          BookingCodeProvider
      get_events()          create_booking_code()
      get_event_odds()
      health_check()

A concrete adapter may implement either, both, or neither.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from app.core.logging import get_logger
from app.providers.errors import ProviderCapabilityError, ProviderError
from app.providers.models import (
    ProviderBookingCode,
    ProviderCapability,
    ProviderCompetition,
    ProviderEvent,
    ProviderHealth,
    ProviderOddsSnapshot,
    ProviderRole,
)

logger = get_logger(__name__)

T = TypeVar("T")


class BaseProvider(ABC):
    """Identity, role and capability declaration common to all providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable provider identifier, as stored in ``provider_name`` columns."""

    @property
    @abstractmethod
    def role(self) -> ProviderRole:
        """Whether this provider supplies reference or tradeable prices."""

    @property
    @abstractmethod
    def capabilities(self) -> frozenset[ProviderCapability]:
        """Everything this provider can do."""

    def supports(self, capability: ProviderCapability) -> bool:
        """Return whether this provider offers ``capability``."""
        return capability in self.capabilities

    def require(self, capability: ProviderCapability) -> None:
        """Assert a capability before using it.

        Raises:
            ProviderCapabilityError: If unsupported.
        """
        if not self.supports(capability):
            raise ProviderCapabilityError(
                f"Provider does not support '{capability}'. "
                f"Available: {sorted(self.capabilities)}.",
                provider_name=self.name,
                operation=str(capability),
            )

    async def observe(self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        """Run a provider call with structured timing and failure logging.

        Every adapter routes its outbound calls through this so that provider
        name, role, operation, duration and outcome are logged uniformly, and
        the correlation ID set by middleware is attached automatically. No
        credentials or headers are logged, only the operation name.

        Args:
            operation: Short operation label, e.g. ``get_events``.
            call: Zero-argument coroutine factory performing the work.
        """
        started = time.perf_counter()
        try:
            result = await call()
        except ProviderError as exc:
            logger.warning(
                "provider.call_failed",
                provider=self.name,
                role=str(self.role),
                operation=operation,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error_type=type(exc).__name__,
                retryable=exc.is_retryable,
            )
            raise
        except Exception as exc:
            # An untranslated exception is an adapter bug: the boundary is
            # supposed to convert everything into a ProviderError.
            logger.error(
                "provider.untranslated_error",
                provider=self.name,
                operation=operation,
                error_type=type(exc).__name__,
            )
            raise ProviderError(
                f"Untranslated error in {operation}: {exc}",
                provider_name=self.name,
                operation=operation,
            ) from exc

        logger.info(
            "provider.call_succeeded",
            provider=self.name,
            role=str(self.role),
            operation=operation,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return result

    async def health_check(self) -> ProviderHealth:
        """Probe the provider. Must never raise.

        A failing provider degrades the system; it does not crash it. The
        default implementation reports healthy, so adapters that cannot
        meaningfully probe do not have to pretend.
        """
        return ProviderHealth(
            provider_name=self.name,
            provider_role=self.role,
            healthy=True,
            checked_at=datetime.now(UTC),
        )


class OddsProvider(BaseProvider):
    """A source of fixtures and prices."""

    @abstractmethod
    async def get_competitions(self) -> tuple[ProviderCompetition, ...]:
        """Return the competitions this provider covers.

        Raises:
            ProviderCapabilityError: If COMPETITION_METADATA is unsupported.
            ProviderError: On any external failure.
        """

    @abstractmethod
    async def get_events(
        self,
        hours_ahead: int = 48,
        competition_external_ids: tuple[str, ...] | None = None,
    ) -> tuple[ProviderEvent, ...]:
        """Return upcoming fixtures within the window.

        Args:
            hours_ahead: How far ahead to look.
            competition_external_ids: Restrict to these competitions.

        Raises:
            ProviderError: On any external failure.
        """

    @abstractmethod
    async def get_event(self, external_id: str) -> ProviderEvent:
        """Return one fixture with its markets and outcomes.

        Raises:
            ProviderDataError: If the event does not exist.
            ProviderError: On any external failure.
        """

    @abstractmethod
    async def get_event_odds(self, external_id: str) -> tuple[ProviderOddsSnapshot, ...]:
        """Return current prices for one fixture as timestamped snapshots.

        Raises:
            ProviderError: On any external failure.
        """


class BookingCodeProvider(BaseProvider):
    """A service that converts selections into a bookmaker booking code."""

    @abstractmethod
    async def create_booking_code(
        self, selections: tuple[dict[str, Any], ...]
    ) -> ProviderBookingCode:
        """Create a booking code for the given selections.

        Raises:
            ProviderCapabilityError: If BOOKING_CODES is unsupported.
            ProviderError: On any external failure.
        """


__all__ = [
    "BaseProvider",
    "BookingCodeProvider",
    "OddsProvider",
]
