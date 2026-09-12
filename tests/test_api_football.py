"""API-Football adapter tests.

No network. Responses are mocked, so the tests exercise parsing, error
translation and quota behaviour rather than the vendor's uptime.

The error-translation tests matter most: every vendor failure mode must become
an internal ``ProviderError`` with the correct retryability, because that is
what stops a bad key from being retried into a lockout.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.providers.api_football import (
    FREE_TIER_DAILY_REQUESTS,
    LEAGUE_IDS,
    MIN_REQUEST_INTERVAL_SECONDS,
    ApiFootballProvider,
    RequestBudget,
    RequestPacer,
)
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderDataError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.models import ProviderCapability, ProviderEventStatus
from app.providers.registry import ProviderConfig, configs_from_env

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _fixture_payload(fixture_id: int = 101, league_id: int = 40) -> dict[str, Any]:
    """Build one vendor fixture record."""
    return {
        "fixture": {
            "id": fixture_id,
            "date": "2026-06-01T15:00:00+00:00",
            "status": {"short": "NS"},
        },
        "teams": {
            "home": {"id": 1, "name": "Leeds"},
            "away": {"id": 2, "name": "Norwich"},
        },
        "league": {"id": league_id, "name": "Championship", "country": "England"},
    }


def _odds_payload() -> dict[str, Any]:
    """Build one vendor odds record with two bookmakers."""
    return {
        "bookmakers": [
            {
                "name": "Book A",
                "bets": [
                    {
                        "name": "Match Winner",
                        "values": [
                            {"value": "Home", "odd": "2.00"},
                            {"value": "Draw", "odd": "3.40"},
                            {"value": "Away", "odd": "3.80"},
                        ],
                    }
                ],
            },
            {
                "name": "Book B",
                "bets": [
                    {
                        "name": "Match Winner",
                        "values": [
                            {"value": "Home", "odd": "2.10"},
                            {"value": "Draw", "odd": "3.50"},
                            {"value": "Away", "odd": "3.60"},
                        ],
                    }
                ],
            },
        ]
    }


def _provider(handler: Any, **kwargs: Any) -> ApiFootballProvider:
    """Build a provider backed by a mock transport."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("request_interval", 0.0)
    return ApiFootballProvider(api_key="test-key", client=client, **kwargs)


