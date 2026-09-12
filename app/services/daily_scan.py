"""Daily fixture scan.

Fetches upcoming fixtures, analyses each one, and stores the result. Run on a
schedule by the worker so the bot never computes anything on a user tap.

**Why precompute.** A single analysis costs several database queries plus an
Elo replay. Doing that per tap makes the interface slow and the cost scale with
users rather than with fixtures. Precomputing makes it scale with fixtures —
roughly fifty a day — no matter how many people are reading.

**Odds are fetched selectively.** The provider's free tier allows 100 requests
daily and each fixture's odds is one request, so odds are fetched only for
fixtures that are actually modellable and within the near window. An
unmodellable fixture gains nothing from a price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import StoredAnalysis
from app.providers.base import OddsProvider
from app.providers.errors import ProviderError, ProviderRateLimitError
from app.providers.models import ProviderEvent
from app.services.match_analysis import Coverage, MatchAnalysis, MatchAnalysisService

logger = get_logger(__name__)

ODDS_WINDOW_HOURS = 30
"""Only fetch odds for fixtures inside this window.

Prices further out move enough to be worth refetching anyway, so spending
quota on them wastes the allowance.
"""

RETENTION_HOURS = 6
"""How long a finished fixture's analysis is kept before being cleared."""


@dataclass
class ScanReport:
    """What one scan covered."""

    fixtures_seen: int = 0
    analysed: int = 0
    stored: int = 0
    odds_fetched: int = 0
    errors: int = 0
    duplicates_skipped: int = 0
    coverage: dict[str, int] = field(default_factory=dict)
    quota_exhausted: bool = False

    def summary(self) -> str:
        """Return a one-line human summary."""
        grades = ", ".join(f"{k}={v}" for k, v in sorted(self.coverage.items()))
        return (
            f"{self.fixtures_seen} fixtures, {self.stored} stored, "
            f"{self.odds_fetched} priced, {self.duplicates_skipped} duplicates, "
            f"{self.errors} errors"
            + (f"; coverage: {grades}" if grades else "")
            + ("; quota exhausted" if self.quota_exhausted else "")
        )


