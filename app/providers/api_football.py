"""API-Football live fixture and odds provider.

Adapts api-football.com into the internal provider contract. Nothing outside
this module knows the vendor exists.

**The free tier allows 100 requests per day**, which shapes the design more
than anything else. One request per fixture's odds would exhaust the quota on a
single busy Saturday, so:

* Fixtures for a whole date arrive in one call.
* Odds are fetched per fixture only when actually needed, and the caller is
  expected to cache.
* A local budget counter refuses to issue a request once the daily allowance is
  spent, rather than letting the vendor return errors that look like outages.

**Exhausting the quota is not an outage.** It raises
``ProviderRateLimitError``, which is retryable *after a wait*, so callers can
distinguish "come back tomorrow" from "the service is broken".
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx

from app.core.competitions import LEAGUE_IDS as _REGISTRY_LEAGUE_IDS
from app.core.logging import get_logger
from app.providers.base import OddsProvider
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderDataError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.models import (
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
from app.providers.registry import ProviderConfig

logger = get_logger(__name__)

DEFAULT_BASE_URL: Final[str] = "https://v3.football.api-sports.io"
FREE_TIER_DAILY_REQUESTS: Final[int] = 100
"""Requests a day on the free plan.

A starting assumption only. The real allowance is read from the response
headers on the first call, because a hardcoded number quietly lies about a
paid plan — it reported 100 remaining while the account held 7,500, which made
the quota look like the cause of a failure it had nothing to do with.
"""

DAILY_LIMIT_ENV: Final[str] = "API_FOOTBALL_DAILY_LIMIT"
MAX_DAYS_ENV: Final[str] = "API_FOOTBALL_MAX_DAYS"

FREE_TIER_MAX_DAYS_AHEAD: Final[int] = 1
"""How many days either side of today the free plan will serve.

The binding constraint on settling older fixtures. At one day, a match played
on Saturday could not be settled from Monday — the request was refused, and
the selection stayed pending permanently. Paid plans serve a far wider window,
so this is configurable rather than fixed.
"""
"""How far ahead the free plan permits fixture queries.

The plan allows roughly today plus one day. Requesting a date outside that
window returns an error rather than an empty list, so a 48-hour request would
otherwise fail on its third calendar day and take the whole scan with it.
Paid plans lift this; the value is configurable per provider.
"""

# API-Football status strings mapped onto internal lifecycle states. Anything
# unrecognised becomes UNKNOWN rather than being guessed at.
_STATUS_MAP: Final[dict[str, ProviderEventStatus]] = {
    "TBD": ProviderEventStatus.SCHEDULED,
    "NS": ProviderEventStatus.SCHEDULED,
    "1H": ProviderEventStatus.LIVE,
    "HT": ProviderEventStatus.LIVE,
    "2H": ProviderEventStatus.LIVE,
    "ET": ProviderEventStatus.LIVE,
    "BT": ProviderEventStatus.LIVE,
    "P": ProviderEventStatus.LIVE,
    "LIVE": ProviderEventStatus.LIVE,
    "FT": ProviderEventStatus.FINISHED,
    "AET": ProviderEventStatus.FINISHED,
    "PEN": ProviderEventStatus.FINISHED,
    "SUSP": ProviderEventStatus.SUSPENDED,
    "INT": ProviderEventStatus.SUSPENDED,
    "PST": ProviderEventStatus.POSTPONED,
    "CANC": ProviderEventStatus.CANCELLED,
    "ABD": ProviderEventStatus.CANCELLED,
    "AWD": ProviderEventStatus.FINISHED,
    "WO": ProviderEventStatus.FINISHED,
}

# Sourced from the shared competition registry, so a league cannot be live
# here but unknown to the historical parser.
LEAGUE_IDS: Final[dict[str, int]] = _REGISTRY_LEAGUE_IDS

_MATCH_WINNER_BET_ID: Final[int] = 1
"""The vendor's id for the 1X2 market."""


