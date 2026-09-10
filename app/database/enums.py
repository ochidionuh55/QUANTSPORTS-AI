"""Domain enumerations.

Stored as native PostgreSQL enum types. Adding a value later requires a
migration, which is the point: it forces a deliberate decision rather than
silently widening the domain.
"""

from __future__ import annotations

from enum import StrEnum


class UserPlan(StrEnum):
    """Subscription tier."""

    FREE = "free"
    STANDARD = "standard"
    PREMIUM = "premium"


class TransactionType(StrEnum):
    """Why a ledger entry exists."""

    SIGNUP_BONUS = "signup_bonus"
    PURCHASE = "purchase"
    ADMIN_ADJUSTMENT = "admin_adjustment"
    SCAN_RESERVATION = "scan_reservation"
    SCAN_SETTLEMENT = "scan_settlement"
    SCAN_RELEASE = "scan_release"
    BOOKING_CODE = "booking_code"
    REFUND = "refund"


class TransactionState(StrEnum):
    """Lifecycle of a ledger entry.

    A reservation is never a completed deduction. Credits move to COMPLETED
    only after the operation succeeds, and to RELEASED if it does not.
    """

    RESERVED = "reserved"
    COMPLETED = "completed"
    RELEASED = "released"
    FAILED = "failed"


class ReviewStatus(StrEnum):
    """Team-alias resolution state.

    Low-confidence fuzzy matches are never accepted silently; they queue for
    review, because a wrong alias silently fragments a team's Elo rating and
    corrupts every downstream estimate.
    """

    AUTO_CONFIRMED = "auto_confirmed"
    MANUALLY_CONFIRMED = "manually_confirmed"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"


class MatchStatus(StrEnum):
    """Fixture state as reported by the provider."""

    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ModellabilityStatus(StrEnum):
    """Whether a fixture can be modelled at all.

    Bookmaker fixture lists cover far more competitions than we hold history
    for. Unmodellable matches are reported as such rather than dropped: a
    coverage gap must never be indistinguishable from "no value found".
    """

    MODELLABLE = "modellable"
    NO_HISTORICAL_COVERAGE = "no_historical_coverage"
    UNRESOLVED_TEAMS = "unresolved_teams"
    INSUFFICIENT_MATCHES = "insufficient_matches"
    UNSUPPORTED_SPORT = "unsupported_sport"


class OutcomeStatus(StrEnum):
    """Availability of a single betting outcome."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    SETTLED = "settled"
    REMOVED = "removed"


class StalenessStatus(StrEnum):
    """Freshness of stored odds.

    Booking-code generation must never use anything but FRESH data; prices are
    revalidated immediately before a code is created.
    """

    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"


class ProviderRole(StrEnum):
    """What a price is used for.

    The central distinction of the quant architecture. A REFERENCE price comes
    from a sharp book and forms the prior. A TRADEABLE price is the one bet
    into. Measuring expected value against the same book that supplied the
    prior compares a book to itself and drives edge structurally to zero.
    """

    REFERENCE = "reference"
    TRADEABLE = "tradeable"


class SnapshotType(StrEnum):
    """Position of an odds observation in a market's lifecycle."""

    OPENING = "opening"
    INTERIM = "interim"
    CLOSING = "closing"


class MarginMethod(StrEnum):
    """Method used to strip bookmaker margin from quoted odds.

    Proportional scaling is biased: it over-prices longshots. It is retained
    only as a baseline for backtest comparison.
    """

    PROPORTIONAL = "proportional"
    POWER = "power"
    SHIN = "shin"


class EnsembleMethod(StrEnum):
    """How model estimates are combined with the market prior."""

    MARKET_ONLY = "market_only"
    LOG_ODDS_SHRINKAGE = "log_odds_shrinkage"
    BAYESIAN_UPDATE = "bayesian_update"
    RESIDUAL_MODEL = "residual_model"
    WEIGHTED_AVERAGE = "weighted_average"


class ModelStatus(StrEnum):
    """Promotion state of a model version.

    Only a PROMOTED version may drive user-facing value detection.
    """

    DRAFT = "draft"
    TRAINING = "training"
    VALIDATED = "validated"
    PROMOTED = "promoted"
    RETIRED = "retired"
    REJECTED = "rejected"


class ScanType(StrEnum):
    """Requested scan breadth."""

    TODAY = "today"
    NEXT_24H = "next_24h"
    NEXT_48H = "next_48h"
    CUSTOM_RANGE = "custom_range"


class ScanMode(StrEnum):
    """Selection filter profile."""

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"
    RESEARCH = "research"


class ScanStatus(StrEnum):
    """Scan execution state."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_OPPORTUNITY = "no_opportunity"
    CANCELLED = "cancelled"


class BookingCodeStatus(StrEnum):
    """Lifecycle of a generated booking code."""

    PENDING = "pending"
    ACTIVE = "active"
    EXPIRED = "expired"
    FAILED = "failed"
    PARTIALLY_UNAVAILABLE = "partially_unavailable"
