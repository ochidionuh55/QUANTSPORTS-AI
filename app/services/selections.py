"""Publishing, settling and reading Best of the Day selections.

The lifecycle in one place: build model-only forecasts from stored analyses,
rank them through the validated engine, write the qualifying ones before
kickoff, and fill in results afterwards.

**Withheld services never publish.** Validation over 1,740 days found two
services whose selections win materially less often than they claim. They stay
defined so their status is visible internally, and they are excluded from every
published list, because a service that promises 63% and delivers 52% costs
users money while telling them they are winning.

**Publication is once.** A unique constraint on service and date means a second
worker run cannot replace a claim already made; it finds the existing row and
leaves it alone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.competitions import code_for_name
from app.core.logging import get_logger
from app.database.models import (
    DailySnapshot,
    ScanRun,
    SelectionAudit,
    ServiceSelection,
    SettledPrediction,
    StoredAnalysis,
)
from app.database.models.selections import LOST, PENDING, VOID, WON
from app.quant.grid import build_grid
from app.services.best_of_day import (
    MIN_SAMPLE,
    SERVICES,
    SERVICES_BY_KEY,
    BestOfDayEngine,
    ModelForecast,
    model_only_version,
    settles,
)
from app.services.capability_gate import CapabilityGate
from app.services.scan_telemetry import ScanTelemetry

logger = get_logger(__name__)

DEPTH = 10
"""How many ranked selections each service publishes.

One per service reads as a tip sheet and gives a user nothing to work with on a
busy Saturday. A ranked list lets them see where the strength drops off, which
is information a single pick hides — the tenth entry is genuinely weaker than
the first, and showing both is more honest than showing only the top.
"""


WITHHELD: frozenset[str] = frozenset({"under_25", "no_btts"})
"""Services validation found overconfident, excluded from publication.

``Best Under 2.5`` claimed 64.1% and delivered 57.7%; ``Best No BTTS`` claimed
63.1% and delivered 52.3%, both over more than a thousand selections. Those are
not sampling accidents, and shipping them would be selling a number we have
already measured as wrong.
"""

UNDER_OBSERVATION: frozenset[str] = frozenset({"draw", "away_draw"})
"""Services with too little evidence to describe as validated.

``Best Draw`` almost never qualifies; ``Best Away or Draw`` runs
underconfident. Both publish, neither is marketed as proven.
"""


def published_services() -> tuple[str, ...]:
    """Return the services permitted to publish, in display order."""
    return tuple(s.key for s in SERVICES if s.key not in WITHHELD)


@dataclass
class PublishReport:
    """What one publication run did."""

    selection_date: date | None = None
    analysed: int = 0
    forecasts: int = 0
    published: int = 0
    already_present: int = 0
    services_qualified: int = 0
    withheld: int = 0

    def summary(self) -> str:
        """Return a one-line human summary."""
        return (
            f"{self.forecasts} forecasts from {self.analysed} analyses, "
            f"{self.services_qualified} services qualified, "
            f"{self.published} published, {self.already_present} already present"
        )


@dataclass
class ServiceRecord:
    """One service's live performance."""

    key: str
    label: str
    total: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0
    pending: int = 0
    expected: float = 0.0
    status: str = "observed"

    @property
    def settled(self) -> int:
        """Selections with a known outcome."""
        return self.won + self.lost

    @property
    def actual_rate(self) -> float | None:
        """Share of settled selections that won."""
        if not self.settled:
            return None
        return self.won / self.settled

    @property
    def expected_rate(self) -> float | None:
        """What the published probabilities implied."""
        if not self.settled:
            return None
        return self.expected / self.settled

    @property
    def gap(self) -> float | None:
        """Actual minus expected."""
        actual = self.actual_rate
        expected = self.expected_rate
        if actual is None or expected is None:
            return None
        return actual - expected

    @property
    def meaningful(self) -> bool:
        """Whether the sample is large enough to quote a rate.

        Thirty is low for a strong claim and high enough that a single result
        cannot swing the figure by ten points.
        """
        return self.settled >= 30


