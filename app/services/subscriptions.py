"""Subscriptions, trials and what each tier may reach.

**No payment processor appears here.** Entitlement is a question about a user's
state, not about how they paid, so the rules live apart from any gateway. A
processor writes a payment record; this module decides what that record
entitles someone to. If a gateway rejects us or we change provider, nothing in
this file moves.

**The evidence stays free.** History and the track record are never gated.
They are how someone decides whether the paid part is worth anything, and
hiding the losses behind a paywall would make the honesty performative — the
one thing the product cannot afford.

**Access is granted by time, never by outcome.** A subscription buys a period
of access to analysis. It does not buy results, and nothing here should ever
be conditioned on whether selections won.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Final


class Tier(str, Enum):
    """What a user currently has."""

    FREE = "free"
    TRIAL = "trial"
    PRO = "pro"
    EXPIRED = "expired"
    """Had a trial or subscription that has run out.

    Distinct from ``FREE`` so the product can speak differently to someone who
    has already seen the paid features.
    """


TRIAL_DAYS: Final[int] = 7
"""Length of the free trial.

Long enough to cover a full weekend of football and to watch a few selections
settle, which is the only way to judge the product honestly.
"""


class Feature(str, Enum):
    """Something a user may or may not reach."""

    TODAYS_ANALYSIS = "todays_analysis"
    HISTORY = "history"
    TRACK_RECORD = "track_record"
    HOW_IT_WORKS = "how_it_works"

    BEST_OF_TODAY = "best_of_today"
    MARKET_EXPLORER = "market_explorer"
    TEAM_INTELLIGENCE = "team_intelligence"
    COMPETITIONS = "competitions"
    FOLLOW = "follow"
    SEARCH = "search"


FREE_FEATURES: Final[frozenset[Feature]] = frozenset(
    {
        Feature.TODAYS_ANALYSIS,
        Feature.HISTORY,
        Feature.TRACK_RECORD,
        Feature.HOW_IT_WORKS,
    }
)
"""What anyone may reach without paying.

History and the track record are deliberately open. They are the evidence, and
evidence a prospective customer cannot inspect is just a claim. Today's
analysis is open so the quality is visible before anyone is asked for money.
"""

PAID_FEATURES: Final[frozenset[Feature]] = frozenset(Feature) - FREE_FEATURES


@dataclass(frozen=True)
class Access:
    """A user's current entitlement."""

    tier: Tier
    expires_at: datetime | None = None
    started_at: datetime | None = None

    @property
    def active(self) -> bool:
        """Whether paid features are currently reachable."""
        return self.tier in {Tier.TRIAL, Tier.PRO}

    def days_left(self, now: datetime | None = None) -> int | None:
        """Whole days remaining, or ``None`` if not time-limited."""
        if self.expires_at is None:
            return None
        moment = now or datetime.now(UTC)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        remaining = expires - moment
        return max(0, remaining.days + (1 if remaining.seconds else 0))

    def allows(self, feature: Feature) -> bool:
        """Whether this entitlement reaches a feature."""
        if feature in FREE_FEATURES:
            return True
        return self.active


def resolve(
    tier: str | None,
    expires_at: datetime | None,
    started_at: datetime | None = None,
    now: datetime | None = None,
) -> Access:
    """Return a user's entitlement, expiring it if its time has passed.

    Expiry is computed rather than stored as a state, so a lapsed subscription
    cannot keep working because a background job failed to run.
    """
    moment = now or datetime.now(UTC)
    try:
        current = Tier(tier or Tier.FREE.value)
    except ValueError:
        current = Tier.FREE

    if current in {Tier.TRIAL, Tier.PRO} and expires_at is not None:
        expires = expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= moment:
            return Access(Tier.EXPIRED, expires_at=expires, started_at=started_at)

    return Access(current, expires_at=expires_at, started_at=started_at)


def start_trial(now: datetime | None = None) -> Access:
    """Begin a trial."""
    moment = now or datetime.now(UTC)
    return Access(
        Tier.TRIAL,
        expires_at=moment + timedelta(days=TRIAL_DAYS),
        started_at=moment,
    )


def grant(days: int, now: datetime | None = None) -> Access:
    """Grant or extend paid access by a number of days."""
    moment = now or datetime.now(UTC)
    return Access(Tier.PRO, expires_at=moment + timedelta(days=days), started_at=moment)


def extend(access: Access, days: int, now: datetime | None = None) -> Access:
    """Add days to an entitlement, from whichever is later.

    Renewing early must not shorten a subscription, so the extension runs from
    the existing expiry when that is still in the future.
    """
    moment = now or datetime.now(UTC)
    base = moment
    if access.expires_at is not None:
        expires = access.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        base = max(moment, expires)
    return Access(Tier.PRO, expires_at=base + timedelta(days=days), started_at=moment)


FEATURE_LABELS: Final[dict[Feature, str]] = {
    Feature.BEST_OF_TODAY: "🔥 Best of today",
    Feature.MARKET_EXPLORER: "📊 Market explorer",
    Feature.TEAM_INTELLIGENCE: "👥 Team intelligence",
    Feature.COMPETITIONS: "🏆 Competitions",
    Feature.FOLLOW: "⭐ Following",
    Feature.SEARCH: "🔎 Search",
    Feature.TODAYS_ANALYSIS: "⚽ Today's analysis",
    Feature.HISTORY: "📚 History",
    Feature.TRACK_RECORD: "📈 Track record",
    Feature.HOW_IT_WORKS: "📖 How it works",
}


def describe(access: Access, now: datetime | None = None) -> str:
    """Return a short description of where a user stands."""
    if access.tier is Tier.PRO:
        days = access.days_left(now)
        return f"Pro · {days} day(s) remaining" if days else "Pro"
    if access.tier is Tier.TRIAL:
        days = access.days_left(now)
        return f"Trial · {days} day(s) remaining" if days else "Trial ending today"
    if access.tier is Tier.EXPIRED:
        return "Expired"
    return "Free"
