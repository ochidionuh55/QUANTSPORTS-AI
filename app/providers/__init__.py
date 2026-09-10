"""Market data provider abstraction.

The application depends on the contracts and DTOs exported here, never on a
concrete adapter. Selecting a provider is done by capability or role, never by
name, so that adding or replacing a bookmaker is a configuration change rather
than an edit to business logic.
"""

from app.providers.base import BaseProvider, BookingCodeProvider, OddsProvider
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderCapabilityError,
    ProviderConfigurationError,
    ProviderDataError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.models import (
    ProviderBookingCode,
    ProviderCapability,
    ProviderCompetition,
    ProviderEvent,
    ProviderEventStatus,
    ProviderHealth,
    ProviderMarket,
    ProviderOddsSnapshot,
    ProviderOutcome,
    ProviderOutcomeStatus,
    ProviderRole,
    ProviderTeam,
)
from app.providers.registry import (
    DEFAULT_PROVIDER_CONFIGS,
    ProviderConfig,
    ProviderRegistry,
    build_default_registry,
)

__all__ = [
    "DEFAULT_PROVIDER_CONFIGS",
    "BaseProvider",
    "BookingCodeProvider",
    "OddsProvider",
    "ProviderAuthenticationError",
    "ProviderBookingCode",
    "ProviderCapability",
    "ProviderCapabilityError",
    "ProviderCompetition",
    "ProviderConfig",
    "ProviderConfigurationError",
    "ProviderDataError",
    "ProviderError",
    "ProviderEvent",
    "ProviderEventStatus",
    "ProviderHealth",
    "ProviderMarket",
    "ProviderOddsSnapshot",
    "ProviderOutcome",
    "ProviderOutcomeStatus",
    "ProviderRateLimitError",
    "ProviderRegistry",
    "ProviderRole",
    "ProviderTeam",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "build_default_registry",
]