@dataclass
class DayView:
    """One historical day, read from what was stored."""

    day: date
    selections: list[ServiceSelection] = field(default_factory=list)
    snapshot: DailySnapshot | None = None

    @property
    def settled(self) -> int:
        """How many selections have results."""
        return sum(1 for s in self.selections if s.is_settled)

    @property
    def won(self) -> int:
        """How many won."""
        return sum(1 for s in self.selections if s.status == WON)


class SelectionService:
    """Publishes, settles and reads service selections."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._engine = BestOfDayEngine()
        self.last_results_error: str | None = None
        """The vendor's message from the most recent failed results fetch.

        Surfaced so an operator sees why settlement could not complete rather
        than only that it did not.
        """

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish(self, now: datetime | None = None, top: int = DEPTH) -> PublishReport:
        """Rank today's fixtures and store the qualifying selections.

        Only fixtures that have not kicked off are eligible, so a published
        claim can never have been made with the result in view.
        """
        moment = now or datetime.now(UTC)
        report = PublishReport(selection_date=moment.date())

        # Today only. A card that quietly includes tomorrow's fixtures makes a
        # daily service dishonest: a user reading "Best Home Win today" and
        # finding a match that kicks off in thirty hours has been misled, even
        # though the probability was sound.
        end_of_day = datetime.combine(moment.date(), time(23, 59, 59), tzinfo=UTC)
        rows = await self._session.execute(
            select(StoredAnalysis)
            .where(
                StoredAnalysis.kickoff > moment,
                StoredAnalysis.kickoff <= end_of_day,
            )
            .order_by(StoredAnalysis.kickoff)
        )
        analyses = list(rows.scalars().all())
        report.analysed = len(analyses)

        forecasts = [f for f in (_forecast(a) for a in analyses) if f is not None]
        report.forecasts = len(forecasts)

        coverage: dict[str, int] = {}
        published_by_fixture: dict[str, int] = {}
        gate = CapabilityGate(self._session)
        version = model_only_version()
        blocked_by_capability = 0
        for analysis in analyses:
            coverage[analysis.coverage] = coverage.get(analysis.coverage, 0) + 1

        if forecasts:
            ranked = self._engine.rank(forecasts, limit=top)
            by_id = {a.provider_event_id: a for a in analyses}

            for key, selections in ranked.items():
                if key in WITHHELD:
                    report.withheld += len(selections)
                    continue
                if selections:
                    report.services_qualified += 1
                for rank, selection in enumerate(selections, start=1):
                    found = by_id.get(selection.forecast.fixture_id)
                    if found is None:
                        continue

                    # Wave 1 competitions publish only where a validated
                    # capability says they may. Established competitions are
                    # not gated, so this cannot stop what already worked.
                    decision = await gate.may_publish(
                        code_for_name(getattr(found, "competition", None)),
                        key,
                        version,
                    )
                    if not decision:
                        blocked_by_capability += 1
                        continue
                    created = await self._store(selection, found, rank, moment)
                    published_by_fixture[selection.forecast.fixture_id] = (
                        published_by_fixture.get(selection.forecast.fixture_id, 0) + 1
                    )
                    if created:
                        report.published += 1
                    else:
                        report.already_present += 1

        # Close the funnel. Qualification is decided here, not during the scan,
        # so without this the last stage reads zero however many fixtures
        # published — which is exactly what the first telemetry run showed.
        await self._attach_telemetry(published_by_fixture, moment)

        if blocked_by_capability:
            logger.info(
                "selections.capability_blocked",
                blocked=blocked_by_capability,
                model_version=version,
            )

        await self._snapshot(moment, report, coverage)
        await self._session.flush()
        logger.info("selections.published", summary=report.summary())
        return report

    async def _attach_telemetry(
        self, published_by_fixture: dict[str, int], moment: datetime
    ) -> None:
        """Record which fixtures qualified against the most recent scan.

        Observability only, and deliberately tolerant: if no scan run is found,
        or the write fails, publication proceeds unchanged. Telemetry describes
        the product; it must never be able to stop it.
        """
        try:
            start = datetime.combine(moment.date(), time.min, tzinfo=UTC)
            rows = await self._session.execute(
                select(ScanRun)
                .where(ScanRun.started_at >= start)
                .order_by(ScanRun.started_at.desc())
                .limit(1)
            )
            run = rows.scalars().first()
            if run is None:
                return
            await ScanTelemetry(self._session).attach_publication(
                published_by_fixture, run_id=run.id
            )
        except SQLAlchemyError as error:
            logger.warning("selections.telemetry_attach_failed", error=str(error))

    async def _store(
        self,
        selection: object,
        analysis: StoredAnalysis,
        rank: int,
        moment: datetime,
    ) -> bool:
        """Write one selection, returning whether it was new."""
        service = selection.service  # type: ignore[attr-defined]
        existing = await self._session.execute(
            select(ServiceSelection).where(
                ServiceSelection.service_key == service.key,
                ServiceSelection.selection_date == moment.date(),
                ServiceSelection.rank == rank,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return False

        record = ServiceSelection(
            service_key=service.key,
            service_label=service.label,
            selection_date=moment.date(),
            rank=rank,
            provider_event_id=analysis.provider_event_id,
            home_name=analysis.home_name,
            away_name=analysis.away_name,
            competition=analysis.competition,
            country=analysis.country,
            kickoff=analysis.kickoff,
            market=service.market,
            outcome=service.outcome,
            probability=selection.probability,  # type: ignore[attr-defined]
            score=selection.score,  # type: ignore[attr-defined]
            coverage=analysis.coverage,
            model_version=model_only_version(),
            components_used=list(analysis.components_used or []),
            factors=dict(selection.factors),  # type: ignore[attr-defined]
            rationale=selection.reason,  # type: ignore[attr-defined]
            sample_size=selection.forecast.sample,  # type: ignore[attr-defined]
            published_at=moment,
            status=PENDING,
        )
        self._session.add(record)
        await self._session.flush()

        self._session.add(
            SelectionAudit(
                selection_id=record.id,
                event="published",
                detail=f"{service.label}: {record.home_name} v {record.away_name}",
                occurred_at=moment,
            )
        )
        return True

    async def _snapshot(
        self, moment: datetime, report: PublishReport, coverage: dict[str, int]
    ) -> None:
        """Record the shape of the day, whether or not anything qualified.

        Without this, a day with no selections is indistinguishable from a day
        the worker never ran.
        """
        existing = await self._session.execute(
            select(DailySnapshot).where(DailySnapshot.snapshot_date == moment.date())
        )
        snapshot = existing.scalar_one_or_none()
        if snapshot is None:
            snapshot = DailySnapshot(snapshot_date=moment.date(), generated_at=moment)
            self._session.add(snapshot)

        snapshot.fixtures_available = report.analysed
        snapshot.fixtures_modelled = report.forecasts
        snapshot.services_run = len(published_services())
        snapshot.services_qualified = report.services_qualified
        snapshot.coverage = coverage
        snapshot.model_version = model_only_version()
        snapshot.generated_at = moment

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    async def settle(self, now: datetime | None = None, source: object | None = None) -> int:
        """Fill in results for selections whose matches have finished.

        Results come from the settlement table first, so a selection cannot be
        settled from a source that disagrees with the rest of the record.

        Where no settlement row exists, and a results source is supplied, the
        provider is asked directly. A published selection carries its own
        fixture id and does not depend on the analysis that produced it — and
        that analysis is transient, so a selection that outlived it must still
        be scoreable. Otherwise a published claim could sit unresolved forever
        through nobody's fault but our own retention policy.
        """
        moment = now or datetime.now(UTC)
        rows = await self._session.execute(
            select(ServiceSelection).where(
                ServiceSelection.status == PENDING,
                ServiceSelection.kickoff < moment - timedelta(hours=2),
            )
        )
        pending = list(rows.scalars().all())
        if not pending:
            return 0

        results = await self._session.execute(
            select(SettledPrediction).where(
                SettledPrediction.provider_event_id.in_([s.provider_event_id for s in pending])
            )
        )
        scores = {
            row.provider_event_id: (row.home_goals, row.away_goals)
            for row in results.scalars().all()
        }

        # Anything the settlement table cannot answer is asked of the provider
        # directly, so a selection is never stranded by a missing analysis row.
        outstanding = [
            selection for selection in pending if selection.provider_event_id not in scores
        ]
        if outstanding and source is not None:
            # Fetched by date, not by fixture id. The free plan forbids the
            # ``ids`` parameter, which silently stranded every selection; a
            # date covers a whole card in one request and is permitted.
            days = sorted(
                {
                    (
                        selection.kickoff
                        if selection.kickoff.tzinfo
                        else selection.kickoff.replace(tzinfo=UTC)
                    ).date()
                    for selection in outstanding
                }
            )

            fetch = getattr(source, "get_results_for_dates", None)
            if fetch is None:
                fetch = getattr(source, "get_results", None)
                arguments: object = [s.provider_event_id for s in outstanding]
            else:
                arguments = days

            if fetch is not None:
                try:
                    for result in await fetch(arguments):
                        identifier = getattr(result, "provider_event_id", None)
                        home_goals = getattr(result, "home_goals", None)
                        away_goals = getattr(result, "away_goals", None)
                        if identifier and home_goals is not None and away_goals is not None:
                            scores[str(identifier)] = (
                                int(home_goals),
                                int(away_goals),
                            )
                except Exception as exc:  # noqa: BLE001 - a provider failure must not stop settlement
                    # The vendor's own message is logged, not just the
                    # exception type. "ProviderDataError" tells an operator
                    # nothing about whether the key is spent, the plan forbids
                    # the call, or the ids were malformed — and those need
                    # different responses.
                    logger.warning(
                        "selections.direct_results_failed",
                        error_type=type(exc).__name__,
                        detail=str(exc)[:300],
                        missing=len(outstanding),
                    )
                    self.last_results_error = str(exc)[:300]

        settled = 0
        for selection in pending:
            score = scores.get(selection.provider_event_id)
            if score is None:
                continue
            home_goals, away_goals = score
            won = settles(selection.service_key, home_goals, away_goals)

            selection.status = VOID if won is None else (WON if won else LOST)
            selection.home_goals = home_goals
            selection.away_goals = away_goals
            selection.settled_at = moment
            settled += 1

            self._session.add(
                SelectionAudit(
                    selection_id=selection.id,
                    event="settled",
                    detail=f"{home_goals}-{away_goals} -> {selection.status}",
                    occurred_at=moment,
                )
            )

        await self._session.flush()
        logger.info("selections.settled", count=settled)
        return settled

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def today(self, now: datetime | None = None) -> list[ServiceSelection]:
        """Return today's published selections, in service order."""
        moment = now or datetime.now(UTC)
        return await self.for_day(moment.date())

    async def for_day(self, day: date) -> list[ServiceSelection]:
        """Return what was published on a date, exactly as stored.

        Never recomputed: the point of history is that anyone can look clever
        about last week's football.
        """
        order = {key: index for index, key in enumerate(published_services())}
        rows = await self._session.execute(
            select(ServiceSelection).where(ServiceSelection.selection_date == day)
        )
        selections = [s for s in rows.scalars().all() if s.service_key not in WITHHELD]
        selections.sort(key=lambda s: (order.get(s.service_key, 99), s.rank))
        return selections

    async def for_service(
        self, service_key: str, day: date | None = None
    ) -> list[ServiceSelection]:
        """Return one service's ranked selections for a day."""
        target = day or datetime.now(UTC).date()
        if service_key in WITHHELD:
            return []
        rows = await self._session.execute(
            select(ServiceSelection)
            .where(
                ServiceSelection.service_key == service_key,
                ServiceSelection.selection_date == target,
            )
            .order_by(ServiceSelection.rank)
        )
        return list(rows.scalars().all())

    async def service_counts(self, day: date | None = None) -> dict[str, int]:
        """Return how many selections each service published on a day."""
        target = day or datetime.now(UTC).date()
        rows = await self._session.execute(
            select(ServiceSelection.service_key, func.count())
            .where(ServiceSelection.selection_date == target)
            .group_by(ServiceSelection.service_key)
        )
        return {key: int(count) for key, count in rows.all() if key not in WITHHELD}

    async def picks_for_fixture(
        self, fixture_id: str, day: date | None = None
    ) -> list[tuple[str, str, float]]:
        """Return the services that selected a fixture, strongest first."""
        target = day or datetime.now(UTC).date()
        rows = await self._session.execute(
            select(ServiceSelection)
            .where(
                ServiceSelection.provider_event_id == fixture_id,
                ServiceSelection.selection_date == target,
            )
            .order_by(ServiceSelection.probability.desc())
        )
        return [
            (row.service_label, row.outcome, row.probability)
            for row in rows.scalars().all()
            if row.service_key not in WITHHELD
        ]

    async def picks_by_fixture(
        self, day: date | None = None
    ) -> dict[str, list[tuple[str, str, float]]]:
        """Return every fixture's selections for a day, keyed by fixture.

        One query for the whole card, so listing today's football does not
        issue a database round trip per fixture.
        """
        target = day or datetime.now(UTC).date()
        rows = await self._session.execute(
            select(ServiceSelection)
            .where(ServiceSelection.selection_date == target)
            .order_by(ServiceSelection.probability.desc())
        )
        grouped: dict[str, list[tuple[str, str, float]]] = {}
        for row in rows.scalars().all():
            if row.service_key in WITHHELD:
                continue
            grouped.setdefault(row.provider_event_id, []).append(
                (row.service_label, row.outcome, row.probability)
            )
        return grouped

    async def day_view(self, day: date) -> DayView:
        """Return a full historical day, selections and card shape together."""
        selections = await self.for_day(day)
        snapshot = (
            await self._session.execute(
                select(DailySnapshot).where(DailySnapshot.snapshot_date == day)
            )
        ).scalar_one_or_none()
        return DayView(day=day, selections=selections, snapshot=snapshot)

    async def available_days(self, limit: int = 60) -> list[date]:
        """Return dates with published selections, newest first."""
        rows = await self._session.execute(
            select(ServiceSelection.selection_date)
            .distinct()
            .order_by(ServiceSelection.selection_date.desc())
            .limit(limit)
        )
        return [row[0] for row in rows.all()]

    async def selection(self, selection_id: int) -> ServiceSelection | None:
        """Return one published selection."""
        return await self._session.get(ServiceSelection, selection_id)

    async def service_history(self, service_key: str, limit: int = 20) -> list[ServiceSelection]:
        """Return one service's recent selections, newest first."""
        rows = await self._session.execute(
            select(ServiceSelection)
            .where(ServiceSelection.service_key == service_key)
            .order_by(ServiceSelection.selection_date.desc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def track_record(self, days: int | None = None) -> list[ServiceRecord]:
        """Return live performance for every published service."""
        statement = select(ServiceSelection)
        if days is not None:
            cutoff = (datetime.now(UTC) - timedelta(days=days)).date()
            statement = statement.where(ServiceSelection.selection_date >= cutoff)

        rows = list((await self._session.execute(statement)).scalars().all())
        records = {
            key: ServiceRecord(
                key=key,
                label=SERVICES_BY_KEY[key].label,
                status="observed" if key in UNDER_OBSERVATION else "live",
            )
            for key in published_services()
        }

        for row in rows:
            record = records.get(row.service_key)
            if record is None:
                continue
            record.total += 1
            if row.status == WON:
                record.won += 1
                record.expected += row.probability
            elif row.status == LOST:
                record.lost += 1
                record.expected += row.probability
            elif row.status == VOID:
                record.void += 1
            else:
                record.pending += 1

        return list(records.values())

    async def pending_count(self) -> int:
        """How many selections are awaiting a result."""
        result = await self._session.execute(
            select(func.count())
            .select_from(ServiceSelection)
            .where(ServiceSelection.status == PENDING)
        )
        return int(result.scalar_one())


def _forecast(analysis: StoredAnalysis) -> ModelForecast | None:
    """Rebuild the model-only forecast from a stored analysis.

    Returns ``None`` when the stored record lacks the model-only figures, which
    happens for fixtures that were never modelled. Nothing is invented to fill
    the gap.
    """
    model = analysis.model_probabilities or {}
    if not all(key in model for key in ("home", "draw", "away")):
        return None

    lambda_home = analysis.expected_home_goals
    lambda_away = analysis.expected_away_goals
    if not lambda_home or not lambda_away:
        return None

    try:
        result = {key: float(str(model[key])) for key in ("home", "draw", "away")}
    except (TypeError, ValueError):
        return None
    if sum(result.values()) <= 0:
        return None

    grid = _tilted(build_grid(float(lambda_home), float(lambda_away)), result)

    views: list[dict[str, Decimal]] = []
    for raw in analysis.component_views or []:
        if not isinstance(raw, dict):
            continue
        try:
            views.append({k: Decimal(str(v)) for k, v in raw.items()})
        except (TypeError, ValueError):
            continue

    home_stats = analysis.home_stats or {}
    away_stats = analysis.away_stats or {}
    return ModelForecast(
        fixture_id=analysis.provider_event_id,
        home_name=analysis.home_name,
        away_name=analysis.away_name,
        competition=analysis.competition,
        kickoff=analysis.kickoff,
        grid=grid,
        components=tuple(analysis.components_used or []),
        component_results=tuple(views),
        home_matches=_matches(home_stats),
        away_matches=_matches(away_stats),
        coverage=analysis.coverage,
    )


def _matches(stats: dict[str, object]) -> int:
    """Read a side's match count from stored statistics."""
    raw = stats.get("matches", 0) if isinstance(stats, dict) else 0
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return 0


def _tilted(
    grid: dict[tuple[int, int], Decimal], result: dict[str, float]
) -> dict[tuple[int, int], Decimal]:
    """Reweight a scoreline grid so its result matches the model estimate.

    Keeps Elo and form inside the distribution every market derives from,
    rather than letting Poisson alone decide the goals markets while the other
    components decide the result.
    """
    weights: dict[str, Decimal] = {}
    predicates: tuple[tuple[str, Callable[[int, int], bool]], ...] = (
        ("home", lambda h, a: h > a),
        ("draw", lambda h, a: h == a),
        ("away", lambda h, a: h < a),
    )
    for name, predicate in predicates:
        mass = sum((p for (h, a), p in grid.items() if predicate(h, a)), Decimal(0))
        weights[name] = Decimal(str(result[name])) / mass if mass > 0 else Decimal(0)

    tilted = {
        (h, a): p * (weights["home"] if h > a else weights["draw"] if h == a else weights["away"])
        for (h, a), p in grid.items()
    }
    total = sum(tilted.values())
    return {k: v / total for k, v in tilted.items()} if total > 0 else grid


__all__ = [
    "MIN_SAMPLE",
    "UNDER_OBSERVATION",
    "WITHHELD",
    "DayView",
    "PublishReport",
    "SelectionService",
    "ServiceRecord",
    "published_services",
]
