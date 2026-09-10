"""Provider configuration, registry and factory.

Adapters are registered by an ``adapter`` key and instantiated from
configuration, so adding a provider is a configuration change plus one new
module — never an edit to a call site.

The registry is an instance, not a module-level global. A global would make
tests order-dependent and would prevent an API process and a worker process in
the same interpreter from holding different provider sets.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.core.logging import get_logger
from app.providers.base import BaseProvider, BookingCodeProvider, OddsProvider
from app.providers.errors import ProviderConfigurationError
from app.providers.models import ProviderCapability, ProviderHealth, ProviderRole

logger = get_logger(__name__)

ProviderFactory = Callable[["ProviderConfig"], BaseProvider]


class ProviderConfig(BaseModel):
    """Configuration for one provider instance.

    Credentials are ``SecretStr`` so they are redacted from reprs and logs.
    Nothing in this model is ever logged directly; adapters log ``name`` and
    ``role`` only.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    """Stable identifier, written into ``provider_name`` columns."""

    adapter: str = Field(min_length=1)
    """Which registered adapter class builds this provider."""

    role: ProviderRole = ProviderRole.TRADEABLE
    enabled: bool = True

    base_url: str | None = None
    api_key: SecretStr | None = None

    timeout_seconds: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)
    backoff_seconds: float = Field(default=0.5, gt=0)
    rate_limit_per_minute: int | None = Field(default=None, ge=1)

    priority: int = Field(default=100)
    """Lower runs first when several providers can serve the same request."""

    options: dict[str, Any] = Field(default_factory=dict)
    """Adapter-specific settings that do not belong in the shared model."""


DEFAULT_PROVIDER_CONFIGS: tuple[ProviderConfig, ...] = (
    ProviderConfig(
        name="mock_tradeable",
        adapter="mock",
        role=ProviderRole.TRADEABLE,
        priority=10,
    ),
    ProviderConfig(
        name="mock_reference",
        adapter="mock",
        role=ProviderRole.REFERENCE,
        priority=5,
        options={"overround": "1.025"},
    ),
)
"""Default set: one tradeable and one reference mock.

Two rather than one, deliberately. The market-as-prior design needs a
reference price distinct from the tradeable price, and a default that supplies
only one provider would let later phases be built against a shape that cannot
express the thing they exist to measure.
"""


class ProviderRegistry:
    """Holds adapter factories and the provider instances built from config."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}
        self._instances: dict[str, BaseProvider] = {}

    def register_adapter(self, adapter: str, factory: ProviderFactory) -> None:
        """Register an adapter factory under a key.

        Raises:
            ProviderConfigurationError: If the key is already registered.
        """
        if adapter in self._factories:
            raise ProviderConfigurationError(
                f"Adapter '{adapter}' is already registered. Duplicate "
                "registration usually means a module was imported twice under "
                "different names."
            )
        self._factories[adapter] = factory
        logger.debug("provider.adapter_registered", adapter=adapter)

    def build(self, configs: Iterable[ProviderConfig]) -> None:
        """Instantiate every enabled provider from configuration.

        Raises:
            ProviderConfigurationError: On unknown adapters or duplicate names.
        """
        for config in configs:
            if not config.enabled:
                logger.info("provider.disabled", provider=config.name)
                continue
            if config.adapter not in self._factories:
                raise ProviderConfigurationError(
                    f"Unknown adapter '{config.adapter}' for provider "
                    f"'{config.name}'. Registered: {sorted(self._factories)}."
                )
            if config.name in self._instances:
                raise ProviderConfigurationError(
                    f"Duplicate provider name '{config.name}'. Names are "
                    "written to provider_name columns and must be unique."
                )
            self._instances[config.name] = self._factories[config.adapter](config)
            logger.info(
                "provider.built",
                provider=config.name,
                adapter=config.adapter,
                role=str(config.role),
            )

    def get(self, name: str) -> BaseProvider:
        """Return a provider by name.

        Raises:
            ProviderConfigurationError: If no such provider is configured.
        """
        try:
            return self._instances[name]
        except KeyError:
            raise ProviderConfigurationError(
                f"No provider named '{name}'. Configured: " f"{sorted(self._instances)}."
            ) from None

    def all(self) -> tuple[BaseProvider, ...]:
        """Return every configured provider, lowest priority value first."""
        return tuple(self._instances.values())

    def with_capability(self, capability: ProviderCapability) -> tuple[BaseProvider, ...]:
        """Return providers offering a capability.

        This is the supported way to select a provider. Branching on provider
        names couples the application to a specific bookmaker, which is exactly
        what this layer exists to prevent.
        """
        return tuple(p for p in self._instances.values() if p.supports(capability))

    def by_role(self, role: ProviderRole) -> tuple[BaseProvider, ...]:
        """Return providers serving a given role."""
        return tuple(p for p in self._instances.values() if p.role is role)

    def odds_providers(self, role: ProviderRole | None = None) -> tuple[OddsProvider, ...]:
        """Return odds-capable providers, optionally filtered by role."""
        found = [p for p in self._instances.values() if isinstance(p, OddsProvider)]
        if role is not None:
            found = [p for p in found if p.role is role]
        return tuple(found)

    def booking_providers(self) -> tuple[BookingCodeProvider, ...]:
        """Return booking-code-capable providers."""
        return tuple(p for p in self._instances.values() if isinstance(p, BookingCodeProvider))

    async def health(self) -> tuple[ProviderHealth, ...]:
        """Probe every provider concurrently.

        Failures are reported, never raised: a provider outage degrades the
        system rather than failing the process health check.
        """
        providers = self.all()
        if not providers:
            return ()

        results = await asyncio.gather(
            *(p.health_check() for p in providers), return_exceptions=True
        )
        reports: list[ProviderHealth] = []
        for provider, result in zip(providers, results, strict=True):
            if isinstance(result, ProviderHealth):
                reports.append(result)
            else:
                from datetime import UTC, datetime

                reports.append(
                    ProviderHealth(
                        provider_name=provider.name,
                        provider_role=provider.role,
                        healthy=False,
                        checked_at=datetime.now(UTC),
                        detail=f"{type(result).__name__}: {result}",
                    )
                )
        return tuple(reports)


def configs_from_env(
    api_football_key: str | None = None,
) -> tuple[ProviderConfig, ...]:
    """Build provider configuration from the environment.

    The live provider is included only when a key is present, so an
    unconfigured deployment falls back to mocks rather than failing at startup
    with an authentication error.

    Args:
        api_football_key: API-Football key, read from the environment by the
            caller so this module never touches ``os.environ`` directly.
    """
    if not api_football_key:
        return DEFAULT_PROVIDER_CONFIGS
    return (
        ProviderConfig(
            name="api_football",
            adapter="api_football",
            role=ProviderRole.TRADEABLE,
            api_key=SecretStr(api_football_key),
            priority=1,
            timeout_seconds=15.0,
            options={"daily_limit": 100},
        ),
    )


def build_default_registry(
    configs: Iterable[ProviderConfig] | None = None,
) -> ProviderRegistry:
    """Build a registry with the built-in adapters registered.

    Args:
        configs: Provider configuration. Defaults to the mock pair.
    """
    from app.providers.api_football import ApiFootballProvider
    from app.providers.mock import MockOddsProvider

    registry = ProviderRegistry()
    registry.register_adapter("mock", MockOddsProvider.from_config)
    registry.register_adapter("api_football", ApiFootballProvider.from_config)
    registry.build(configs if configs is not None else DEFAULT_PROVIDER_CONFIGS)
    return registry
