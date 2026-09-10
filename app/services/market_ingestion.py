"""Market and odds ingestion.

Turns provider events into canonical fixtures, markets, outcomes and odds
snapshots.

Two things distinguish this from historical ingestion. Prices change, so
outcomes are updated in place while every observation is *also* appended to
``odds_snapshots`` — the current price is state, the price history is a ledger,
and closing-line comparison needs the ledger. And fixtures arrive whose teams
cannot be resolved, so every match carries an explicit modellability status
rather than being silently omitted; a coverage gap must be distinguishable from
"no value found".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.enums import (
    MatchStatus,
    ModellabilityStatus,
    OutcomeStatus,
    SnapshotType,
    StalenessStatus,
)
from app.database.enums import (
    ProviderRole as DbProviderRole,
)
from app.database.models import (
    Market,
    Match,
    OddsSnapshot,
    Outcome,
)
from app.providers.base import OddsProvider
from app.providers.models import (
    ProviderEvent,
    ProviderEventStatus,
    ProviderOutcomeStatus,
    ProviderRole,
)
from app.services.team_resolution import TeamResolver

logger = get_logger(__name__)

DEFAULT_ODDS_TTL = timedelta(minutes=15)
"""How long a price is treated as fresh.

Pre-match prices move on team news and money. Fifteen minutes is short enough
that a scan result is still broadly valid when the user reads it, and long
enough to avoid refetching constantly.
"""

_STATUS_MAP = {
    ProviderEventStatus.SCHEDULED: MatchStatus.SCHEDULED,
    ProviderEventStatus.LIVE: MatchStatus.LIVE,
    # No MatchStatus.SUSPENDED: a suspended event is still scheduled to be
    # played. Suspension is a property of the prices, and is carried on the
    # outcomes, which is where it actually blocks a selection.
    ProviderEventStatus.SUSPENDED: MatchStatus.SCHEDULED,
    ProviderEventStatus.FINISHED: MatchStatus.FINISHED,
    ProviderEventStatus.POSTPONED: MatchStatus.POSTPONED,
    ProviderEventStatus.CANCELLED: MatchStatus.CANCELLED,
    ProviderEventStatus.UNKNOWN: MatchStatus.SCHEDULED,
}

_OUTCOME_STATUS_MAP = {
    ProviderOutcomeStatus.ACTIVE: OutcomeStatus.ACTIVE,
    ProviderOutcomeStatus.SUSPENDED: OutcomeStatus.SUSPENDED,
    ProviderOutcomeStatus.SETTLED: OutcomeStatus.SETTLED,
    ProviderOutcomeStatus.REMOVED: OutcomeStatus.REMOVED,
}


@dataclass
class MarketIngestionReport:
    """Outcome of one market ingestion run."""

    provider_name: str
    events_seen: int = 0
    matches_created: int = 0
    matches_updated: int = 0
    markets_upserted: int = 0
    outcomes_upserted: int = 0
    snapshots_written: int = 0
    duplicates_skipped: int = 0
    unmodellable: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        """Return a one-line human summary."""
        return (
            f"{self.provider_name}: {self.events_seen} events, "
            f"{self.matches_created} new, {self.matches_updated} updated, "
            f"{self.outcomes_upserted} outcomes, "
            f"{self.snapshots_written} snapshots, "
            f"{self.duplicates_skipped} duplicates skipped"
        )


class MarketIngestionService:
    """Ingests provider events into canonical market tables."""

    def __init__(
        self,
        session: AsyncSession,
        odds_ttl: timedelta = DEFAULT_ODDS_TTL,
        sport: str = "football",
    ) -> None:
        self._session = session
        self._ttl = odds_ttl
        self._sport = sport

    async def ingest_events(
        self,
        provider: OddsProvider,
        hours_ahead: int = 48,
        snapshot_type: SnapshotType = SnapshotType.INTERIM,
        now: datetime | None = None,
    ) -> MarketIngestionReport:
        """Fetch and store fixtures, markets and prices.

        Args:
            provider: Source of events.
            hours_ahead: Fixture window.
            snapshot_type: Whether these are opening, interim or closing prices.
            now: Clock override, for deterministic tests.
        """
        moment = now or datetime.now(UTC)
        report = MarketIngestionReport(provider_name=provider.name)
        events = await provider.get_events(hours_ahead=hours_ahead)

        seen_external_ids: set[str] = set()
        for event in events:
            # Providers do emit the same fixture twice. Deduplicating here
            # rather than trusting the feed keeps one canonical row per event.
            if event.external_id in seen_external_ids:
                report.duplicates_skipped += 1
                continue
            seen_external_ids.add(event.external_id)
            report.events_seen += 1

            match = await self._upsert_match(provider, event, report)
            await self._upsert_markets(provider, event, match, report, moment, snapshot_type)

        await self._session.flush()
        logger.info("market.ingested", **_loggable(report))
        return report

    async def _upsert_match(
        self, provider: OddsProvider, event: ProviderEvent, report: MarketIngestionReport
    ) -> Match:
        """Create or update the canonical fixture."""
        result = await self._session.execute(
            select(Match).where(
                Match.provider_name == provider.name,
                Match.provider_event_id == event.external_id,
            )
        )
        match = result.scalar_one_or_none()
        status = _STATUS_MAP.get(event.status, MatchStatus.SCHEDULED)

        if match is None:
            match = Match(
                provider_name=provider.name,
                provider_event_id=event.external_id,
                sport=event.sport,
                home_team_raw=event.home_team.name,
                away_team_raw=event.away_team.name,
                competition_raw=event.competition.name if event.competition else None,
                country=event.competition.country if event.competition else None,
                start_time=event.start_time,
                status=status,
                modellability=ModellabilityStatus.UNRESOLVED_TEAMS,
            )
            self._session.add(match)
            report.matches_created += 1
        else:
            match.status = status
            match.start_time = event.start_time
            report.matches_updated += 1

        await self._resolve_teams(provider, event, match, report)
        await self._session.flush()
        return match

    async def _resolve_teams(
        self,
        provider: OddsProvider,
        event: ProviderEvent,
        match: Match,
        report: MarketIngestionReport,
    ) -> None:
        """Attach canonical teams, or record why the fixture is unmodellable.

        Teams are never created from an odds feed. Canonical identity is
        bootstrapped from historical data, which is the source that actually
        supports a team's ratings; letting a bookmaker's spelling mint a new
        club would create teams with no history attached.
        """
        resolver = TeamResolver(self._session)
        country = event.competition.country if event.competition else None

        home = await resolver.resolve(
            provider.name, event.home_team.name, sport=self._sport, country=country
        )
        away = await resolver.resolve(
            provider.name, event.away_team.name, sport=self._sport, country=country
        )

        if home.is_resolved and away.is_resolved and home.team and away.team:
            match.home_team_id = home.team.id
            match.away_team_id = away.team.id
            match.modellability = ModellabilityStatus.MODELLABLE
            match.modellability_note = None
            return

        unresolved = [
            name
            for name, res in (
                (event.home_team.name, home),
                (event.away_team.name, away),
            )
            if not res.is_resolved
        ]
        match.modellability = ModellabilityStatus.UNRESOLVED_TEAMS
        match.modellability_note = (
            f"No canonical team for: {', '.join(unresolved)}. "
            "The fixture is stored but excluded from analysis."
        )
        key = str(ModellabilityStatus.UNRESOLVED_TEAMS)
        report.unmodellable[key] = report.unmodellable.get(key, 0) + 1

    async def _upsert_markets(
        self,
        provider: OddsProvider,
        event: ProviderEvent,
        match: Match,
        report: MarketIngestionReport,
        moment: datetime,
        snapshot_type: SnapshotType,
    ) -> None:
        """Store each market, its outcomes, and a snapshot per price."""
        role = (
            DbProviderRole.REFERENCE
            if provider.role is ProviderRole.REFERENCE
            else DbProviderRole.TRADEABLE
        )

        for provider_market in event.markets:
            market = await self._get_or_create_market(
                match,
                provider_market.external_id,
                provider_market.name,
                provider_market.specifier,
                report,
            )

            for provider_outcome in provider_market.outcomes:
                outcome = await self._upsert_outcome(market, provider_outcome, moment, report)
                self._session.add(
                    OddsSnapshot(
                        outcome_id=outcome.id,
                        provider_name=provider.name,
                        provider_role=role,
                        odds=provider_outcome.odds,
                        snapshot_type=snapshot_type,
                        captured_at=moment,
                        source_timestamp=provider_outcome.source_timestamp,
                    )
                )
                report.snapshots_written += 1

    async def _get_or_create_market(
        self,
        match: Match,
        external_id: str,
        name: str,
        specifier: str | None,
        report: MarketIngestionReport,
    ) -> Market:
        """Return the canonical market row, creating it if new."""
        result = await self._session.execute(
            select(Market).where(
                Market.match_id == match.id,
                Market.provider_market_id == external_id,
            )
        )
        market = result.scalar_one_or_none()
        if market is None:
            market = Market(
                match_id=match.id,
                provider_market_id=external_id,
                market_name=name,
                specifier=specifier,
            )
            self._session.add(market)
            await self._session.flush()
        report.markets_upserted += 1
        return market

    async def _upsert_outcome(
        self,
        market: Market,
        provider_outcome: object,
        moment: datetime,
        report: MarketIngestionReport,
    ) -> Outcome:
        """Update the current price, or create the outcome if new."""
        external_id = provider_outcome.external_id  # type: ignore[attr-defined]
        odds: Decimal = provider_outcome.odds  # type: ignore[attr-defined]
        status = _OUTCOME_STATUS_MAP.get(
            provider_outcome.status,  # type: ignore[attr-defined]
            OutcomeStatus.ACTIVE,
        )

        result = await self._session.execute(
            select(Outcome).where(
                Outcome.market_id == market.id,
                Outcome.provider_outcome_id == external_id,
            )
        )
        outcome = result.scalar_one_or_none()

        if outcome is None:
            outcome = Outcome(
                market_id=market.id,
                provider_outcome_id=external_id,
                outcome_name=provider_outcome.name,  # type: ignore[attr-defined]
                odds=odds,
                status=status,
                last_fetched_at=moment,
                expires_at=moment + self._ttl,
                staleness=StalenessStatus.FRESH,
            )
            self._session.add(outcome)
            await self._session.flush()
        else:
            outcome.odds = odds
            outcome.status = status
            outcome.last_fetched_at = moment
            outcome.expires_at = moment + self._ttl
            outcome.staleness = StalenessStatus.FRESH

        report.outcomes_upserted += 1
        return outcome


def _as_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime.

    PostgreSQL returns timezone-aware values for ``TIMESTAMPTZ``; SQLite, used
    by the test suite, returns naive ones. Everything is written as UTC, so a
    naive value read back is UTC — but comparing it against an aware ``now``
    raises. Normalising here keeps the same code correct on both.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def staleness_of(outcome: Outcome, now: datetime, hard_expiry_factor: int = 4) -> StalenessStatus:
    """Classify how stale a stored price is.

    Three states rather than a boolean: a slightly old price is still useful
    for ranking, whereas an expired one must never reach a booking code. The
    hard expiry is a multiple of the TTL, past which a price is treated as
    gone rather than merely old.
    """
    if outcome.expires_at is None:
        return StalenessStatus.FRESH

    expires_at = _as_utc(outcome.expires_at)
    fetched_at = _as_utc(outcome.last_fetched_at)
    moment = _as_utc(now)

    if moment <= expires_at:
        return StalenessStatus.FRESH

    window = expires_at - fetched_at
    if moment <= expires_at + window * hard_expiry_factor:
        return StalenessStatus.STALE
    return StalenessStatus.EXPIRED


async def refresh_staleness(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """Recompute staleness for every stored outcome.

    Run on a schedule so that a scan can filter on stored state rather than
    recomputing expiry per row at query time.
    """
    moment = now or datetime.now(UTC)
    result = await session.execute(select(Outcome))
    counts: dict[str, int] = {}
    for outcome in result.scalars().all():
        status = staleness_of(outcome, moment)
        outcome.staleness = status
        counts[str(status)] = counts.get(str(status), 0) + 1
    await session.flush()
    return counts


def _loggable(report: MarketIngestionReport) -> dict[str, object]:
    """Return report fields suitable for structured logging."""
    return {
        "provider": report.provider_name,
        "events": report.events_seen,
        "created": report.matches_created,
        "updated": report.matches_updated,
        "outcomes": report.outcomes_upserted,
        "snapshots": report.snapshots_written,
        "duplicates": report.duplicates_skipped,
    }