MIN_REQUEST_INTERVAL_SECONDS = 6.5
"""Spacing between requests.

The free plan allows ten requests a minute, and exceeding it returns an error
rather than a queue — so a burst does not merely wait, it loses the data. Six
and a half seconds keeps roughly nine requests a minute with margin for clock
drift, which costs a little time and saves every fixture's prices.
"""


class RequestPacer:
    """Spaces outgoing requests to respect a per-minute ceiling.

    Waiting is better than failing here. A refused request returns nothing and
    the fixture is analysed without prices; a delayed one returns the data.
    """

    def __init__(self, interval: float = MIN_REQUEST_INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._last: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        """Block until the next request may be sent."""
        if self._interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._last is not None:
                elapsed = now - self._last
                if elapsed < self._interval:
                    await asyncio.sleep(self._interval - elapsed)
                    now = loop.time()
            self._last = now


REMAINING_HEADER: Final[str] = "x-ratelimit-requests-remaining"
LIMIT_HEADER: Final[str] = "x-ratelimit-requests-limit"


class RequestBudget:
    """Tracks daily request usage against a quota.

    Counted locally rather than relying on response headers, because the last
    request before the limit still succeeds and the failure only appears on the
    next one — by which time a scan is already half done.
    """

    def __init__(self, daily_limit: int = FREE_TIER_DAILY_REQUESTS) -> None:
        self._limit = daily_limit
        self._day: date | None = None
        self._used = 0
        self._reported_remaining: int | None = None
        """What the vendor last said was left, which overrides our count."""

    @property
    def used(self) -> int:
        """Requests spent today."""
        return self._used

    @property
    def limit(self) -> int:
        """The allowance this key actually has."""
        return self._limit

    @property
    def remaining(self) -> int:
        """Requests left today."""
        if self._reported_remaining is not None:
            return self._reported_remaining
        return max(0, self._limit - self._used)

    def observe(self, headers: object) -> None:
        """Learn the real allowance from a response.

        The vendor reports both the plan limit and what is left on every call.
        Trusting a local counter instead meant reporting 100 remaining on an
        account holding 7,500 — which made the quota look like the cause of a
        failure it had nothing to do with, and cost an afternoon.
        """
        try:
            limit = headers.get(LIMIT_HEADER)  # type: ignore[attr-defined]
            remaining = headers.get(REMAINING_HEADER)  # type: ignore[attr-defined]
        except AttributeError:
            return

        if limit is not None:
            with suppress(TypeError, ValueError):
                self._limit = max(self._limit, int(limit))
        if remaining is not None:
            with suppress(TypeError, ValueError):
                self._reported_remaining = max(0, int(remaining))

    def spend(self, now: datetime | None = None) -> None:
        """Record one request.

        Raises:
            ProviderRateLimitError: If the daily allowance is exhausted.
        """
        today = (now or datetime.now(UTC)).date()
        if self._day != today:
            self._day = today
            self._used = 0
            self._reported_remaining = None

        if self._used >= self._limit:
            raise ProviderRateLimitError(
                f"Daily quota of {self._limit} requests is exhausted. This is a "
                "plan limit, not an outage; the allowance resets at midnight UTC.",
                retry_after_seconds=_seconds_until_midnight(now),
            )
        self._used += 1


def _seconds_until_midnight(now: datetime | None = None) -> float:
    """Return seconds until the quota resets."""
    moment = now or datetime.now(UTC)
    tomorrow = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - moment).total_seconds()


def _decimal(value: Any) -> Decimal | None:
    """Parse a price, returning ``None`` when unusable."""
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return price if price > 1 else None