def _responder(
    payload: list[dict[str, Any]] | None = None,
    status: int = 200,
    errors: Any = None,
) -> Any:
    """Return a handler producing a fixed response."""

    def handler(_request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {"response": payload if payload is not None else []}
        if errors is not None:
            body["errors"] = errors
        return httpx.Response(status, json=body)

    return handler


class TestRequestBudget:
    """Quota is tracked locally, not inferred from failures."""

    def test_counts_usage(self) -> None:
        budget = RequestBudget(daily_limit=3)
        budget.spend(NOW)
        assert budget.used == 1
        assert budget.remaining == 2

    def test_refuses_once_exhausted(self) -> None:
        budget = RequestBudget(daily_limit=2)
        budget.spend(NOW)
        budget.spend(NOW)
        with pytest.raises(ProviderRateLimitError, match="plan limit, not an outage"):
            budget.spend(NOW)

    def test_exhaustion_is_retryable_after_a_wait(self) -> None:
        budget = RequestBudget(daily_limit=1)
        budget.spend(NOW)
        with pytest.raises(ProviderRateLimitError) as info:
            budget.spend(NOW)
        assert info.value.is_retryable
        assert info.value.retry_after_seconds
        assert info.value.retry_after_seconds > 0

    def test_resets_the_next_day(self) -> None:
        budget = RequestBudget(daily_limit=1)
        budget.spend(NOW)
        budget.spend(NOW + timedelta(days=1))
        assert budget.used == 1


class TestErrorTranslation:
    """Vendor failures become internal errors with correct retryability."""

    async def test_missing_key_is_rejected_before_spending_quota(self) -> None:
        provider = ApiFootballProvider(api_key="")
        with pytest.raises(ProviderAuthenticationError, match="No API key"):
            await provider.get_events()
        assert provider.budget.used == 0

    async def test_rejected_key_is_not_retryable(self) -> None:
        provider = _provider(_responder(status=401))
        with pytest.raises(ProviderAuthenticationError) as info:
            await provider.get_events()
        assert info.value.is_retryable is False

    async def test_rate_limit_status_is_translated(self) -> None:
        provider = _provider(_responder(status=429))
        with pytest.raises(ProviderRateLimitError):
            await provider.get_events()

    async def test_server_error_is_retryable(self) -> None:
        provider = _provider(_responder(status=503))
        with pytest.raises(ProviderUnavailableError) as info:
            await provider.get_event("101")
        assert info.value.is_retryable is True

    async def test_timeout_is_translated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("slow", request=request)

        with pytest.raises(ProviderTimeoutError):
            await _provider(handler).get_event("101")

    async def test_network_error_is_translated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(ProviderUnavailableError):
            await _provider(handler).get_event("101")

    async def test_error_inside_a_200_response_is_caught(self) -> None:
        """The vendor reports failures with a 200 status."""
        provider = _provider(_responder(errors={"requests": "limit reached"}))
        with pytest.raises(ProviderDataError, match="limit reached"):
            await provider.get_event("101")

    async def test_auth_error_inside_a_200_response(self) -> None:
        provider = _provider(_responder(errors={"token": "invalid api key"}))
        with pytest.raises(ProviderAuthenticationError):
            await provider.get_events()

    async def test_malformed_payload_is_data_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"nothing": "useful"})

        with pytest.raises(ProviderDataError, match="no 'response' list"):
            await _provider(handler).get_event("101")


class TestFixtureParsing:
    """Vendor fixtures become internal events."""

    async def test_parses_a_fixture(self) -> None:
        provider = _provider(_responder([_fixture_payload()]))
        events = await provider.get_events(hours_ahead=0)

        assert len(events) == 1
        event = events[0]
        assert event.external_id == "101"
        assert event.home_team.name == "Leeds"
        assert event.away_team.external_id == "2"
        assert event.status is ProviderEventStatus.SCHEDULED
        assert event.start_time.tzinfo is UTC

    async def test_unsupported_league_is_excluded(self) -> None:
        """Fixtures outside our historical coverage are not surfaced."""
        provider = _provider(_responder([_fixture_payload(league_id=9999)]))
        assert await provider.get_events(hours_ahead=0) == ()

    async def test_unparseable_fixture_is_skipped_not_fatal(self) -> None:
        """One bad record must not lose the whole day's fixtures."""
        provider = _provider(_responder([{"fixture": {}}, _fixture_payload()]))
        assert len(await provider.get_events(hours_ahead=0)) == 1

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("NS", ProviderEventStatus.SCHEDULED),
            ("1H", ProviderEventStatus.LIVE),
            ("FT", ProviderEventStatus.FINISHED),
            ("PST", ProviderEventStatus.POSTPONED),
            ("SUSP", ProviderEventStatus.SUSPENDED),
            ("WEIRD", ProviderEventStatus.UNKNOWN),
        ],
    )
    async def test_status_mapping(self, code: str, expected: ProviderEventStatus) -> None:
        payload = _fixture_payload()
        payload["fixture"]["status"]["short"] = code
        provider = _provider(_responder([payload]))
        events = await provider.get_events(hours_ahead=0)
        assert events[0].status is expected

    async def test_unknown_fixture_raises(self) -> None:
        provider = _provider(_responder([]))
        with pytest.raises(ProviderDataError, match="Unknown fixture"):
            await provider.get_event("999")

    async def test_window_costs_one_request_per_day(self) -> None:
        """Quota discipline: not one request per fixture."""
        provider = _provider(_responder([_fixture_payload()]), max_days_ahead=2)
        await provider.get_events(hours_ahead=48)
        assert provider.budget.used == 3

    async def test_window_is_clamped_to_the_plan(self) -> None:
        """The free plan rejects dates beyond tomorrow.

        Regression: a 48-hour request spanned three calendar days, the third
        was refused, and the error aborted the entire scan.
        """
        provider = _provider(_responder([_fixture_payload()]))
        await provider.get_events(hours_ahead=48)
        assert provider.budget.used == 2

    async def test_one_rejected_day_does_not_lose_the_others(self) -> None:
        """A plan restriction on one date must not empty the whole card."""
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 2:
                return httpx.Response(
                    200,
                    json={
                        "response": [],
                        "errors": {"plan": "Free plans do not have access"},
                    },
                )
            return httpx.Response(200, json={"response": [_fixture_payload()]})

        provider = _provider(handler, max_days_ahead=2)
        events = await provider.get_events(hours_ahead=48)
        assert len(events) == 2


