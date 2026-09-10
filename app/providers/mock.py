"""Deterministic mock odds provider.

Every value is derived from a fixed seed dataset or from a hash of stable
identifiers. Nothing uses ``random`` without a seed, and nothing depends on the
wall clock except through an injectable ``now`` function. A test that passes
today must pass identically next month, or it is measuring the calendar rather
than the code.

Fixture start times are expressed as offsets from an injected reference time,
so the data stays "upcoming" without ever being non-deterministic: pass a fixed
``now`` and the output is byte-identical.

The dataset deliberately includes the awkward cases that break naive ingestion:
an empty competition, a suspended event, an event quoting no odds, an event
with corrupt odds, a stale event, and duplicate external IDs.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from app.providers.base import OddsProvider
from app.providers.errors import (
    ProviderDataError,
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
from app.providers.pricing import (
    REFERENCE_OVERROUND,
    TRADEABLE_OVERROUND,
    OutcomePricing,
    price_market,
)
from app.providers.registry import ProviderConfig

REFERENCE_NOW: Final[datetime] = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
"""Default clock. Fixed so output is identical on every run."""

_COMPETITIONS: Final[tuple[tuple[str, str, str], ...]] = (
    ("comp-epl", "Premier League", "England"),
    ("comp-laliga", "La Liga", "Spain"),
    ("comp-empty", "Regionalliga Nord", "Germany"),
)

# (event id, competition, home, away, hours ahead, status)
_FIXTURES: Final[tuple[tuple[str, str, str, str, int, ProviderEventStatus], ...]] = (
    ("evt-1001", "comp-epl", "Arsenal", "Chelsea", 6, ProviderEventStatus.SCHEDULED),
    (
        "evt-1002",
        "comp-epl",
        "Manchester United",
        "Liverpool",
        26,
        ProviderEventStatus.SCHEDULED,
    ),
    (
        "evt-1003",
        "comp-laliga",
        "Real Madrid",
        "Sevilla",
        30,
        ProviderEventStatus.SCHEDULED,
    ),
    (
        "evt-1004",
        "comp-laliga",
        "Barcelona",
        "Valencia",
        44,
        ProviderEventStatus.SUSPENDED,
    ),
    ("evt-1005", "comp-epl", "Everton", "Brentford", 70, ProviderEventStatus.SCHEDULED),
    (
        "evt-1006",
        "comp-epl",
        "Fulham",
        "Brighton and Hove Albion",
        -3,
        ProviderEventStatus.LIVE,
    ),
    ("evt-1007", "comp-laliga", "Girona", "Osasuna", 20, ProviderEventStatus.SCHEDULED),
    (
        "evt-1008",
        "comp-epl",
        "Manchester City",
        "Luton Town",
        34,
        ProviderEventStatus.SCHEDULED,
    ),
)

NO_ODDS_EVENT_ID: Final[str] = "evt-1005"
"""Quotes no markets. Ingestion must handle this without dividing by zero."""

SUSPENDED_EVENT_ID: Final[str] = "evt-1004"
STALE_EVENT_ID: Final[str] = "evt-1006"
"""Kicked off already; its prices are stale by definition."""

CORRUPT_EVENT_ID: Final[str] = "evt-corrupt"
"""Returns odds of 1.0, which cannot pay out. Must raise ProviderDataError."""

DUPLICATE_EXTERNAL_ID: Final[str] = "evt-1002"
"""Appears twice in the raw feed to exercise deduplication."""


# Fair probabilities per fixture and market, with the tilt the soft book
# applies to each outcome. Reference prices are always untilted, so removing
# its margin proportionally recovers the fair probabilities exactly — which is
# what makes it usable as an honest prior.
#
# Tilt below 1.0 lengthens a price and can create genuine value; above 1.0
# shortens it. Scenarios are chosen so a value scanner must actually
# discriminate rather than accept everything.
_FairMarket = tuple[OutcomePricing, ...]

_D = Decimal


def _market(*rows: tuple[str, str, str]) -> _FairMarket:
    """Build a market from (label, fair probability, tilt) triples."""
    return tuple(
        OutcomePricing(label=label, fair_probability=_D(prob), tilt=_D(tilt))
        for label, prob, tilt in rows
    )


# evt-1001 - NO VALUE. Margin spread proportionally; every outcome is -EV.
# evt-1002 - GENUINE VALUE on Away: the soft book has lengthened it past fair.
# evt-1003 - APPARENT VALUE: Home is longer than the reference quotes it, but
#            still short of fair once the reference margin is removed.
# evt-1007 - MARGINAL: Draw is positive but under a 3% threshold.
# evt-1008 - UNDERDOG SHAPE: heavy favourite shortened, underdog lengthened
#            into genuine value.
_FAIR: Final[dict[tuple[str, str], _FairMarket]] = {
    ("evt-1001", "1x2"): _market(
        ("Home", "0.45", "1.0"), ("Draw", "0.27", "1.0"), ("Away", "0.28", "1.0")
    ),
    ("evt-1001", "ou25"): _market(("Over", "0.54", "1.0"), ("Under", "0.46", "1.0")),
    ("evt-1002", "1x2"): _market(
        ("Home", "0.40", "1.09"), ("Draw", "0.26", "1.06"), ("Away", "0.34", "0.83")
    ),
    ("evt-1002", "ou25"): _market(("Over", "0.58", "1.04"), ("Under", "0.42", "0.97")),
    ("evt-1003", "1x2"): _market(
        ("Home", "0.52", "0.93"), ("Draw", "0.24", "1.05"), ("Away", "0.24", "1.05")
    ),
    ("evt-1003", "ou25"): _market(("Over", "0.61", "1.02"), ("Under", "0.39", "1.0")),
    ("evt-1004", "1x2"): _market(
        ("Home", "0.55", "1.0"), ("Draw", "0.23", "1.0"), ("Away", "0.22", "1.0")
    ),
    ("evt-1004", "ou25"): _market(("Over", "0.57", "1.0"), ("Under", "0.43", "1.0")),
    ("evt-1006", "1x2"): _market(
        ("Home", "0.33", "1.02"), ("Draw", "0.28", "1.01"), ("Away", "0.39", "1.0")
    ),
    ("evt-1006", "ou25"): _market(("Over", "0.50", "1.01"), ("Under", "0.50", "1.01")),
    ("evt-1007", "1x2"): _market(
        ("Home", "0.38", "1.04"), ("Draw", "0.28", "0.925"), ("Away", "0.34", "1.03")
    ),
    ("evt-1007", "ou25"): _market(("Over", "0.52", "1.02"), ("Under", "0.48", "1.01")),
    ("evt-1008", "1x2"): _market(
        ("Home", "0.74", "1.10"), ("Draw", "0.16", "1.02"), ("Away", "0.10", "0.95")
    ),
    ("evt-1008", "ou25"): _market(("Over", "0.66", "1.03"), ("Under", "0.34", "1.0")),
}

NO_VALUE_EVENT_ID: Final[str] = "evt-1001"
GENUINE_VALUE_EVENT_ID: Final[str] = "evt-1002"
APPARENT_VALUE_EVENT_ID: Final[str] = "evt-1003"
MARGINAL_VALUE_EVENT_ID: Final[str] = "evt-1007"
UNDERDOG_VALUE_EVENT_ID: Final[str] = "evt-1008"


def fair_probabilities(event_id: str, market: str) -> dict[str, Decimal]:
    """Return the underlying true probabilities for a mock market.

    Test-only introspection: it lets a test assert that a scanner found the
    opportunities that genuinely exist, rather than merely that it found some.
    """
    return {o.label: o.fair_probability for o in _FAIR[(event_id, market)]}


class MockOddsProvider(OddsProvider):
    """In-memory odds provider producing deterministic data."""

    def __init__(
        self,
        name: str = "mock",
        role: ProviderRole = ProviderRole.TRADEABLE,
        now: Callable[[], datetime] | None = None,
        overround: Decimal | None = None,
        fail: bool = False,
        capabilities: frozenset[ProviderCapability] | None = None,
    ) -> None:
        """Create a mock provider.

        Args:
            name: Provider identifier.
            role: Reference or tradeable.
            now: Clock. Defaults to a fixed instant for determinism.
            overround: Total implied probability this book quotes to.
                Defaults by role: a sharp 1.025 for reference, a soft 1.075
                for tradeable.
            fail: Simulate a total provider outage.
            capabilities: Override the declared capability set, to exercise
                unsupported-capability handling.
        """
        self._name = name
        self._role = role
        self._now = now or (lambda: REFERENCE_NOW)
        self._overround = overround or (
            REFERENCE_OVERROUND if role is ProviderRole.REFERENCE else TRADEABLE_OVERROUND
        )
        self._fail = fail
        self._capabilities = capabilities or frozenset(
            {
                ProviderCapability.FIXTURES,
                ProviderCapability.ODDS,
                ProviderCapability.EVENT_DETAIL,
                ProviderCapability.COMPETITION_METADATA,
            }
        )

    @classmethod
    def from_config(cls, config: ProviderConfig) -> MockOddsProvider:
        """Build from configuration, as used by the registry."""
        raw = config.options.get("overround")
        return cls(
            name=config.name,
            role=config.role,
            overround=Decimal(str(raw)) if raw is not None else None,
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
        return self._capabilities

    def _guard(self, operation: str) -> None:
        """Simulate an outage when configured to fail."""
        if self._fail:
            raise ProviderUnavailableError(
                "Simulated provider outage.",
                provider_name=self._name,
                operation=operation,
            )

    async def health_check(self) -> ProviderHealth:
        """Report health without raising, even when simulating an outage."""
        return ProviderHealth(
            provider_name=self._name,
            provider_role=self._role,
            healthy=not self._fail,
            checked_at=self._now(),
            latency_ms=0.0,
            detail="Simulated outage." if self._fail else None,
        )

    async def get_competitions(self) -> tuple[ProviderCompetition, ...]:
        """Return the mock competitions."""
        self.require(ProviderCapability.COMPETITION_METADATA)

        async def call() -> tuple[ProviderCompetition, ...]:
            self._guard("get_competitions")
            return tuple(
                ProviderCompetition(
                    provider_name=self._name,
                    external_id=external_id,
                    name=name,
                    country=country,
                )
                for external_id, name, country in _COMPETITIONS
            )

        return await self.observe("get_competitions", call)

    def _competition(self, external_id: str) -> ProviderCompetition | None:
        """Return one competition DTO by ID."""
        for comp_id, name, country in _COMPETITIONS:
            if comp_id == external_id:
                return ProviderCompetition(
                    provider_name=self._name,
                    external_id=comp_id,
                    name=name,
                    country=country,
                )
        return None

    def _build_event(
        self,
        external_id: str,
        competition_id: str,
        home: str,
        away: str,
        hours: int,
        status: ProviderEventStatus,
        with_markets: bool,
    ) -> ProviderEvent:
        """Assemble one deterministic event."""
        now = self._now()
        markets: tuple[ProviderMarket, ...] = ()
        if with_markets and external_id != NO_ODDS_EVENT_ID:
            markets = self._build_markets(external_id, status, now)

        return ProviderEvent(
            provider_name=self._name,
            external_id=external_id,
            home_team=ProviderTeam(provider_name=self._name, name=home),
            away_team=ProviderTeam(provider_name=self._name, name=away),
            start_time=now + timedelta(hours=hours),
            competition=self._competition(competition_id),
            status=status,
            markets=markets,
            source_timestamp=now - timedelta(seconds=30),
        )

    def _build_markets(
        self, event_id: str, status: ProviderEventStatus, now: datetime
    ) -> tuple[ProviderMarket, ...]:
        """Build 1X2 and Over/Under 2.5 markets for an event."""
        outcome_status = (
            ProviderOutcomeStatus.SUSPENDED
            if status is ProviderEventStatus.SUSPENDED
            else ProviderOutcomeStatus.ACTIVE
        )
        source = now - timedelta(seconds=30)

        def build(market_key: str, name: str, specifier: str | None) -> ProviderMarket:
            """Price one market from its fair probabilities."""
            pricing = _FAIR[(event_id, market_key)]
            # The reference book is priced without tilt, so removing its margin
            # proportionally returns the fair probabilities exactly. The soft
            # book applies the per-outcome tilt, which is what creates — and
            # withholds — value.
            quoted = price_market(
                pricing,
                overround=self._overround,
                apply_tilt=self._role is not ProviderRole.REFERENCE,
            )
            return ProviderMarket(
                provider_name=self._name,
                external_id=f"{event_id}:{market_key}",
                name=name,
                specifier=specifier,
                outcomes=tuple(
                    ProviderOutcome(
                        provider_name=self._name,
                        external_id=f"{event_id}:{market_key}:{o.label}",
                        name=o.label,
                        odds=quoted[o.label],
                        status=outcome_status,
                        source_timestamp=source,
                    )
                    for o in pricing
                ),
                source_timestamp=source,
            )

        return (
            build("1x2", "1X2", None),
            build("ou25", "Over/Under", "total=2.5"),
        )

    async def get_events(
        self,
        hours_ahead: int = 48,
        competition_external_ids: tuple[str, ...] | None = None,
    ) -> tuple[ProviderEvent, ...]:
        """Return fixtures within the window.

        The raw feed intentionally contains a duplicate external ID; it is
        returned as-is so that ingestion, not the adapter, is exercised on
        deduplication.
        """
        self.require(ProviderCapability.FIXTURES)

        async def call() -> tuple[ProviderEvent, ...]:
            self._guard("get_events")
            events: list[ProviderEvent] = []
            for external_id, comp, home, away, hours, status in _FIXTURES:
                if hours > hours_ahead:
                    continue
                if competition_external_ids and comp not in competition_external_ids:
                    continue
                events.append(
                    self._build_event(
                        external_id, comp, home, away, hours, status, with_markets=True
                    )
                )
                if external_id == DUPLICATE_EXTERNAL_ID:
                    events.append(events[-1])
            return tuple(events)

        return await self.observe("get_events", call)

    async def get_event(self, external_id: str) -> ProviderEvent:
        """Return one fixture.

        Raises:
            ProviderDataError: For an unknown ID, or the corrupt fixture.
        """
        self.require(ProviderCapability.EVENT_DETAIL)

        async def call() -> ProviderEvent:
            self._guard("get_event")
            if external_id == CORRUPT_EVENT_ID:
                raise ProviderDataError(
                    "Outcome quoted at decimal odds of 1.0, which cannot pay "
                    "out; treating the payload as corrupt.",
                    provider_name=self._name,
                    operation="get_event",
                )
            for event_id, comp, home, away, hours, status in _FIXTURES:
                if event_id == external_id:
                    return self._build_event(
                        event_id, comp, home, away, hours, status, with_markets=True
                    )
            raise ProviderDataError(
                f"Unknown event '{external_id}'.",
                provider_name=self._name,
                operation="get_event",
            )

        return await self.observe("get_event", call)

    async def get_event_odds(self, external_id: str) -> tuple[ProviderOddsSnapshot, ...]:
        """Return current prices as timestamped snapshots."""
        self.require(ProviderCapability.ODDS)
        event = await self.get_event(external_id)
        fetched_at = self._now()

        async def call() -> tuple[ProviderOddsSnapshot, ...]:
            return tuple(
                ProviderOddsSnapshot(
                    provider_name=self._name,
                    provider_role=self._role,
                    event_external_id=event.external_id,
                    market_external_id=market.external_id,
                    outcome_external_id=out.external_id,
                    odds=out.odds,
                    fetched_at=fetched_at,
                    source_timestamp=out.source_timestamp,
                )
                for market in event.markets
                for out in market.outcomes
            )

        return await self.observe("get_event_odds", call)


def make_failing_provider(name: str = "mock_down") -> MockOddsProvider:
    """Return a provider that simulates a total outage."""
    return MockOddsProvider(name=name, fail=True)


def make_limited_provider(name: str = "mock_limited") -> MockOddsProvider:
    """Return a provider declaring fixtures only, for capability tests."""
    return MockOddsProvider(name=name, capabilities=frozenset({ProviderCapability.FIXTURES}))


__all__: Final[list[str]] = [
    "CORRUPT_EVENT_ID",
    "DUPLICATE_EXTERNAL_ID",
    "NO_ODDS_EVENT_ID",
    "REFERENCE_NOW",
    "STALE_EVENT_ID",
    "SUSPENDED_EVENT_ID",
    "MockOddsProvider",
    "make_failing_provider",
    "make_limited_provider",
]
