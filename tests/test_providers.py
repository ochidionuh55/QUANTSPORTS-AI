"""Provider abstraction tests.

Two groups matter most. The determinism tests guard the property that makes the
mock usable for backtesting at all — identical input, identical output, every
run. The architecture tests fail the build if provider-name branching creeps
back into application code, which is the failure mode this whole layer exists
to prevent.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.providers import (
    DEFAULT_PROVIDER_CONFIGS,
    BookingCodeProvider,
    OddsProvider,
    ProviderCapability,
    ProviderCapabilityError,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderDataError,
    ProviderError,
    ProviderRegistry,
    ProviderRole,
    ProviderUnavailableError,
    build_default_registry,
)
from app.providers.mock import (
    CORRUPT_EVENT_ID,
    DUPLICATE_EXTERNAL_ID,
    NO_ODDS_EVENT_ID,
    REFERENCE_NOW,
    SUSPENDED_EVENT_ID,
    MockOddsProvider,
    make_failing_provider,
    make_limited_provider,
)
from app.providers.models import ProviderOddsSnapshot, ProviderOutcome


@pytest.fixture
def provider() -> MockOddsProvider:
    """A tradeable mock provider on the fixed clock."""
    return MockOddsProvider(name="mock_tradeable")


@pytest.fixture
def registry() -> ProviderRegistry:
    """A registry built from the default configuration."""
    return build_default_registry()


class TestContract:
    """Adapters must satisfy the declared contract."""

    def test_mock_is_an_odds_provider(self, provider: MockOddsProvider) -> None:
        assert isinstance(provider, OddsProvider)

    def test_mock_is_not_a_booking_provider(self, provider: MockOddsProvider) -> None:
        """Capability segregation: an odds feed does not book bets."""
        assert not isinstance(provider, BookingCodeProvider)

    def test_declares_identity_and_role(self, provider: MockOddsProvider) -> None:
        assert provider.name == "mock_tradeable"
        assert provider.role is ProviderRole.TRADEABLE

    def test_abstract_base_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            OddsProvider()  # type: ignore[abstract]


class TestDeterminism:
    """The same input must always produce the same output."""

    async def test_events_are_identical_across_instances(self) -> None:
        first = await MockOddsProvider().get_events()
        second = await MockOddsProvider().get_events()
        assert first == second

    async def test_odds_are_identical_across_calls(self, provider: MockOddsProvider) -> None:
        first = await provider.get_event_odds("evt-1001")
        second = await provider.get_event_odds("evt-1001")
        assert [s.odds for s in first] == [s.odds for s in second]

    async def test_odds_do_not_depend_on_wall_clock(self) -> None:
        """Prices must come from the seed, not the current time."""
        early = MockOddsProvider(now=lambda: REFERENCE_NOW)
        later = MockOddsProvider(now=lambda: REFERENCE_NOW + timedelta(days=400))

        early_odds = [s.odds for s in await early.get_event_odds("evt-1001")]
        later_odds = [s.odds for s in await later.get_event_odds("evt-1001")]
        assert early_odds == later_odds

    async def test_reference_provider_prices_differ_from_tradeable(self) -> None:
        """The prior must be able to disagree with the tradeable price.

        If both roles quoted identically, expected value would be zero by
        construction and the model could never demonstrate an edge.
        """
        tradeable = MockOddsProvider(role=ProviderRole.TRADEABLE)
        reference = MockOddsProvider(role=ProviderRole.REFERENCE)
        t = [s.odds for s in await tradeable.get_event_odds("evt-1001")]
        r = [s.odds for s in await reference.get_event_odds("evt-1001")]
        assert t != r


class TestEdgeCases:
    """The awkward payloads that break naive ingestion."""

    async def test_event_with_no_odds(self, provider: MockOddsProvider) -> None:
        event = await provider.get_event(NO_ODDS_EVENT_ID)
        assert event.markets == ()

    async def test_suspended_event_marks_outcomes_suspended(
        self, provider: MockOddsProvider
    ) -> None:
        event = await provider.get_event(SUSPENDED_EVENT_ID)
        statuses = {o.status for m in event.markets for o in m.outcomes}
        assert statuses == {"suspended"}

    async def test_empty_competition_returns_no_events(self, provider: MockOddsProvider) -> None:
        events = await provider.get_events(competition_external_ids=("comp-empty",))
        assert events == ()

    async def test_duplicate_external_ids_are_surfaced(self, provider: MockOddsProvider) -> None:
        """The adapter passes duplicates through; ingestion must dedupe."""
        events = await provider.get_events()
        ids = [e.external_id for e in events]
        assert ids.count(DUPLICATE_EXTERNAL_ID) == 2

    async def test_corrupt_payload_raises_data_error(self, provider: MockOddsProvider) -> None:
        with pytest.raises(ProviderDataError) as info:
            await provider.get_event(CORRUPT_EVENT_ID)
        assert info.value.is_retryable is False

    async def test_unknown_event_raises_data_error(self, provider: MockOddsProvider) -> None:
        with pytest.raises(ProviderDataError):
            await provider.get_event("evt-does-not-exist")

    async def test_outage_raises_retryable_error(self) -> None:
        down = make_failing_provider()
        with pytest.raises(ProviderUnavailableError) as info:
            await down.get_events()
        assert info.value.is_retryable is True

    async def test_stale_event_has_start_time_in_the_past(self, provider: MockOddsProvider) -> None:
        event = await provider.get_event("evt-1006")
        assert event.start_time < REFERENCE_NOW


class TestValidation:
    """Impossible values are rejected at the DTO boundary."""

    def test_odds_at_or_below_stake_are_rejected(self) -> None:
        for bad in (Decimal("1.0"), Decimal("0.5")):
            with pytest.raises(ValueError, match="must exceed 1.0"):
                ProviderOutcome(provider_name="x", external_id="y", name="Home", odds=bad)

    def test_naive_timestamps_are_rejected(self) -> None:
        """A naive datetime must never be assumed to be UTC."""
        with pytest.raises(ValueError, match="Naive datetime"):
            ProviderOddsSnapshot(
                provider_name="x",
                provider_role=ProviderRole.TRADEABLE,
                event_external_id="e",
                market_external_id="m",
                outcome_external_id="o",
                odds=Decimal("2.0"),
                fetched_at=datetime(2026, 1, 1, 12, 0),
            )

    def test_timestamps_are_converted_to_utc(self) -> None:
        from datetime import timezone

        lagos = timezone(timedelta(hours=1))
        snapshot = ProviderOddsSnapshot(
            provider_name="x",
            provider_role=ProviderRole.TRADEABLE,
            event_external_id="e",
            market_external_id="m",
            outcome_external_id="o",
            odds=Decimal("2.0"),
            fetched_at=datetime(2026, 1, 1, 13, 0, tzinfo=lagos),
        )
        assert snapshot.fetched_at.tzinfo is UTC
        assert snapshot.fetched_at.hour == 12

    def test_fetched_at_and_source_timestamp_are_distinct(self) -> None:
        """Conflating them destroys point-in-time reconstruction."""
        snapshot = ProviderOddsSnapshot(
            provider_name="x",
            provider_role=ProviderRole.REFERENCE,
            event_external_id="e",
            market_external_id="m",
            outcome_external_id="o",
            odds=Decimal("2.0"),
            fetched_at=REFERENCE_NOW,
            source_timestamp=REFERENCE_NOW - timedelta(seconds=45),
        )
        assert snapshot.latency_seconds == 45.0

    def test_dtos_are_immutable(self, provider: MockOddsProvider) -> None:
        outcome = ProviderOutcome(
            provider_name="x", external_id="y", name="Home", odds=Decimal("2.0")
        )
        with pytest.raises(ValidationError):
            outcome.odds = Decimal("3.0")  # type: ignore[misc]


class TestCapabilities:
    """Capability checks replace provider-name conditionals."""

    def test_supports_declared_capability(self, provider: MockOddsProvider) -> None:
        assert provider.supports(ProviderCapability.ODDS)

    def test_does_not_support_booking(self, provider: MockOddsProvider) -> None:
        assert not provider.supports(ProviderCapability.BOOKING_CODES)

    def test_require_raises_for_unsupported(self, provider: MockOddsProvider) -> None:
        with pytest.raises(ProviderCapabilityError) as info:
            provider.require(ProviderCapability.BOOKING_CODES)
        assert info.value.is_retryable is False

    async def test_limited_provider_refuses_odds(self) -> None:
        limited = make_limited_provider()
        with pytest.raises(ProviderCapabilityError):
            await limited.get_event_odds("evt-1001")

    async def test_limited_provider_still_serves_fixtures(self) -> None:
        limited = make_limited_provider()
        assert await limited.get_events()


class TestErrorModel:
    """Retryability is a property of the failure, not a caller guess."""

    @pytest.mark.parametrize(
        ("error", "retryable"),
        [
            (ProviderUnavailableError("x"), True),
            (ProviderDataError("x"), False),
            (ProviderCapabilityError("x"), False),
        ],
    )
    def test_retryability(self, error: ProviderError, retryable: bool) -> None:
        assert error.is_retryable is retryable

    def test_message_carries_context(self) -> None:
        error = ProviderDataError("bad payload", provider_name="sporty", operation="get_event")
        assert "sporty" in str(error)
        assert "get_event" in str(error)

    async def test_untranslated_errors_are_wrapped(self) -> None:
        """An adapter leaking a raw exception is itself a bug, but must not
        leak a non-provider exception into business logic."""

        class Leaky(MockOddsProvider):
            async def get_competitions(self):  # type: ignore[no-untyped-def]
                async def call():  # type: ignore[no-untyped-def]
                    raise KeyError("some internal detail")

                return await self.observe("get_competitions", call)

        with pytest.raises(ProviderError):
            await Leaky().get_competitions()


class TestRegistry:
    """Resolution is configuration-driven and instance-scoped."""

    def test_builds_configured_providers(self, registry: ProviderRegistry) -> None:
        assert {p.name for p in registry.all()} == {
            "mock_tradeable",
            "mock_reference",
        }

    def test_lookup_by_name(self, registry: ProviderRegistry) -> None:
        assert registry.get("mock_tradeable").name == "mock_tradeable"

    def test_unknown_provider_raises(self, registry: ProviderRegistry) -> None:
        with pytest.raises(ProviderConfigurationError, match="No provider named"):
            registry.get("sporty")

    def test_duplicate_adapter_registration_raises(self) -> None:
        reg = ProviderRegistry()
        reg.register_adapter("mock", MockOddsProvider.from_config)
        with pytest.raises(ProviderConfigurationError, match="already registered"):
            reg.register_adapter("mock", MockOddsProvider.from_config)

    def test_duplicate_provider_name_raises(self) -> None:
        reg = ProviderRegistry()
        reg.register_adapter("mock", MockOddsProvider.from_config)
        configs = (
            ProviderConfig(name="dup", adapter="mock"),
            ProviderConfig(name="dup", adapter="mock"),
        )
        with pytest.raises(ProviderConfigurationError, match="Duplicate provider"):
            reg.build(configs)

    def test_unknown_adapter_raises(self) -> None:
        reg = ProviderRegistry()
        with pytest.raises(ProviderConfigurationError, match="Unknown adapter"):
            reg.build((ProviderConfig(name="x", adapter="nope"),))

    def test_disabled_provider_is_not_built(self) -> None:
        reg = ProviderRegistry()
        reg.register_adapter("mock", MockOddsProvider.from_config)
        reg.build((ProviderConfig(name="off", adapter="mock", enabled=False),))
        assert reg.all() == ()

    def test_selection_by_role(self, registry: ProviderRegistry) -> None:
        assert [p.name for p in registry.by_role(ProviderRole.REFERENCE)] == ["mock_reference"]

    def test_selection_by_capability(self, registry: ProviderRegistry) -> None:
        found = registry.with_capability(ProviderCapability.BOOKING_CODES)
        assert found == ()

    def test_registry_is_not_global(self) -> None:
        """Two registries must not share state."""
        first = build_default_registry()
        second = ProviderRegistry()
        assert first.all()
        assert second.all() == ()

    def test_credentials_are_not_exposed_in_repr(self) -> None:
        config = ProviderConfig(name="x", adapter="mock", api_key="super-secret")
        assert "super-secret" not in repr(config)

    def test_default_config_supplies_both_roles(self) -> None:
        roles = {c.role for c in DEFAULT_PROVIDER_CONFIGS}
        assert roles == {ProviderRole.REFERENCE, ProviderRole.TRADEABLE}


class TestHealth:
    """Provider outages degrade, they do not crash."""

    async def test_healthy_provider(self, provider: MockOddsProvider) -> None:
        health = await provider.health_check()
        assert health.healthy is True
        assert health.provider_name == "mock_tradeable"

    async def test_failing_provider_reports_unhealthy_without_raising(self) -> None:
        health = await make_failing_provider().health_check()
        assert health.healthy is False
        assert health.detail

    async def test_registry_health_covers_every_provider(self, registry: ProviderRegistry) -> None:
        reports = await registry.health()
        assert {r.provider_name for r in reports} == {
            "mock_tradeable",
            "mock_reference",
        }


class TestConsumerIsProviderAgnostic:
    """A future scanning service must work without knowing the implementation."""

    async def test_consumer_works_through_the_contract(self, registry: ProviderRegistry) -> None:
        async def collect_prices(source: OddsProvider) -> list[Decimal]:
            """Stand-in for a future scanning service."""
            prices: list[Decimal] = []
            for event in await source.get_events(hours_ahead=48):
                if not event.markets:
                    continue
                prices.extend(s.odds for s in await source.get_event_odds(event.external_id))
            return prices

        for source in registry.odds_providers():
            assert await collect_prices(source)

    async def test_consumer_selects_by_role_not_name(self, registry: ProviderRegistry) -> None:
        tradeable = registry.odds_providers(role=ProviderRole.TRADEABLE)
        reference = registry.odds_providers(role=ProviderRole.REFERENCE)
        assert tradeable and reference
        assert tradeable[0].name != reference[0].name


class TestArchitecture:
    """Fail the build if provider coupling reappears."""

    def test_no_provider_name_conditionals_in_application_code(self) -> None:
        """Branching on a provider name recouples the app to one bookmaker."""
        offenders: list[str] = []
        pattern = re.compile(r"""(provider(_name)?|\.name)\s*==\s*["'](?!mock)""", re.IGNORECASE)
        root = Path(__file__).resolve().parents[1] / "app"
        for path in root.rglob("*.py"):
            if path.parts[-2:] == ("providers", "registry.py"):
                continue
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(root)}:{number}")
        assert offenders == [], (
            "Provider-name branching found. Use capabilities or roles: " f"{offenders}"
        )

    def test_orm_models_are_not_imported_by_providers(self) -> None:
        """Adapters must return DTOs, never SQLAlchemy models."""
        offenders: list[str] = []
        root = Path(__file__).resolve().parents[1] / "app" / "providers"
        for path in root.rglob("*.py"):
            text = path.read_text()
            if "app.database.models" in text:
                offenders.append(path.name)
        assert offenders == [], f"Provider layer must not depend on ORM models: {offenders}"

    def test_business_logic_does_not_import_http_libraries(self) -> None:
        """HTTP exceptions must be translated at the adapter boundary."""
        offenders: list[str] = []
        root = Path(__file__).resolve().parents[1] / "app"
        for area in ("services", "scanners", "quant"):
            area_path = root / area
            if not area_path.exists():
                continue
            for path in area_path.rglob("*.py"):
                text = path.read_text()
                if "import httpx" in text or "import requests" in text:
                    offenders.append(str(path.relative_to(root)))
        assert offenders == [], f"HTTP client leaked into business logic: {offenders}"
