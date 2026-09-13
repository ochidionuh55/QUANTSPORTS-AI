"""Live basketball fixtures.

Separates two things that are easy to confuse: **whether we can see a game**
and **whether we can say anything about it**.

Seeing games needs a fixtures feed. The free basketball feeds cover the NBA and
little else, so the leagues playing in September — the WNBA playoffs, FIBA
tournaments, the Australian NBL — require a subscription. That is a payment
problem and a subscription solves it.

Saying something about a game needs that competition's own measured parameters.
No subscription provides those; they come from ingesting a league's history and
fitting it. A fixture can therefore be visible and still carry no probability,
and the product must show it that way rather than inventing one.

**Without a key, this returns nothing and says why.** It never falls back to a
different sport's provider or a different league's numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from app.core.basketball_competitions import competition, parameters_for
from app.core.logging import get_logger

logger = get_logger(__name__)

API_KEY_ENV = "BASKETBALL_API_KEY"
"""Environment variable holding the basketball subscription key.

Separate from the football key: the two are different products, and a
deployment may reasonably have one and not the other.
"""

API_ROOT = "https://v1.basketball.api-sports.io"
REQUEST_TIMEOUT = 15.0

# League identifiers used by the provider, mapped to our own keys so the rest
# of the system never learns a vendor's numbering.
LEAGUE_IDS: dict[int, str] = {
    12: "NBA",
    13: "WNBA",
    120: "NBL",
    194: "EUROLEAGUE",
    1: "FIBA_WC",
}


@dataclass(frozen=True)
class BasketballFixture:
    """One upcoming game, as much as we can honestly say about it."""

    provider_event_id: str
    competition_key: str
    competition_name: str
    home_name: str
    away_name: str
    tip_off: datetime
    status: str = "scheduled"

    @property
    def modellable(self) -> bool:
        """Whether this competition has measured parameters.

        A fixture we can see is not a fixture we can model. Keeping the two
        apart is what stops an unmeasured league inheriting the NBA's numbers.
        """
        return parameters_for(self.competition_key) is not None

    @property
    def coverage(self) -> str:
        """The grade this fixture can honestly carry."""
        return "fully_modelled" if self.modellable else "unsupported"

    @property
    def unavailable_reason(self) -> str:
        """Why no probability is shown, when none is."""
        if self.modellable:
            return ""
        entry = competition(self.competition_key)
        if entry is None:
            return "Competition not in our register."
        return f"{entry.name} has no measured parameters yet. " f"{entry.status_line}."


def from_environment(client: httpx.AsyncClient | None = None) -> BasketballProvider:
    """Build a provider from the environment.

    Returns a provider either way. An unconfigured one reports itself honestly
    rather than being absent, so the interface can explain what is missing
    instead of showing an empty screen.
    """
    return BasketballProvider(os.environ.get(API_KEY_ENV), client=client)


class BasketballProviderError(RuntimeError):
    """Raised when the basketball feed cannot be used."""


class BasketballProvider:
    """Reads upcoming basketball fixtures from a subscription feed."""

    def __init__(
        self,
        api_key: str | None,
        base_url: str = API_ROOT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = base_url.rstrip("/")
        self._client = client

    @property
    def configured(self) -> bool:
        """Whether a key is present."""
        return bool(self._api_key)

    async def fixtures(self, day: date | None = None) -> list[BasketballFixture]:
        """Return a day's fixtures across every league we recognise.

        Raises:
            BasketballProviderError: If no key is configured. Returning an
                empty list instead would be indistinguishable from a quiet day
                and would hide a fixable problem.
        """
        if not self.configured:
            raise BasketballProviderError(
                "No basketball feed configured. Set BASKETBALL_API_KEY to see "
                "fixtures for leagues outside the NBA."
            )

        target = day or datetime.now(UTC).date()
        payload = await self._get("/games", {"date": target.isoformat()})
        fixtures: list[BasketballFixture] = []

        for raw in payload:
            fixture = self._parse(raw)
            if fixture is not None:
                fixtures.append(fixture)

        fixtures.sort(key=lambda item: item.tip_off)
        logger.info(
            "basketball.fixtures",
            day=target.isoformat(),
            found=len(fixtures),
            modellable=sum(1 for f in fixtures if f.modellable),
        )
        return fixtures

    def _parse(self, raw: dict[str, Any]) -> BasketballFixture | None:
        """Turn one provider record into a fixture, or discard it.

        Leagues we do not recognise are dropped rather than guessed at: an
        unknown competition has no parameters, no history and no honest
        coverage grade.
        """
        league = raw.get("league") or {}
        league_id = league.get("id")
        key = LEAGUE_IDS.get(league_id if isinstance(league_id, int) else -1)
        if key is None:
            return None

        teams = raw.get("teams") or {}
        home = (teams.get("home") or {}).get("name")
        away = (teams.get("away") or {}).get("name")
        raw_date = raw.get("date")
        identifier = raw.get("id")

        if not (home and away and raw_date and identifier is not None):
            return None

        try:
            tip_off = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
        except ValueError:
            return None
        if tip_off.tzinfo is None:
            tip_off = tip_off.replace(tzinfo=UTC)

        entry = competition(key)
        status = ((raw.get("status") or {}).get("short") or "NS").lower()

        return BasketballFixture(
            provider_event_id=str(identifier),
            competition_key=key,
            competition_name=entry.name if entry else key,
            home_name=str(home),
            away_name=str(away),
            tip_off=tip_off,
            status="scheduled" if status in {"ns", "sched"} else status,
        )

    async def _get(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        """Call the feed and return its records."""
        client = self._client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        owned = self._client is None
        try:
            response = await client.get(
                f"{self._base_url}{path}",
                params=params,
                headers={"x-apisports-key": self._api_key},
            )
            if response.status_code in {401, 403}:
                raise BasketballProviderError(
                    "The basketball feed rejected the key. Check the " "subscription is active."
                )
            if response.status_code == 429:
                raise BasketballProviderError(
                    "The basketball feed's request limit has been reached for " "now."
                )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise BasketballProviderError(f"Could not reach the basketball feed: {exc}") from exc
        finally:
            if owned:
                await client.aclose()

        records = body.get("response") if isinstance(body, dict) else None
        return [r for r in records or [] if isinstance(r, dict)]


def summarise(fixtures: list[BasketballFixture]) -> dict[str, int]:
    """Count fixtures per competition, for display."""
    counts: dict[str, int] = {}
    for fixture in fixtures:
        counts[fixture.competition_name] = counts.get(fixture.competition_name, 0) + 1
    return counts