class TestOddsParsing:
    """Prices are averaged across bookmakers."""

    async def test_averages_across_bookmakers(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "odds" in request.url.path:
                return httpx.Response(200, json={"response": [_odds_payload()]})
            return httpx.Response(200, json={"response": [_fixture_payload()]})

        provider = _provider(handler)
        event = await provider.get_event("101")

        assert len(event.markets) == 1
        prices = {o.name: o.odds for o in event.markets[0].outcomes}
        assert prices["Home"] == Decimal("2.05")
        assert prices["Draw"] == Decimal("3.45")
        assert prices["Away"] == Decimal("3.70")

    async def test_records_how_many_books_contributed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "odds" in request.url.path:
                return httpx.Response(200, json={"response": [_odds_payload()]})
            return httpx.Response(200, json={"response": [_fixture_payload()]})

        event = await _provider(handler).get_event("101")
        assert event.markets[0].outcomes[0].raw["bookmaker_count"] == 2

    async def test_missing_odds_yields_no_market(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"response": [] if "odds" in request.url.path else [_fixture_payload()]},
            )

        event = await _provider(handler).get_event("101")
        assert event.markets == ()

    async def test_incomplete_market_is_rejected(self) -> None:
        """A market missing an outcome cannot have its margin removed."""
        partial = {
            "bookmakers": [
                {
                    "bets": [
                        {
                            "values": [
                                {"value": "Home", "odd": "2.00"},
                                {"value": "Draw", "odd": "3.40"},
                            ]
                        }
                    ]
                }
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if "odds" in request.url.path:
                return httpx.Response(200, json={"response": [partial]})
            return httpx.Response(200, json={"response": [_fixture_payload()]})

        event = await _provider(handler).get_event("101")
        assert event.markets == ()

    async def test_impossible_price_is_dropped(self) -> None:
        bad = {
            "bookmakers": [
                {
                    "bets": [
                        {
                            "values": [
                                {"value": "Home", "odd": "1.00"},
                                {"value": "Draw", "odd": "3.40"},
                                {"value": "Away", "odd": "3.80"},
                            ]
                        }
                    ]
                }
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if "odds" in request.url.path:
                return httpx.Response(200, json={"response": [bad]})
            return httpx.Response(200, json={"response": [_fixture_payload()]})

        event = await _provider(handler).get_event("101")
        assert event.markets == ()

    async def test_snapshots_carry_role_and_timestamps(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "odds" in request.url.path:
                return httpx.Response(200, json={"response": [_odds_payload()]})
            return httpx.Response(200, json={"response": [_fixture_payload()]})

        snapshots = await _provider(handler).get_event_odds("101")
        assert len(snapshots) == 3
        for snapshot in snapshots:
            assert snapshot.fetched_at.tzinfo is UTC
            assert snapshot.source_timestamp is not None


class TestHealthAndConfig:
    """Health reports without raising; configuration stays declarative."""

    async def test_missing_key_is_unhealthy_not_fatal(self) -> None:
        health = await ApiFootballProvider(api_key="").health_check()
        assert health.healthy is False
        assert "No API key" in (health.detail or "")

    async def test_exhausted_quota_is_unhealthy(self) -> None:
        provider = ApiFootballProvider(api_key="k", daily_limit=1, request_interval=0.0)
        provider.budget.spend(NOW)
        health = await provider.health_check()
        assert health.healthy is False
        assert "quota" in (health.detail or "").lower()

    async def test_healthy_reports_remaining_quota(self) -> None:
        health = await ApiFootballProvider(api_key="k", request_interval=0.0).health_check()
        assert health.healthy is True
        assert str(FREE_TIER_DAILY_REQUESTS) in (health.detail or "")

    def test_capabilities_exclude_booking(self) -> None:
        provider = ApiFootballProvider(api_key="k")
        assert provider.supports(ProviderCapability.ODDS)
        assert not provider.supports(ProviderCapability.BOOKING_CODES)

    def test_built_from_config_without_leaking_the_key(self) -> None:
        config = ProviderConfig(name="live", adapter="api_football", api_key="super-secret")
        provider = ApiFootballProvider.from_config(config)
        assert provider.name == "live"
        assert "super-secret" not in repr(config)

    def test_env_config_falls_back_to_mocks(self) -> None:
        """An unconfigured deployment must not fail at startup."""
        assert {c.adapter for c in configs_from_env(None)} == {"mock"}

    def test_env_config_uses_the_live_provider_when_keyed(self) -> None:
        configs = configs_from_env("a-key")
        assert [c.adapter for c in configs] == ["api_football"]

    def test_supported_leagues_come_from_the_registry(self) -> None:
        """One registry, so a league cannot be live here but unknown to the
        historical parser."""
        from app.core.competitions import CSV_COMPETITIONS
        from app.core.competitions import LEAGUE_IDS as REGISTRY

        assert LEAGUE_IDS == REGISTRY
        assert set(LEAGUE_IDS) <= set(CSV_COMPETITIONS)
        assert len(LEAGUE_IDS) > 30


class TestRequestPacing:
    """Spacing requests under the plan's per-minute ceiling.

    The free plan refuses a burst rather than queuing it, so an unpaced scan
    does not merely run slowly — it silently loses every fixture's prices and
    the card fills with market-less analyses.
    """

    async def test_first_request_is_not_delayed(self) -> None:
        """Nothing is owed before anything has been sent."""
        pacer = RequestPacer(interval=10.0)
        started = asyncio.get_running_loop().time()
        await pacer.wait()

        assert asyncio.get_running_loop().time() - started < 0.5

    async def test_second_request_waits(self) -> None:
        pacer = RequestPacer(interval=0.2)
        await pacer.wait()

        started = asyncio.get_running_loop().time()
        await pacer.wait()
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed >= 0.15

    async def test_zero_interval_disables_pacing(self) -> None:
        """Paid plans and tests should not pay for a ceiling they do not have."""
        pacer = RequestPacer(interval=0.0)
        started = asyncio.get_running_loop().time()
        for _ in range(5):
            await pacer.wait()

        assert asyncio.get_running_loop().time() - started < 0.5

    async def test_concurrent_callers_are_serialised(self) -> None:
        """Ten tasks starting at once must not become ten simultaneous calls."""
        pacer = RequestPacer(interval=0.05)
        started = asyncio.get_running_loop().time()
        await asyncio.gather(*(pacer.wait() for _ in range(4)))
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed >= 0.1

    async def test_default_interval_stays_under_the_free_ceiling(self) -> None:
        """Ten a minute is the limit, so the gap must exceed six seconds."""
        assert MIN_REQUEST_INTERVAL_SECONDS > 60 / 10

    async def test_provider_paces_by_default(self) -> None:
        """A provider built without argument must not burst."""
        provider = ApiFootballProvider(api_key="k")
        assert provider._pacer._interval == MIN_REQUEST_INTERVAL_SECONDS
