"""Shared live provider access.

One provider instance per process, not one per request. The registry was
previously rebuilt on every callback, which reset the request-budget counter
each time and so defeated quota tracking entirely — the guard existed but never
saw more than one request.

Returns ``None`` when no key is configured, so an unkeyed deployment degrades
to "live fixtures unavailable" rather than failing.
"""

from __future__ import annotations

import os
from functools import lru_cache

from app.core.logging import get_logger
from app.providers.base import OddsProvider
from app.providers.registry import build_default_registry, configs_from_env

logger = get_logger(__name__)

API_KEY_ENV = "API_FOOTBALL_KEY"


@lru_cache(maxsize=1)
def _registry_for(key: str) -> object:
    """Build the registry once per key."""
    return build_default_registry(configs_from_env(key))


def live_odds_provider() -> OddsProvider | None:
    """Return the process-wide live odds provider, or ``None`` if unconfigured."""
    key = os.getenv(API_KEY_ENV, "")
    if not key:
        return None
    registry = _registry_for(key)
    providers = registry.odds_providers()  # type: ignore[attr-defined]
    if not providers:
        logger.warning("live.no_odds_provider_configured")
        return None
    return providers[0]  # type: ignore[no-any-return]


def reset() -> None:
    """Clear the cached registry. For tests and configuration changes."""
    _registry_for.cache_clear()
