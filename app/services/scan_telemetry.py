"""Recording what a scan examined and why it rejected the rest.

**Observability only.** Nothing here changes what is analysed, published or
settled. Telemetry failing must never cost a selection, so every write is
best-effort: a broken recorder degrades the evidence, not the product.

**Counts unique fixtures.** The funnel's unit is the provider event, not the
selection row. One fixture publishing in six services is one fixture, and the
first diagnostic's funnel widened at the last stage precisely because it
counted rows there and fixtures everywhere else.

**Keeps rejections.** A fixture discarded for an unsupported competition is
the evidence this module exists to preserve.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import ScanDecision, ScanRun
from app.database.models.scan_telemetry import RejectionCode, ScanRunStatus, ScanStage

logger = get_logger(__name__)

RETENTION_DAYS = 400
"""How long scan telemetry is kept.

Long enough to compare a weekday across a full season. Deliberately by age of
the scan and not by whether its fixtures finished: the value of a rejection
record is entirely historical, which is the opposite of a stored analysis.
"""


class ScanTelemetry:
    """Accumulates one scan's decisions and writes them once."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._run: ScanRun | None = None
        self._decisions: dict[str, ScanDecision] = {}

    @property
    def run(self) -> ScanRun | None:
        """The run being recorded, if started."""
        return self._run

    async def start(
        self,
        provider: str,
        hours_ahead: int,
        model_version: str | None = None,
        grid_version: str | None = None,
        now: datetime | None = None,
    ) -> ScanRun | None:
        """Open a run. Returns ``None`` if telemetry could not start."""
        try:
            run = ScanRun(
                started_at=now or datetime.now(UTC),
                provider=provider,
                hours_ahead=hours_ahead,
                model_version=model_version,
                grid_version=grid_version,
                status=ScanRunStatus.RUNNING.value,
                fixtures_seen=0,
                supported=0,
                identity_resolved=0,
                sufficient_history=0,
                model_produced=0,
                fully_modelled=0,
                fixtures_with_qualifying_selection=0,
                selections_published=0,
                duplicates_skipped=0,
                odds_fetched=0,
                error_count=0,
            )
            self._session.add(run)
            await self._session.flush()
            self._run = run
            return run
        except Exception as error:  # noqa: BLE001
            logger.warning("scan_telemetry.start_failed", error=str(error))
            return None

    def record(
        self,
        provider_event_id: str,
        *,
        competition: str | None = None,
        competition_code: str | None = None,
        kickoff: datetime | None = None,
        home_team: str | None = None,
        away_team: str | None = None,
        competition_supported: bool = False,
        identity_resolved: bool = False,
        history_sufficient: bool = False,
        model_produced: bool = False,
        fully_modelled: bool = False,
        rejection_code: RejectionCode | None = None,
        rejection_detail: str | None = None,
        coverage_tier: str | None = None,
        model_version: str | None = None,
    ) -> None:
        """Record one fixture's outcome.

        Recording the same fixture twice replaces the earlier decision rather
        than adding a second. A repeated fixture in the feed, or a retried
        scan, must not inflate the funnel.
        """
        if self._run is None:
            return

        stage = ScanStage.SEEN
        if fully_modelled:
            stage = ScanStage.FULLY_MODELLED
        elif model_produced:
            stage = ScanStage.MODEL_PRODUCED
        elif history_sufficient:
            stage = ScanStage.HISTORY_SUFFICIENT
        elif identity_resolved:
            stage = ScanStage.IDENTITY_RESOLVED
        elif competition_supported:
            stage = ScanStage.SUPPORTED

        existing = self._decisions.get(provider_event_id)
        if existing is None:
            # Defaults are applied by the database at flush, so a decision
            # held in memory before then has None where the schema says 0.
            # Aggregating those raises, and the whole run is then lost to a
            # best-effort except. Set them explicitly on construction.
            existing = ScanDecision(
                scan_run_id=self._run.id,
                provider_event_id=provider_event_id,
                selections_published=0,
                qualified=False,
                competition_supported=False,
                identity_resolved=False,
                history_sufficient=False,
                model_produced=False,
                fully_modelled=False,
            )
            self._decisions[provider_event_id] = existing

        existing.competition = competition
        existing.competition_code = competition_code
        existing.kickoff = kickoff
        existing.home_team = home_team
        existing.away_team = away_team
        existing.competition_supported = competition_supported
        existing.identity_resolved = identity_resolved
        existing.history_sufficient = history_sufficient
        existing.model_produced = model_produced
        existing.fully_modelled = fully_modelled
        existing.terminal_stage = stage.value
        existing.rejection_code = rejection_code.value if rejection_code else None
        existing.rejection_detail = (rejection_detail or None) and rejection_detail[:512]
        existing.coverage_tier = coverage_tier
        existing.model_version = model_version

    def record_duplicate(self, provider_event_id: str) -> None:
        """Note a repeated fixture without counting it again.

        Kept as a counter on the run rather than a decision row, so a noisy
        feed is visible without distorting the fixture funnel.
        """
        if self._run is not None:
            self._run.duplicates_skipped = (self._run.duplicates_skipped or 0) + 1

    async def finish(
        self,
        status: ScanRunStatus = ScanRunStatus.COMPLETED,
        fixtures_seen: int | None = None,
        odds_fetched: int = 0,
        error_count: int = 0,
        now: datetime | None = None,
    ) -> ScanRun | None:
        """Write the decisions and close the run.

        The funnel is computed from the decisions themselves rather than from
        counters incremented along the way, so the stored totals cannot
        disagree with the rows that justify them.
        """
        if self._run is None:
            return None

        try:
            decisions = list(self._decisions.values())
            for decision in decisions:
                self._session.add(decision)

            run = self._run
            run.fixtures_seen = (
                fixtures_seen if fixtures_seen is not None else len(decisions)
            )
            run.supported = sum(1 for d in decisions if d.competition_supported)
            run.identity_resolved = sum(1 for d in decisions if d.identity_resolved)
            run.sufficient_history = sum(1 for d in decisions if d.history_sufficient)
            run.model_produced = sum(1 for d in decisions if d.model_produced)
            run.fully_modelled = sum(1 for d in decisions if d.fully_modelled)
            run.fixtures_with_qualifying_selection = sum(1 for d in decisions if d.qualified)
            run.selections_published = sum(d.selections_published or 0 for d in decisions)
            run.odds_fetched = odds_fetched
            run.error_count = error_count
            run.completed_at = now or datetime.now(UTC)
            run.status = status.value

            if not run.funnel_is_monotonic():
                # Loud, because a widening funnel means the stages were counted
                # over different populations — the failure that made the first
                # diagnostic unusable. The run is still written, flagged, so the
                # evidence of the fault survives.
                logger.error(
                    "scan_telemetry.funnel_not_monotonic",
                    funnel=dict(run.funnel()),
                )

            await self._session.flush()
            logger.info(
                "scan_telemetry.recorded",
                run=run.id,
                seen=run.fixtures_seen,
                supported=run.supported,
                fully_modelled=run.fully_modelled,
                qualified=run.fixtures_with_qualifying_selection,
            )
            return run
        except Exception as error:  # noqa: BLE001
            logger.warning("scan_telemetry.finish_failed", error=str(error))
            return None

    async def attach_publication(
        self, published_by_fixture: dict[str, int], run_id: int | None = None
    ) -> None:
        """Mark which fixtures produced published selections.

        Called after publication, since qualification is decided later than the
        scan. Counts of selections are stored per fixture; the funnel still
        counts the fixture once.
        """
        target = run_id if run_id is not None else (self._run.id if self._run else None)
        if target is None:
            return
        try:
            rows = await self._session.execute(
                select(ScanDecision).where(ScanDecision.scan_run_id == target)
            )
            qualified = 0
            published = 0
            for decision in rows.scalars().all():
                count = published_by_fixture.get(decision.provider_event_id, 0)
                decision.selections_published = count
                decision.qualified = count > 0
                if count:
                    qualified += 1
                    published += count
                    decision.terminal_stage = ScanStage.PUBLISHED.value
                    decision.rejection_code = None
                elif decision.fully_modelled and decision.rejection_code is None:
                    decision.rejection_code = RejectionCode.NO_SERVICE_QUALIFIED.value

            run = await self._session.get(ScanRun, target)
            if run is not None:
                run.fixtures_with_qualifying_selection = qualified
                run.selections_published = published
            await self._session.flush()
        except Exception as error:  # noqa: BLE001
            logger.warning("scan_telemetry.attach_failed", error=str(error))

    async def prune(self, now: datetime | None = None) -> int:
        """Drop telemetry older than the retention window.

        By scan age only. Tying this to fixture completion would reproduce the
        deletion that made the original question unanswerable.
        """
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(days=RETENTION_DAYS)
        result = await self._session.execute(
            delete(ScanRun).where(ScanRun.started_at < cutoff)
        )
        return int(result.rowcount or 0)


__all__ = ["RETENTION_DAYS", "ScanTelemetry"]