class ApiFootballProvider(OddsProvider):
    """Live fixtures and odds from api-football.com."""

    def __init__(
        self,
        name: str = "api_football",
        api_key: str = "",
        role: ProviderRole = ProviderRole.TRADEABLE,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 15.0,
        daily_limit: int = FREE_TIER_DAILY_REQUESTS,
        max_days_ahead: int = FREE_TIER_MAX_DAYS_AHEAD,
        client: httpx.AsyncClient | None = None,
        request_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        """Create the provider.

        Args:
            name: Provider identifier written to ``provider_name`` columns.
            api_key: Vendor API key. Never logged.
            role: Reference or tradeable.
            base_url: API root.
            timeout_seconds: Per-request timeout.
            daily_limit: Plan request allowance.
            max_days_ahead: How many days beyond today the plan permits.
            client: Injected HTTP client, for tests.
            request_interval: Seconds between requests. Zero disables pacing,
                which suits tests and any plan without a per-minute ceiling.
        """
        self._name = name
        self._api_key = api_key
        self._role = role
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = client
        self._max_days_ahead = max_days_ahead
        self.budget = RequestBudget(daily_limit)
        self._pacer = RequestPacer(request_interval)

    @classmethod
    def from_config(cls, config: ProviderConfig) -> ApiFootballProvider:
        """Build from configuration, as used by the registry."""
        return cls(
            name=config.name,
            api_key=config.api_key.get_secret_value() if config.api_key else "",
            role=config.role,
            base_url=config.base_url or DEFAULT_BASE_URL,
            timeout_seconds=config.timeout_seconds,
            daily_limit=int(
                config.options.get(
                    "daily_limit",
                    os.environ.get(DAILY_LIMIT_ENV, FREE_TIER_DAILY_REQUESTS),
                )
            ),
            max_days_ahead=int(
                config.options.get(
                    "max_days_ahead",
                    os.environ.get(MAX_DAYS_ENV, FREE_TIER_MAX_DAYS_AHEAD),
                )
            ),
            request_interval=float(
                config.options.get("request_interval", MIN_REQUEST_INTERVAL_SECONDS)
            ),
        )

    @property
    def name(self) -> str:
        """Provider identifier."""
        return self._name

    @property
    def role(self) -> ProviderRole:
        """Reference or tradeable."""
        return self._role

    @property
    def capabilities(self) -> frozenset[ProviderCapability]:
        """Declared capabilities."""
        return frozenset(
            {
                ProviderCapability.FIXTURES,
                ProviderCapability.ODDS,
                ProviderCapability.EVENT_DETAIL,
                ProviderCapability.COMPETITION_METADATA,
            }
        )

    async def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Issue one authenticated request and return the response list.

        Every vendor failure mode is translated here so business logic never
        sees an ``httpx`` exception.

        Raises:
            ProviderAuthenticationError: On a missing or rejected key.
            ProviderRateLimitError: On quota exhaustion.
            ProviderTimeoutError: On timeout.
            ProviderUnavailableError: On network or server failure.
            ProviderDataError: On an unreadable payload.
        """
        if not self._api_key:
            raise ProviderAuthenticationError(
                "No API key configured. Set the provider's api_key from an "
                "environment variable.",
                provider_name=self._name,
                operation=path,
            )

        await self._pacer.wait()

        self.budget.spend()
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None

        try:
            response = await client.get(
                f"{self._base_url}/{path.lstrip('/')}",
                params=params,
                headers={"x-apisports-key": self._api_key},
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"Request timed out after {self._timeout}s.",
                provider_name=self._name,
                operation=path,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"Network failure: {exc}",
                provider_name=self._name,
                operation=path,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        # The vendor reports the real plan limit and what is left on every
        # response. Reading it means the budget reflects the account rather
        # than an assumption made at startup.
        self.budget.observe(response.headers)

        if response.status_code in (401, 403):
            raise ProviderAuthenticationError(
                "API key was rejected.", provider_name=self._name, operation=path
            )
        if response.status_code == 429:
            raise ProviderRateLimitError(
                "Vendor reported the rate limit was exceeded.",
                provider_name=self._name,
                operation=path,
                retry_after_seconds=_seconds_until_midnight(),
            )
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"Vendor returned {response.status_code}.",
                provider_name=self._name,
                operation=path,
            )
        if response.status_code != 200:
            raise ProviderDataError(
                f"Unexpected status {response.status_code}.",
                provider_name=self._name,
                operation=path,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderDataError(
                "Response was not valid JSON.",
                provider_name=self._name,
                operation=path,
            ) from exc

        # The vendor reports errors inside a 200 response, so a successful
        # status code alone does not mean the call worked.
        errors = payload.get("errors")
        if errors and not isinstance(errors, list):
            message = "; ".join(f"{k}: {v}" for k, v in dict(errors).items())
            if any(word in message.lower() for word in ("token", "key", "subscription")):
                raise ProviderAuthenticationError(message, provider_name=self._name, operation=path)
            raise ProviderDataError(message, provider_name=self._name, operation=path)

        result = payload.get("response")
        if not isinstance(result, list):
            raise ProviderDataError(
                "Response payload had no 'response' list.",
                provider_name=self._name,
                operation=path,
            )
        return result

    async def health_check(self) -> ProviderHealth:
        """Probe the vendor without consuming meaningful quota."""
        checked = datetime.now(UTC)
        if not self._api_key:
            return ProviderHealth(
                provider_name=self._name,
                provider_role=self._role,
                healthy=False,
                checked_at=checked,
                detail="No API key configured.",
            )
        if self.budget.remaining == 0:
            return ProviderHealth(
                provider_name=self._name,
                provider_role=self._role,
                healthy=False,
                checked_at=checked,
                detail="Daily quota exhausted; resets at midnight UTC.",
            )
        return ProviderHealth(
            provider_name=self._name,
            provider_role=self._role,
            healthy=True,
            checked_at=checked,
            detail=f"{self.budget.remaining} requests remaining today.",
        )

    async def get_competitions(self) -> tuple[ProviderCompetition, ...]:
        """Return the competitions this integration supports.

        Returned from the local mapping rather than the vendor: the league list
        is stable and fetching it would spend quota on information that does
        not change.
        """
        self.require(ProviderCapability.COMPETITION_METADATA)
        return tuple(
            ProviderCompetition(
                provider_name=self._name,
                external_id=str(league_id),
                name=code,
                raw={"division_code": code},
            )
            for code, league_id in sorted(LEAGUE_IDS.items())
        )

    def _parse_fixture(self, item: dict[str, Any]) -> ProviderEvent | None:
        """Convert one vendor fixture into an event, or ``None`` if unusable."""
        try:
            fixture = item["fixture"]
            teams = item["teams"]
            league = item.get("league", {})
            kickoff = datetime.fromisoformat(str(fixture["date"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            logger.warning("api_football.unparseable_fixture")
            return None

        status_code = str(fixture.get("status", {}).get("short", ""))
        return ProviderEvent(
            provider_name=self._name,
            external_id=str(fixture["id"]),
            home_team=ProviderTeam(
                provider_name=self._name,
                external_id=str(teams["home"]["id"]),
                name=str(teams["home"]["name"]),
            ),
            away_team=ProviderTeam(
                provider_name=self._name,
                external_id=str(teams["away"]["id"]),
                name=str(teams["away"]["name"]),
            ),
            start_time=kickoff,
            competition=ProviderCompetition(
                provider_name=self._name,
                external_id=str(league.get("id", "")),
                name=str(league.get("name", "")),
                country=league.get("country"),
            )
            if league.get("id")
            else None,
            status=_STATUS_MAP.get(status_code, ProviderEventStatus.UNKNOWN),
            source_timestamp=datetime.now(UTC),
        )

    async def get_events(
        self,
        hours_ahead: int = 48,
        competition_external_ids: tuple[str, ...] | None = None,
    ) -> tuple[ProviderEvent, ...]:
        """Return fixtures within the window.

        One request per calendar day covered. A 48-hour window therefore costs
        two or three requests, not one per fixture.
        """
        self.require(ProviderCapability.FIXTURES)

        async def call() -> tuple[ProviderEvent, ...]:
            today = datetime.now(UTC).date()
            # Clamped to what the plan permits. Asking for a date outside the
            # allowed window returns an error, not an empty list.
            requested = (hours_ahead // 24) + 1
            days = range(min(requested, self._max_days_ahead + 1))
            events: list[ProviderEvent] = []

            for offset in days:
                day = today + timedelta(days=offset)
                params: dict[str, Any] = {"date": day.isoformat()}
                try:
                    items = await self._get("fixtures", params)
                except (ProviderRateLimitError, ProviderAuthenticationError):
                    # A spent quota or a rejected key affects every day, so
                    # swallowing it would turn a broken deployment into a
                    # silently empty fixture card.
                    raise
                except ProviderError as exc:
                    # A single rejected date - usually a plan restriction on how
                    # far ahead we may look - must not lose the days that worked.
                    logger.warning("api_football.day_failed", day=day.isoformat(), error=str(exc))
                    continue
                for item in items:
                    event = self._parse_fixture(item)
                    if event is None:
                        continue
                    league_id = event.competition.external_id if event.competition else None
                    if competition_external_ids and league_id not in competition_external_ids:
                        continue
                    if league_id and int(league_id) not in LEAGUE_IDS.values():
                        # Outside the competitions we hold history for. Kept out
                        # of results rather than surfaced as unmodellable noise.
                        continue
                    events.append(event)
            return tuple(events)

        return await self.observe("get_events", call)

    async def get_event(self, external_id: str) -> ProviderEvent:
        """Return one fixture with its 1X2 market attached.

        Raises:
            ProviderDataError: If the fixture is unknown.
        """
        self.require(ProviderCapability.EVENT_DETAIL)

        async def call() -> ProviderEvent:
            items = await self._get("fixtures", {"id": external_id})
            if not items:
                raise ProviderDataError(
                    f"Unknown fixture '{external_id}'.",
                    provider_name=self._name,
                    operation="get_event",
                )
            event = self._parse_fixture(items[0])
            if event is None:
                raise ProviderDataError(
                    f"Fixture '{external_id}' could not be parsed.",
                    provider_name=self._name,
                    operation="get_event",
                )
            markets = await self._fetch_markets(external_id)
            return event.model_copy(update={"markets": markets})

        return await self.observe("get_event", call)

    async def _fetch_markets(self, fixture_id: str) -> tuple[ProviderMarket, ...]:
        """Fetch and average the 1X2 market across available bookmakers.

        Averaging rather than picking one book: a consensus is closer to a fair
        price than any single operator's opinion, and it is far more robust to
        one bookmaker being absent for a given fixture.
        """
        items = await self._get("odds", {"fixture": fixture_id, "bet": _MATCH_WINNER_BET_ID})
        if not items:
            return ()

        collected: dict[str, list[Decimal]] = {"Home": [], "Draw": [], "Away": []}
        for entry in items:
            for bookmaker in entry.get("bookmakers", []):
                for bet in bookmaker.get("bets", []):
                    for value in bet.get("values", []):
                        label = str(value.get("value", ""))
                        price = _decimal(value.get("odd"))
                        if label in collected and price is not None:
                            collected[label].append(price)

        if not all(collected.values()):
            return ()

        now = datetime.now(UTC)
        outcomes = tuple(
            ProviderOutcome(
                provider_name=self._name,
                external_id=f"{fixture_id}:1x2:{label}",
                name=label,
                odds=(sum(prices, Decimal(0)) / len(prices)).quantize(Decimal("0.01")),
                status=ProviderOutcomeStatus.ACTIVE,
                source_timestamp=now,
                raw={"bookmaker_count": len(prices)},
            )
            for label, prices in collected.items()
        )
        return (
            ProviderMarket(
                provider_name=self._name,
                external_id=f"{fixture_id}:1x2",
                name="1X2",
                outcomes=outcomes,
                source_timestamp=now,
            ),
        )

    async def get_event_odds(self, external_id: str) -> tuple[ProviderOddsSnapshot, ...]:
        """Return current prices for one fixture as timestamped snapshots."""
        self.require(ProviderCapability.ODDS)

        async def call() -> tuple[ProviderOddsSnapshot, ...]:
            markets = await self._fetch_markets(external_id)
            fetched_at = datetime.now(UTC)
            return tuple(
                ProviderOddsSnapshot(
                    provider_name=self._name,
                    provider_role=self._role,
                    event_external_id=external_id,
                    market_external_id=market.external_id,
                    outcome_external_id=outcome.external_id,
                    odds=outcome.odds,
                    fetched_at=fetched_at,
                    source_timestamp=outcome.source_timestamp,
                )
                for market in markets
                for outcome in market.outcomes
            )

        return await self.observe("get_event_odds", call)

    async def get_results_for_dates(self, days: list[object]) -> list[object]:
        """Return final scores for every finished fixture on the given dates.

        Fetching by date rather than by id, because the free plan forbids the
        ``ids`` parameter outright — a restriction that silently stranded every
        published selection in a permanently unsettled state.

        Cheaper as well as permitted: one request per day covers an entire
        card, where fetching by id costs a request per twenty fixtures. A busy
        Saturday settles in a single call.
        """
        from app.services.settlement import FinalScore

        if not days:
            return []

        self.require(ProviderCapability.FIXTURES)
        finished: list[object] = []

        # The free plan serves a narrow window around today and rejects
        # anything outside it. Requesting a date it will refuse spends an
        # allowance we cannot spare and returns nothing, so those dates are
        # skipped rather than attempted.
        today = datetime.now(UTC).date()
        earliest = today - timedelta(days=self._max_days_ahead)
        latest = today + timedelta(days=self._max_days_ahead)

        for day in days:
            parsed = day if isinstance(day, date) else None
            if parsed is not None and not (earliest <= parsed <= latest):
                logger.info(
                    "provider.date_outside_plan_window",
                    day=str(parsed),
                    earliest=str(earliest),
                    latest=str(latest),
                )
                continue

            items = await self._get("fixtures", {"date": str(day)})
            for item in items:
                fixture = item.get("fixture", {})
                status = str(fixture.get("status", {}).get("short", ""))
                if _STATUS_MAP.get(status) is not ProviderEventStatus.FINISHED:
                    continue
                goals = item.get("goals", {})
                home, away = goals.get("home"), goals.get("away")
                if home is None or away is None:
                    continue
                finished.append(
                    FinalScore(
                        provider_event_id=str(fixture.get("id")),
                        home_goals=int(home),
                        away_goals=int(away),
                    )
                )
        return finished

    async def get_results(self, fixture_ids: list[str]) -> list[object]:
        """Return final scores for finished fixtures.

        Fetched by id in one batched request rather than one per fixture: the
        vendor accepts a dash-separated id list, and settlement would otherwise
        cost a request per match and exhaust the daily allowance on a busy
        Saturday.

        Only genuinely finished matches are returned. A postponed or abandoned
        fixture has no result to settle against, and inventing one would put a
        false record into the permanent performance history.
        """
        from app.services.settlement import FinalScore

        if not fixture_ids:
            return []

        self.require(ProviderCapability.FIXTURES)
        finished: list[object] = []

        # The vendor caps the id list, so requests are chunked.
        for start in range(0, len(fixture_ids), 20):
            batch = fixture_ids[start : start + 20]
            items = await self._get("fixtures", {"ids": "-".join(batch)})
            for item in items:
                fixture = item.get("fixture", {})
                status = str(fixture.get("status", {}).get("short", ""))
                if _STATUS_MAP.get(status) is not ProviderEventStatus.FINISHED:
                    continue
                goals = item.get("goals", {})
                home, away = goals.get("home"), goals.get("away")
                if home is None or away is None:
                    continue
                finished.append(
                    FinalScore(
                        provider_event_id=str(fixture.get("id")),
                        home_goals=int(home),
                        away_goals=int(away),
                    )
                )
        return finished


async def probe(api_key: str, base_url: str = DEFAULT_BASE_URL) -> str:
    """Check a key works and report remaining quota.

    Used by ``scripts/check_provider.py`` so a key can be validated without
    running the whole application.
    """
    provider = ApiFootballProvider(api_key=api_key, base_url=base_url)
    events = await provider.get_events(hours_ahead=24)
    await asyncio.sleep(0)
    return (
        f"OK: {len(events)} fixtures in supported leagues over the next 24h; "
        f"{provider.budget.remaining} of {FREE_TIER_DAILY_REQUESTS} requests left."
    )