class DailyScanService:
    """Fetches, analyses and stores fixtures in bulk."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def scan(
        self,
        provider: OddsProvider,
        hours_ahead: int = 48,
        fetch_odds: bool = True,
        now: datetime | None = None,
    ) -> ScanReport:
        """Analyse every upcoming fixture and store the results.

        Args:
            provider: Live fixture source.
            hours_ahead: How far ahead to scan.
            fetch_odds: Whether to spend quota on prices.
            now: Clock override, for tests.
        """
        moment = now or datetime.now(UTC)
        report = ScanReport()

        try:
            events = await provider.get_events(hours_ahead=hours_ahead)
        except ProviderRateLimitError:
            logger.warning("scan.quota_exhausted")
            report.quota_exhausted = True
            return report
        except ProviderError as exc:
            logger.warning("scan.fixtures_failed", error=str(exc))
            report.errors += 1
            return report

        report.fixtures_seen = len(events)
        analyser = MatchAnalysisService(self._session)
        seen: set[str] = set()

        for event in events:
            # Feeds do repeat fixtures. Analysing one twice wastes an odds
            # request and would double-count coverage in the report.
            if event.external_id in seen:
                report.duplicates_skipped += 1
                continue
            seen.add(event.external_id)

            enriched = event
            if fetch_odds and self._wants_odds(event, moment):
                enriched, spent = await self._with_odds(provider, event, report)
                if spent:
                    report.odds_fetched += 1

            try:
                analysis = await analyser.analyse_event(enriched, now=moment)
            except Exception as exc:  # noqa: BLE001 - one bad fixture must not
                # abort the scan; the rest of the day's card is still useful.
                logger.warning(
                    "scan.analysis_failed",
                    fixture=event.external_id,
                    error=str(exc),
                )
                report.errors += 1
                continue

            report.analysed += 1
            grade = str(analysis.coverage)
            report.coverage[grade] = report.coverage.get(grade, 0) + 1

            await self._store(provider.name, event, analysis, moment)
            report.stored += 1

        await self._prune(moment)
        await self._session.flush()
        logger.info("scan.completed", summary=report.summary())
        return report

    @staticmethod
    def _wants_odds(event: ProviderEvent, moment: datetime) -> bool:
        """Whether this fixture is worth spending a request on."""
        return event.start_time <= moment + timedelta(hours=ODDS_WINDOW_HOURS)

    async def _with_odds(
        self, provider: OddsProvider, event: ProviderEvent, report: ScanReport
    ) -> tuple[ProviderEvent, bool]:
        """Attach odds to a fixture, tolerating quota exhaustion."""
        try:
            detailed = await provider.get_event(event.external_id)
        except ProviderRateLimitError:
            report.quota_exhausted = True
            logger.info("scan.odds_skipped_quota", fixture=event.external_id)
            return event, False
        except ProviderError as exc:
            logger.debug("scan.odds_unavailable", fixture=event.external_id, error=str(exc))
            return event, False
        return detailed, True

    async def _store(
        self,
        provider_name: str,
        event: ProviderEvent,
        analysis: MatchAnalysis,
        moment: datetime,
    ) -> None:
        """Insert or replace the stored analysis for a fixture."""
        result = await self._session.execute(
            select(StoredAnalysis).where(
                StoredAnalysis.provider_name == provider_name,
                StoredAnalysis.provider_event_id == event.external_id,
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            record = StoredAnalysis(
                provider_name=provider_name,
                provider_event_id=event.external_id,
            )
            self._session.add(record)

        record.home_name = analysis.home_name
        record.away_name = analysis.away_name
        record.competition = analysis.competition
        record.country = event.competition.country if event.competition else None
        record.kickoff = analysis.kickoff
        record.coverage = str(analysis.coverage)
        record.unavailable_reason = analysis.unavailable_reason
        record.markets = _serialise_markets(analysis.markets)
        record.expected_home_goals = analysis.expected_home_goals
        record.expected_away_goals = analysis.expected_away_goals
        record.market_odds = _serialise(analysis.market_odds)
        record.market_probabilities = _serialise(analysis.market_probabilities)
        record.home_stats = _stats(analysis, home=True)
        record.away_stats = _stats(analysis, home=False)
        record.model_probabilities = {
            key: str(value) for key, value in (analysis.model_probabilities or {}).items()
        }
        record.component_views = [
            {key: str(value) for key, value in view.items()} for view in analysis.component_views
        ]
        record.components_used = list(analysis.components_used)
        record.components_dropped = list(analysis.components_dropped)
        record.provenance = {
            "fixture_source": provider_name,
            "odds_source": provider_name if analysis.market_odds else None,
            "history_source": "csv_historical",
            "coverage": str(analysis.coverage),
            "coverage_reason": analysis.unavailable_reason,
            "home_matches": analysis.home.matches,
            "away_matches": analysis.away.matches,
            "model_probabilities": {
                key: str(value) for key, value in (analysis.model_probabilities or {}).items()
            },
            "component_views": [
                {key: str(value) for key, value in view.items()}
                for view in analysis.component_views
            ],
            "components_used": list(analysis.components_used),
            "components_dropped": list(analysis.components_dropped),
            "computed_at": moment.isoformat(),
        }
        record.computed_at = moment

    async def _prune(self, moment: datetime) -> None:
        """Remove analyses for fixtures that finished some time ago."""
        await self._session.execute(
            delete(StoredAnalysis).where(
                StoredAnalysis.kickoff < moment - timedelta(hours=RETENTION_HOURS)
            )
        )


class AnalysisRepository:
    """Reads stored analyses for the bot."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upcoming(self, limit: int = 40, now: datetime | None = None) -> list[StoredAnalysis]:
        """Return upcoming fixtures, soonest first.

        Includes fixtures that could not be modelled: a user asking what is on
        today should see the whole card, with the unanalysable ones labelled
        rather than missing.
        """
        moment = now or datetime.now(UTC)
        result = await self._session.execute(
            select(StoredAnalysis)
            .where(StoredAnalysis.kickoff >= moment - timedelta(hours=2))
            .order_by(StoredAnalysis.kickoff)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get(self, provider_event_id: str) -> StoredAnalysis | None:
        """Return one stored analysis by fixture id."""
        result = await self._session.execute(
            select(StoredAnalysis).where(StoredAnalysis.provider_event_id == provider_event_id)
        )
        return result.scalar_one_or_none()

    async def count(self, now: datetime | None = None) -> int:
        """Return how many upcoming analyses are stored."""
        return len(await self.upcoming(limit=500, now=now))

    async def last_computed(self) -> datetime | None:
        """Return when the most recent scan ran."""
        result = await self._session.execute(
            select(StoredAnalysis.computed_at).order_by(StoredAnalysis.computed_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()


def _serialise(values: dict[str, Decimal] | None) -> dict[str, object] | None:
    """Convert decimals to strings for JSON storage.

    Strings rather than floats: a probability round-tripped through float and
    back is no longer the number the model produced, and small differences
    matter when comparing against a price.
    """
    if values is None:
        return None
    return {k: str(v) for k, v in values.items()}  # type: ignore[return-value]


def _serialise_markets(
    markets: dict[str, dict[str, Decimal]],
) -> dict[str, object]:
    """Convert every market's probabilities for storage."""
    return {name: {k: str(v) for k, v in outcomes.items()} for name, outcomes in markets.items()}


def _stats(analysis: MatchAnalysis, home: bool) -> dict[str, object]:
    """Summarise one side's record for storage."""
    snapshot = analysis.home if home else analysis.away
    return {
        "name": snapshot.source_name,
        "resolved": snapshot.is_resolved,
        "team_id": snapshot.team_id,
        "matches": snapshot.matches,
        "scored_per_match": round(snapshot.goals_scored_per_match, 2),
        "conceded_per_match": round(snapshot.goals_conceded_per_match, 2),
        "points_per_match": round(snapshot.points_per_match, 2),
        "elo": snapshot.elo,
    }


def coverage_of(record: StoredAnalysis) -> Coverage:
    """Return the coverage grade of a stored record."""
    try:
        return Coverage(record.coverage)
    except ValueError:
        return Coverage.UNSUPPORTED
