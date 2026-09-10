"""Historical data ingestion.

Turns a provider snapshot into canonical database rows::

    provider snapshot → resolve teams → validate → insert → commit season

**Transaction boundary.** One season, one transaction. A season is a few
hundred rows, so rollback is cheap and the resulting invariant is worth far
more than partial-progress machinery: *a season present in the database is
complete and internally consistent*. There is no half-ingested state to reason
about, and no resume logic to get wrong.

This is an implementation detail. Callers ask for ``ingest_dataset`` and are
not coupled to how it commits, so batching can be introduced later without
touching them.

**Rejected rows do not abort ingestion.** A malformed row is a fact about the
source, not a failure of the run. Rejections are counted, reported and
persisted by reason. Only a genuine fault — an unreadable dataset, a database
error — rolls the season back.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import Competition, DatasetIngestion, HistoricalMatch
from app.historical.base import HistoricalDataProvider
from app.historical.models import (
    HistoricalDatasetSnapshot,
    RejectedRecord,
    RejectionReason,
)
from app.historical.models import (
    HistoricalMatch as HistoricalMatchDTO,
)
from app.services.team_names import normalize_team_name
from app.services.team_resolution import TeamResolver

logger = get_logger(__name__)


@dataclass
class IngestionReport:
    """Outcome of one ingestion.

    Attributes:
        rejected: Every unusable source row, with its reason and row number.
        unresolved_teams: Names that could not be confidently identified. The
            matches involving them are skipped rather than attached to a
            guessed team.
    """

    provider_name: str
    competition_external_id: str
    season: str
    content_hash: str

    row_count: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    teams_created: int = 0
    aliases_created: int = 0

    rejected: list[RejectedRecord] = field(default_factory=list)
    unresolved_teams: set[str] = field(default_factory=set)

    @property
    def rejected_count(self) -> int:
        """Number of unusable source rows."""
        return len(self.rejected)

    @property
    def rejections_by_reason(self) -> dict[str, int]:
        """Rejection counts keyed by reason."""
        return dict(Counter(str(r.reason) for r in self.rejected))

    @property
    def is_idempotent_replay(self) -> bool:
        """True when the run inserted nothing because everything was present."""
        return self.inserted == 0 and self.skipped_existing > 0

    def summary(self) -> str:
        """Return a one-line human summary."""
        return (
            f"{self.competition_external_id} {self.season}: "
            f"{self.inserted} inserted, {self.skipped_existing} already present, "
            f"{self.rejected_count} rejected, "
            f"{len(self.unresolved_teams)} unresolved teams, "
            f"{self.teams_created} teams created"
        )


class HistoricalIngestionService:
    """Ingests provider snapshots into the canonical database."""

    def __init__(self, session: AsyncSession, sport: str = "football") -> None:
        self._session = session
        self._sport = sport

    async def ingest_dataset(
        self,
        provider: HistoricalDataProvider,
        competition_external_id: str,
        season: str,
        country: str | None = None,
    ) -> IngestionReport:
        """Ingest one competition-season.

        The caller owns the transaction. Wrapping this call in a single
        ``session.begin()`` is what delivers the whole-season guarantee; if it
        raises, nothing from the season persists.

        Args:
            provider: Source of the dataset.
            competition_external_id: Competition to ingest.
            season: Season label.
            country: Country for newly created competitions and teams.

        Returns:
            A report describing what happened, including rejected rows.

        Raises:
            HistoricalProviderError: If the dataset cannot be read.
        """
        snapshot = await provider.load_dataset(competition_external_id, season)
        return await self.ingest_snapshot(provider, snapshot, country=country)

    async def ingest_snapshot(
        self,
        provider: HistoricalDataProvider,
        snapshot: HistoricalDatasetSnapshot,
        country: str | None = None,
    ) -> IngestionReport:
        """Ingest an already-loaded snapshot."""
        meta = snapshot.metadata
        report = IngestionReport(
            provider_name=meta.provider_name,
            competition_external_id=meta.competition_external_id,
            season=meta.season,
            content_hash=meta.content_hash,
            row_count=meta.row_count,
            rejected=list(snapshot.rejected),
        )

        competition = await self._get_or_create_competition(
            provider, meta.competition_external_id, country
        )
        resolver = TeamResolver(self._session)
        existing = await self._existing_match_ids(meta.provider_name, snapshot.matches)

        for dto in snapshot.matches:
            if dto.external_id in existing:
                report.skipped_existing += 1
                continue

            home = await self._resolve(
                resolver,
                report,
                provider,
                dto.home_team.source_name,
                country,
                competition.id,
            )
            away = await self._resolve(
                resolver,
                report,
                provider,
                dto.away_team.source_name,
                country,
                competition.id,
            )
            if home is not None and home == away:
                # Both sides resolved to one team. Whatever the cause, the row
                # is unusable and the database constraint would reject the
                # whole season's transaction rather than this single match.
                report.rejected.append(
                    RejectedRecord(
                        reason=RejectionReason.SAME_TEAM_BOTH_SIDES,
                        detail=(
                            f"'{dto.home_team.source_name}' and "
                            f"'{dto.away_team.source_name}' both resolved to "
                            "the same canonical team. They are different clubs "
                            "with confusable names; the mapping needs review."
                        ),
                        raw={
                            "home": dto.home_team.source_name,
                            "away": dto.away_team.source_name,
                            "team_id": home,
                        },
                    )
                )
                continue

            if home is None or away is None:
                # Attaching a match to a guessed team would corrupt that team's
                # entire rating history, so the match waits for review instead.
                report.rejected.append(
                    RejectedRecord(
                        reason=RejectionReason.MISSING_TEAM,
                        detail=(
                            "One or both teams could not be confidently "
                            "resolved; match withheld pending review."
                        ),
                        raw={
                            "home": dto.home_team.source_name,
                            "away": dto.away_team.source_name,
                            "match_date": dto.match_date.isoformat(),
                        },
                    )
                )
                continue

            self._session.add(
                HistoricalMatch(
                    provider_name=meta.provider_name,
                    provider_match_id=dto.external_id,
                    competition_id=competition.id,
                    season=dto.season,
                    match_date=dto.kickoff
                    or datetime.combine(dto.match_date, datetime.min.time(), tzinfo=UTC),
                    home_team_id=home,
                    away_team_id=away,
                    home_goals=dto.home_goals,
                    away_goals=dto.away_goals,
                    half_time_home_goals=dto.half_time_home_goals,
                    half_time_away_goals=dto.half_time_away_goals,
                    data_version=meta.content_hash,
                    ingested_at=meta.ingested_at,
                )
            )
            report.inserted += 1

        await self._session.flush()
        await self._record_provenance(snapshot, report)

        logger.info(
            "historical.ingested",
            provider=meta.provider_name,
            competition=meta.competition_external_id,
            season=meta.season,
            inserted=report.inserted,
            skipped=report.skipped_existing,
            rejected=report.rejected_count,
            unresolved_teams=len(report.unresolved_teams),
            content_hash=meta.content_hash[:12],
        )
        return report

    async def _resolve(
        self,
        resolver: TeamResolver,
        report: IngestionReport,
        provider: HistoricalDataProvider,
        source_name: str,
        country: str | None,
        competition_id: int,
    ) -> int | None:
        """Resolve one source team name to a canonical id, or ``None``."""
        result, created = await resolver.resolve_or_create(
            provider_name=provider.name,
            raw_name=source_name,
            sport=self._sport,
            country=country,
            competition_id=competition_id,
        )
        if created:
            report.teams_created += 1
            report.aliases_created += 1
        if not result.is_resolved:
            report.unresolved_teams.add(source_name)
            return None
        return result.team.id if result.team else None

    async def _existing_match_ids(
        self, provider_name: str, matches: tuple[HistoricalMatchDTO, ...]
    ) -> set[str]:
        """Return which provider match ids are already stored.

        Checked up front in one query rather than per row: re-ingesting a
        season is the normal case, and a query per match would make the common
        path the slowest one.
        """
        if not matches:
            return set()
        ids = [m.external_id for m in matches]
        result = await self._session.execute(
            select(HistoricalMatch.provider_match_id).where(
                HistoricalMatch.provider_name == provider_name,
                HistoricalMatch.provider_match_id.in_(ids),
            )
        )
        return set(result.scalars().all())

    async def _get_or_create_competition(
        self,
        provider: HistoricalDataProvider,
        external_id: str,
        country: str | None,
    ) -> Competition:
        """Return the canonical competition, creating it if unknown."""
        competitions = await provider.get_competitions()
        source = next((c for c in competitions if c.external_id == external_id), None)
        name = source.name if source else external_id
        resolved_country = country or (source.country if source else None)
        normalized = normalize_team_name(name)

        result = await self._session.execute(
            select(Competition).where(
                Competition.sport == self._sport,
                Competition.normalized_name == normalized,
                Competition.country == resolved_country,
            )
        )
        competition = result.scalar_one_or_none()
        if competition is not None:
            competition.has_historical_coverage = True
            return competition

        competition = Competition(
            canonical_name=name,
            normalized_name=normalized,
            country=resolved_country,
            sport=self._sport,
            has_historical_coverage=True,
            external_metadata={"source_external_id": external_id},
        )
        self._session.add(competition)
        await self._session.flush()
        logger.info("competition.created", name=name, external_id=external_id)
        return competition

    async def _record_provenance(
        self, snapshot: HistoricalDatasetSnapshot, report: IngestionReport
    ) -> DatasetIngestion:
        """Persist provenance for this run."""
        meta = snapshot.metadata
        record = DatasetIngestion(
            provider_name=meta.provider_name,
            source_name=meta.source_name,
            source_url=meta.source_url,
            competition_external_id=meta.competition_external_id,
            season=meta.season,
            content_hash=meta.content_hash,
            parser_version=meta.parser_version,
            schema_version=meta.schema_version,
            row_count=meta.row_count,
            inserted_count=report.inserted,
            skipped_count=report.skipped_existing,
            rejected_count=report.rejected_count,
            unresolved_team_count=len(report.unresolved_teams),
            teams_created=report.teams_created,
            aliases_created=report.aliases_created,
            rejections=report.rejections_by_reason,
            downloaded_at=meta.downloaded_at,
            ingested_at=meta.ingested_at,
        )
        self._session.add(record)
        await self._session.flush()
        return record


class HistoricalMatchRepository:
    """Reads historical matches for downstream models.

    Exists so that Poisson, Elo and form code query canonical rows without
    knowing which provider or file the data came from. Swapping the historical
    source later must not change a single line of modelling code.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_team(
        self, team_id: int, limit: int = 10, before: datetime | None = None
    ) -> list[HistoricalMatch]:
        """Return a team's most recent matches, newest first.

        ``before`` exists for backtesting: passing a cutoff returns only what
        was knowable at that moment, which is the mechanism that prevents
        future information leaking into a historical prediction.
        """
        statement = select(HistoricalMatch).where(
            (HistoricalMatch.home_team_id == team_id) | (HistoricalMatch.away_team_id == team_id)
        )
        if before is not None:
            statement = statement.where(HistoricalMatch.match_date < before)
        statement = statement.order_by(HistoricalMatch.match_date.desc()).limit(limit)
        result = await self._session.execute(statement)
        return list(result.scalars().all())

    async def for_competition_season(
        self, competition_id: int, season: str
    ) -> list[HistoricalMatch]:
        """Return every match in a competition-season, oldest first."""
        result = await self._session.execute(
            select(HistoricalMatch)
            .where(
                HistoricalMatch.competition_id == competition_id,
                HistoricalMatch.season == season,
            )
            .order_by(HistoricalMatch.match_date)
        )
        return list(result.scalars().all())

    async def count(self) -> int:
        """Return the total number of stored historical matches."""
        result = await self._session.execute(select(HistoricalMatch.id))
        return len(result.scalars().all())
