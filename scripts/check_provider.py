#!/usr/bin/env python3
"""Verify a live provider key works, cheaply.

Spends two or three requests of the daily allowance and reports what came back,
so a key can be validated without running the whole application.

    docker compose exec api python scripts/check_provider.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.providers.api_football import ApiFootballProvider
from app.providers.errors import ProviderError

ENV_VAR = "API_FOOTBALL_KEY"


async def main() -> int:
    """Probe the provider and print a summary."""
    settings = get_settings()
    settings.observability.log_level = "WARNING"
    configure_logging(settings)

    key = os.getenv(ENV_VAR, "")
    if not key:
        print(f"{ENV_VAR} is not set. Add it to .env and restart.", file=sys.stderr)
        return 1

    provider = ApiFootballProvider(api_key=key)

    health = await provider.health_check()
    print(f"health: {'ok' if health.healthy else 'unhealthy'} - {health.detail}")

    try:
        events = await provider.get_events(hours_ahead=24)
    except ProviderError as exc:
        print(f"\nprovider error: {exc}", file=sys.stderr)
        print(f"retryable: {exc.is_retryable}", file=sys.stderr)
        return 1

    print(f"\n{len(events)} fixtures in supported leagues over the next 24h")
    for event in events[:10]:
        competition = event.competition.name if event.competition else "?"
        print(
            f"  {event.start_time:%Y-%m-%d %H:%M}  {competition:<18} "
            f"{event.home_team.name} v {event.away_team.name}"
        )
    if len(events) > 10:
        print(f"  ... and {len(events) - 10} more")

    print(
        f"\nrequests used: {provider.budget.used}, " f"remaining today: {provider.budget.remaining}"
    )
    if not events:
        print(
            "\nNo fixtures found. That is normal on a quiet day, or outside " "the European season."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
