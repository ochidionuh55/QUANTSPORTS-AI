"""Provider capabilities and normalized data transfer objects.

Adapters return these, never SQLAlchemy models. Two reasons: an adapter that
returns ORM objects has to know about sessions, identity mapping and the
canonical team registry — which is the ingestion layer's job, not the
adapter's; and a DTO can carry provider-shaped data (raw names, external IDs,
unmapped competitions) that has no place in the canonical schema.

Every timestamp is timezone-aware UTC, enforced by validator rather than
convention. ``source_timestamp`` and ``fetched_at`` are kept strictly separate:
conflating them silently destroys the ability to reconstruct what was knowable
at prediction time, which is the whole basis of the backtest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProviderCapability(StrEnum):
    """A discrete thing a provider can do.

    Capabilities exist so the application can ask "does this provider support
    X" instead of "is this provider called sporty". Name-based branching makes
    every new provider a change to the call sites; capability-based dispatch
    does not.
    """

    FIXTURES = "fixtures"
    ODDS = "odds"
    LIVE_ODDS = "live_odds"
    HISTORICAL_ODDS = "historical_odds"
    BOOKING_CODES = "booking_codes"
    COMPETITION_METADATA = "competition_metadata"
    EVENT_DETAIL = "event_detail"


class ProviderRole(StrEnum):
    """What a provider's prices mean to the model.

    ``REFERENCE`` prices form the prior — ideally a sharp book or a consensus.
    ``TRADEABLE`` prices are what the user can actually bet into. Measuring
    edge against the same book that produced the prior compares a market with
    itself and drives expected value to zero by construction, so the roles must
    stay distinct.

    Mirrors ``odds_snapshots.provider_role`` from Phase 2.
    """

    REFERENCE = "reference"
    TRADEABLE = "tradeable"


class ProviderEventStatus(StrEnum):
    """Lifecycle state of an event as the provider reports it."""

    SCHEDULED = "scheduled"
    LIVE = "live"
    SUSPENDED = "suspended"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ProviderOutcomeStatus(StrEnum):
    """Whether an individual outcome can currently be backed."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    SETTLED = "settled"
    REMOVED = "removed"


def _require_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to timezone-aware UTC.

    A naive datetime is rejected rather than assumed to be UTC. Guessing here
    would produce timestamps that are silently wrong by the provider's offset,
    which is invisible until a backtest quietly leaks future information.

    Raises:
        ValueError: If the datetime carries no timezone.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(
            "Naive datetime rejected: provider timestamps must declare a "
            "timezone so they can be converted to UTC unambiguously."
        )
    return value.astimezone(UTC)


class ProviderDTO(BaseModel):
    """Base for provider DTOs: immutable and strict."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProviderCompetition(ProviderDTO):
    """A league or tournament as the provider describes it."""

    provider_name: str
    external_id: str
    name: str
    country: str | None = None
    sport: str = "football"
    raw: dict[str, Any] = Field(default_factory=dict)


class ProviderTeam(ProviderDTO):
    """A team as the provider names it.

    The name is deliberately left unresolved. Mapping it to a canonical team is
    the ingestion layer's responsibility, via the Phase 2 resolver.
    """

    provider_name: str
    external_id: str | None = None
    name: str
    country: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ProviderOutcome(ProviderDTO):
    """A single backable selection with its price."""

    provider_name: str
    external_id: str
    name: str
    odds: Decimal
    status: ProviderOutcomeStatus = ProviderOutcomeStatus.ACTIVE
    source_timestamp: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_timestamp")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc(value)

    @field_validator("odds")
    @classmethod
    def _odds_exceed_stake(cls, value: Decimal) -> Decimal:
        """Reject decimal odds at or below 1.0.

        Such a price cannot return a profit and indicates corrupt provider
        data. Catching it here keeps it out of the database, where a CHECK
        constraint would abort a whole ingestion batch instead of one record.
        """
        if value <= 1:
            raise ValueError(f"Decimal odds must exceed 1.0, got {value}.")
        return value


class ProviderMarket(ProviderDTO):
    """A market and its outcomes."""

    provider_name: str
    external_id: str
    name: str
    specifier: str | None = None
    outcomes: tuple[ProviderOutcome, ...] = ()
    source_timestamp: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_timestamp")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc(value)


class ProviderEvent(ProviderDTO):
    """A fixture as the provider describes it."""

    provider_name: str
    external_id: str
    home_team: ProviderTeam
    away_team: ProviderTeam
    start_time: datetime
    competition: ProviderCompetition | None = None
    status: ProviderEventStatus = ProviderEventStatus.SCHEDULED
    sport: str = "football"
    markets: tuple[ProviderMarket, ...] = ()
    source_timestamp: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("start_time")
    @classmethod
    def _start_utc(cls, value: datetime) -> datetime:
        coerced = _require_utc(value)
        assert coerced is not None
        return coerced

    @field_validator("source_timestamp")
    @classmethod
    def _source_utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc(value)


class ProviderOddsSnapshot(ProviderDTO):
    """A price observed at a point in time.

    ``fetched_at`` is when we retrieved it; ``source_timestamp`` is when the
    provider says it was generated. The gap between them is provider latency,
    and only ``source_timestamp`` is meaningful for point-in-time
    reconstruction.
    """

    provider_name: str
    provider_role: ProviderRole
    event_external_id: str
    market_external_id: str
    outcome_external_id: str
    odds: Decimal
    fetched_at: datetime
    source_timestamp: datetime | None = None

    @field_validator("fetched_at")
    @classmethod
    def _fetched_utc(cls, value: datetime) -> datetime:
        coerced = _require_utc(value)
        assert coerced is not None
        return coerced

    @field_validator("source_timestamp")
    @classmethod
    def _source_utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc(value)

    @property
    def latency_seconds(self) -> float | None:
        """Seconds between provider generation and our retrieval."""
        if self.source_timestamp is None:
            return None
        return (self.fetched_at - self.source_timestamp).total_seconds()


class ProviderHealth(ProviderDTO):
    """Result of a provider health probe."""

    provider_name: str
    provider_role: ProviderRole
    healthy: bool
    checked_at: datetime
    latency_ms: float | None = None
    detail: str | None = None

    @field_validator("checked_at")
    @classmethod
    def _checked_utc(cls, value: datetime) -> datetime:
        coerced = _require_utc(value)
        assert coerced is not None
        return coerced


class ProviderBookingCode(ProviderDTO):
    """A booking code returned by a booking-capable provider."""

    provider_name: str
    code: str
    share_url: str | None = None
    expires_at: datetime | None = None
    unavailable_selections: tuple[str, ...] = ()

    @field_validator("expires_at")
    @classmethod
    def _expiry_utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc(value)
