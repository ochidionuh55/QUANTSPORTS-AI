"""Public endpoints for the website.

**One source of truth.** The website reads the same stored analyses,
selections and records the Telegram bot reads. No probability is recomputed
here and no model runs in the frontend, so the two interfaces cannot disagree
about what QUANTSPORT said.

**Nothing is invented to fill a page.** An empty card returns empty counts and
the interface says so. A marketing figure that appears when the data does not
is the beginning of a product that lies quietly.

**Read-only and cacheable.** Every response here is derived from persisted
records, so the website can cache aggressively without risking a stale claim
about a live selection — selections are immutable once published.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.dependencies.common import SessionDep
from app.core.competitions import CSV_COMPETITIONS
from app.database.models import HistoricalMatch, ServiceSelection, StoredAnalysis, Team
from app.services.best_of_day import SERVICES, SERVICES_BY_KEY
from app.services.queries import (
    MARKET_FILTERS,
    MARKETS_BY_KEY,
    FixtureQuery,
    FixtureQueryService,
)
from app.services.selections import (
    UNDER_OBSERVATION,
    WITHHELD,
    SelectionService,
    published_services,
)

router = APIRouter(prefix="/api/v1", tags=["public"])


class PlatformSummary(BaseModel):
    """Headline figures for the homepage.

    Driven from the database rather than hard-coded, so the site cannot
    advertise a corpus that is no longer there.
    """

    matches: int = Field(description="Historical matches on record.")
    competitions: int = Field(description="Competitions covered.")
    teams: int = Field(description="Canonical teams resolved.")
    services: int = Field(description="Daily services published.")
    fixtures_today: int
    fixtures_modelled_today: int
    selections_today: int
    updated_at: datetime


class ServiceStatus(BaseModel):
    """One daily service and how far it has been validated."""

    key: str
    label: str
    market: str
    outcome: str
    status: str = Field(
        description="validated, observation or withheld.",
    )
    published: bool
    selections_today: int


class FixtureCard(BaseModel):
    """One fixture as the website lists it."""

    fixture_id: str
    home_name: str
    away_name: str
    competition: str | None
    kickoff: datetime
    coverage: str
    strongest_market: str | None = None
    strongest_probability: float | None = None
    services: list[str] = Field(default_factory=list)


class SelectionCard(BaseModel):
    """A published selection, exactly as it was stored."""

    id: int
    service_key: str
    service_label: str
    home_name: str
    away_name: str
    competition: str | None
    kickoff: datetime
    market: str
    outcome: str
    probability: float
    coverage: str
    status: str
    home_goals: int | None = None
    away_goals: int | None = None
    published_at: datetime
    model_version: str
    rationale: str


@router.get("/summary", response_model=PlatformSummary)
async def summary(session: SessionDep) -> PlatformSummary:
    """Return the headline figures shown on the homepage."""
    moment = datetime.now(UTC)

    matches = int(
        (await session.execute(select(func.count()).select_from(HistoricalMatch))).scalar_one()
    )
    teams = int((await session.execute(select(func.count()).select_from(Team))).scalar_one())

    upcoming = list(
        (await session.execute(select(StoredAnalysis).where(StoredAnalysis.kickoff > moment)))
        .scalars()
        .all()
    )
    modelled = sum(1 for record in upcoming if (record.model_probabilities or {}).get("home"))

    selections = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ServiceSelection)
                .where(ServiceSelection.selection_date == moment.date())
            )
        ).scalar_one()
    )

    return PlatformSummary(
        matches=matches,
        competitions=len(CSV_COMPETITIONS),
        teams=teams,
        services=len(published_services()),
        fixtures_today=len(upcoming),
        fixtures_modelled_today=modelled,
        selections_today=selections,
        updated_at=moment,
    )


@router.get("/services", response_model=list[ServiceStatus])
async def services(session: SessionDep) -> list[ServiceStatus]:
    """Return every service and its validation status.

    Withheld services are listed rather than hidden. A reader is better served
    knowing we built a service and stopped publishing it than wondering why the
    numbers do not add up.
    """
    counts = await SelectionService(session).service_counts()

    statuses: list[ServiceStatus] = []
    for service in SERVICES:
        if service.key in WITHHELD:
            status = "withheld"
        elif service.key in UNDER_OBSERVATION:
            status = "observation"
        else:
            status = "validated"

        statuses.append(
            ServiceStatus(
                key=service.key,
                label=service.label,
                market=service.market,
                outcome=service.outcome,
                status=status,
                published=service.key not in WITHHELD,
                selections_today=counts.get(service.key, 0),
            )
        )
    return statuses


@router.get("/today", response_model=list[FixtureCard])
async def today(
    session: SessionDep, limit: int = Query(default=40, ge=1, le=200)
) -> list[FixtureCard]:
    """Return today's upcoming fixtures with any services that chose them."""
    moment = datetime.now(UTC)
    records = list(
        (
            await session.execute(
                select(StoredAnalysis)
                .where(StoredAnalysis.kickoff > moment)
                .order_by(StoredAnalysis.kickoff)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    picks = await SelectionService(session).picks_by_fixture()

    cards: list[FixtureCard] = []
    for record in records:
        chosen = picks.get(record.provider_event_id, [])
        strongest = chosen[0] if chosen else None
        cards.append(
            FixtureCard(
                fixture_id=record.provider_event_id,
                home_name=record.home_name,
                away_name=record.away_name,
                competition=record.competition,
                kickoff=record.kickoff,
                coverage=record.coverage,
                strongest_market=strongest[1] if strongest else None,
                strongest_probability=strongest[2] if strongest else None,
                services=[label for label, _, _ in chosen],
            )
        )
    return cards


@router.get("/best-of-today", response_model=list[SelectionCard])
async def best_of_today(session: SessionDep) -> list[SelectionCard]:
    """Return today's published selections."""
    selections = await SelectionService(session).today()
    return [_as_card(selection) for selection in selections]


@router.get("/services/{service_key}", response_model=list[SelectionCard])
async def service_selections(service_key: str, session: SessionDep) -> list[SelectionCard]:
    """Return one service's ranked selections for today."""
    if service_key not in SERVICES_BY_KEY:
        raise HTTPException(status_code=404, detail="Unknown service.")
    selections = await SelectionService(session).for_service(service_key)
    return [_as_card(selection) for selection in selections]


@router.get("/history/{day}", response_model=list[SelectionCard])
async def history(day: date, session: SessionDep) -> list[SelectionCard]:
    """Return exactly what was published on a date.

    Read from storage, never recomputed. Regenerating an old day with today's
    model would let the site look clever about football that has already been
    played.
    """
    selections = await SelectionService(session).for_day(day)
    return [_as_card(selection) for selection in selections]


@router.get("/history", response_model=list[date])
async def history_days(
    session: SessionDep, limit: int = Query(default=60, ge=1, le=365)
) -> list[date]:
    """Return dates with published selections, newest first."""
    return await SelectionService(session).available_days(limit=limit)


class MarketOption(BaseModel):
    """One market a reader can filter on."""

    key: str
    label: str
    market: str
    outcome: str
    available: int = Field(description="Fixtures on today's card reaching the probability floor.")


@router.get("/markets", response_model=list[MarketOption])
async def markets(
    session: SessionDep,
    min_probability: float = Query(default=0.5, ge=0.0, le=1.0),
) -> list[MarketOption]:
    """Return every filterable market with a live count.

    Counts come from the same query service the search endpoint uses, so a
    market that advertises twelve fixtures returns twelve when opened. A count
    computed by a different path is a promise the next screen may not keep.
    """
    service = FixtureQueryService(session)
    options: list[MarketOption] = []

    for definition in MARKET_FILTERS:
        found = await service.search(
            FixtureQuery(market=definition.key, min_probability=min_probability, limit=200)
        )
        options.append(
            MarketOption(
                key=definition.key,
                label=definition.label,
                market=definition.market,
                outcome=definition.outcome,
                available=len(found),
            )
        )
    return options


@router.get("/search", response_model=list[FixtureCard])
async def search(
    session: SessionDep,
    market: str | None = Query(default=None),
    competition: str | None = Query(default=None),
    min_probability: float = Query(default=0.0, ge=0.0, le=1.0),
    coverage: str | None = Query(default=None),
    limit: int = Query(default=60, ge=1, le=200),
) -> list[FixtureCard]:
    """Search today's modelled fixtures.

    Returns an empty list rather than an error when nothing matches: a search
    that finds nothing is a real answer about the card, not a failure.
    """
    if market is not None and market not in MARKETS_BY_KEY:
        raise HTTPException(status_code=404, detail="Unknown market.")

    query = FixtureQuery(
        competitions=(competition,) if competition else (),
        market=market,
        min_probability=min_probability,
        min_coverage=coverage,
        limit=limit,
    )
    records = await FixtureQueryService(session).search(query)
    picks = await SelectionService(session).picks_by_fixture()

    cards: list[FixtureCard] = []
    for record in records:
        chosen = picks.get(record.provider_event_id, [])
        probability = None
        outcome = None

        if market is not None:
            definition = MARKETS_BY_KEY[market]
            raw = (record.markets or {}).get(definition.market, {})
            value = raw.get(definition.outcome) if isinstance(raw, dict) else None
            if value is not None:
                try:
                    probability = float(str(value))
                    outcome = definition.outcome
                except (TypeError, ValueError):
                    probability = None
        elif chosen:
            outcome = chosen[0][1]
            probability = chosen[0][2]

        cards.append(
            FixtureCard(
                fixture_id=record.provider_event_id,
                home_name=record.home_name,
                away_name=record.away_name,
                competition=record.competition,
                kickoff=record.kickoff,
                coverage=record.coverage,
                strongest_market=outcome,
                strongest_probability=probability,
                services=[label for label, _, _ in chosen],
            )
        )
    return cards


class ServiceRecordCard(BaseModel):
    """One service's live performance."""

    key: str
    label: str
    status: str
    total: int
    won: int
    lost: int
    pending: int
    actual_rate: float | None
    expected_rate: float | None
    gap: float | None
    meaningful: bool = Field(description="Whether the sample is large enough to quote a rate.")


@router.get("/track-record", response_model=list[ServiceRecordCard])
async def track_record(session: SessionDep) -> list[ServiceRecordCard]:
    """Return live performance per service.

    Rates are returned with a ``meaningful`` flag rather than suppressed, so
    the interface can show a sample honestly instead of the API deciding what a
    reader may see.
    """
    records = await SelectionService(session).track_record()
    return [
        ServiceRecordCard(
            key=record.key,
            label=record.label,
            status=record.status,
            total=record.total,
            won=record.won,
            lost=record.lost,
            pending=record.pending,
            actual_rate=record.actual_rate,
            expected_rate=record.expected_rate,
            gap=record.gap,
            meaningful=record.meaningful,
        )
        for record in records
    ]


def _as_card(selection: ServiceSelection) -> SelectionCard:
    """Convert a stored selection for transport."""
    return SelectionCard(
        id=selection.id,
        service_key=selection.service_key,
        service_label=selection.service_label,
        home_name=selection.home_name,
        away_name=selection.away_name,
        competition=selection.competition,
        kickoff=selection.kickoff,
        market=selection.market,
        outcome=selection.outcome,
        probability=selection.probability,
        coverage=selection.coverage,
        status=selection.status,
        home_goals=selection.home_goals,
        away_goals=selection.away_goals,
        published_at=selection.published_at,
        model_version=selection.model_version,
        rationale=selection.rationale,
    )
