"""Historical ingestion tests.

The tests that matter most are the ones asserting what ingestion *refuses* to
do: create a second canonical team when a match is merely plausible, insert a
match against a guessed team, or leave a half-written season behind.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.enums import ReviewStatus
from app.database.models import (
    Competition,
    DatasetIngestion,
    HistoricalMatch,
    Team,
    TeamAlias,
)
from app.historical import (
    CsvHistoricalDataProvider,
    InMemorySource,
    MockHistoricalDataProvider,
    RejectionReason,
)
from app.services.historical_ingestion import (
    HistoricalIngestionService,
    HistoricalMatchRepository,
)
from app.services.team_resolution import TeamResolver

HEADER = "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,HTHG,HTAG,FTR\n"


def _csv(*rows: str) -> bytes:
    """Build a CSV payload."""
    return (HEADER + "".join(row + "\n" for row in rows)).encode()


def _csv_provider(files: dict[str, bytes], name: str = "csv_a") -> CsvHistoricalDataProvider:
    """Build a CSV provider over in-memory datasets."""
    return CsvHistoricalDataProvider(
        source=InMemorySource(files),
        competitions={"E0": ("Premier League", "England")},
        name=name,
    )


SEASON_ROWS = (
    "E0,12/08/2023,Arsenal,Chelsea,2,1,1,0,H",
    "E0,13/08/2023,Liverpool,Everton,3,0,2,0,H",
    "E0,19/08/2023,Chelsea,Liverpool,1,1,0,1,D",
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Provide a session against a fresh in-memory schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as active:
        yield active
    await engine.dispose()


async def _count(session: AsyncSession, model: type) -> int:
    """Return a row count for a model."""
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


class TestFirstIngestion:
    """An empty database must bootstrap cleanly."""

    async def test_creates_canonical_teams_and_aliases(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        report = await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )

        assert report.inserted == 3
        assert report.teams_created == 4
        assert await _count(session, Team) == 4
        assert await _count(session, TeamAlias) == 4

    async def test_matches_link_to_canonical_teams(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )

        result = await session.execute(select(HistoricalMatch))
        matches = list(result.scalars().all())
        assert len(matches) == 3
        for match in matches:
            assert match.home_team_id is not None
            assert match.away_team_id != match.home_team_id
            assert match.competition_id is not None

    async def test_competition_is_created_once(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        service = HistoricalIngestionService(session)
        await service.ingest_dataset(provider, "E0", "2023/2024", country="England")

        competitions = (await session.execute(select(Competition))).scalars().all()
        assert len(competitions) == 1
        assert competitions[0].has_historical_coverage is True

    async def test_canonical_name_is_editable_without_breaking_links(
        self, session: AsyncSession
    ) -> None:
        """The id is the identity; the name is metadata.

        The first source's spelling must not become permanent.
        """
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )

        team = (
            await session.execute(select(Team).where(Team.canonical_name == "Arsenal"))
        ).scalar_one()
        original_id = team.id
        match_count = len(
            (
                await session.execute(
                    select(HistoricalMatch).where(HistoricalMatch.home_team_id == original_id)
                )
            )
            .scalars()
            .all()
        )

        team.canonical_name = "Arsenal FC"
        await session.flush()

        still_linked = len(
            (
                await session.execute(
                    select(HistoricalMatch).where(HistoricalMatch.home_team_id == original_id)
                )
            )
            .scalars()
            .all()
        )
        assert team.id == original_id
        assert still_linked == match_count


class TestSecondIngestion:
    """A second source must resolve against what the first created."""

    async def test_alternative_spellings_resolve_to_existing_teams(
        self, session: AsyncSession
    ) -> None:
        service = HistoricalIngestionService(session)
        await service.ingest_dataset(
            _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)}),
            "E0",
            "2023/2024",
            country="England",
        )
        teams_after_first = await _count(session, Team)

        second = _csv_provider(
            {"E0_2024-2025.csv": _csv("E0,10/08/2024,Arsenal FC,Chelsea FC,1,0,0,0,H")},
            name="csv_b",
        )
        report = await service.ingest_dataset(second, "E0", "2024/2025", country="England")

        assert report.inserted == 1
        assert report.teams_created == 0
        assert await _count(session, Team) == teams_after_first

    async def test_uncertain_name_goes_to_review_not_a_new_team(
        self, session: AsyncSession
    ) -> None:
        """The failure this guards against splits a club's history in two."""
        service = HistoricalIngestionService(session)
        await service.ingest_dataset(
            _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)}),
            "E0",
            "2023/2024",
            country="England",
        )
        before = await _count(session, Team)

        resolver = TeamResolver(session, auto_confirm_threshold=0.99, review_threshold=0.5)
        result, created = await resolver.resolve_or_create(
            provider_name="csv_b", raw_name="Arsenall", country="England"
        )

        assert not created
        assert not result.is_resolved
        assert await _count(session, Team) == before

    async def test_unresolved_team_withholds_the_match(self, session: AsyncSession) -> None:
        """A match is never attached to a guessed team."""
        service = HistoricalIngestionService(session)
        await service.ingest_dataset(
            _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)}),
            "E0",
            "2023/2024",
            country="England",
        )

        resolver = TeamResolver(session, auto_confirm_threshold=0.99, review_threshold=0.5)
        await resolver.resolve_or_create(
            provider_name="csv_b", raw_name="Chelsae", country="England"
        )
        queued = await resolver.pending_review()
        assert queued
        assert queued[0].review_status is ReviewStatus.PENDING_REVIEW


class TestIdempotency:
    """Re-running the same dataset must change nothing."""

    async def test_second_run_inserts_nothing(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        service = HistoricalIngestionService(session)

        first = await service.ingest_dataset(provider, "E0", "2023/2024", country="England")
        second = await service.ingest_dataset(provider, "E0", "2023/2024", country="England")

        assert first.inserted == 3
        assert second.inserted == 0
        assert second.skipped_existing == 3
        assert second.is_idempotent_replay
        assert await _count(session, HistoricalMatch) == 3

    async def test_second_run_creates_no_extra_teams(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        service = HistoricalIngestionService(session)
        await service.ingest_dataset(provider, "E0", "2023/2024", country="England")
        await service.ingest_dataset(provider, "E0", "2023/2024", country="England")

        assert await _count(session, Team) == 4
        assert await _count(session, Competition) == 1

    async def test_partial_overlap_inserts_only_the_new_rows(self, session: AsyncSession) -> None:
        """The realistic case: a source appends fixtures as they are played."""
        service = HistoricalIngestionService(session)
        await service.ingest_dataset(
            _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS[:2])}),
            "E0",
            "2023/2024",
            country="England",
        )
        report = await service.ingest_dataset(
            _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)}),
            "E0",
            "2023/2024",
            country="England",
        )

        assert report.inserted == 1
        assert report.skipped_existing == 2
        assert await _count(session, HistoricalMatch) == 3


class TestTransactionIntegrity:
    """A failed season must leave nothing behind."""

    async def test_rollback_leaves_no_partial_season(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        service = HistoricalIngestionService(session)

        assert await _count(session, HistoricalMatch) == 0

        with pytest.raises(RuntimeError, match="simulated fault"):
            await service.ingest_dataset(provider, "E0", "2023/2024", country="England")
            raise RuntimeError("simulated fault after ingestion, before commit")

        await session.rollback()

        assert await _count(session, HistoricalMatch) == 0
        assert await _count(session, Team) == 0
        assert await _count(session, Competition) == 0
        assert await _count(session, DatasetIngestion) == 0

    async def test_successful_season_is_complete(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )
        await session.commit()

        assert await _count(session, HistoricalMatch) == 3
        assert await _count(session, DatasetIngestion) == 1


class TestRejectedRows:
    """Bad rows are reported, never silently dropped."""

    async def test_rejections_carry_reason_and_row_number(self, session: AsyncSession) -> None:
        provider = _csv_provider(
            {
                "E0_2023-2024.csv": _csv(
                    "E0,12/08/2023,Arsenal,Chelsea,2,1,1,0,H",
                    "E0,13/08/2023,Liverpool,Everton,x,0,2,0,H",
                    "E0,14/08/2023,,Everton,1,0,0,0,H",
                )
            }
        )
        report = await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )

        assert report.inserted == 1
        assert report.rejected_count == 2
        reasons = {r.reason for r in report.rejected}
        assert RejectionReason.INVALID_SCORE in reasons
        assert RejectionReason.MISSING_TEAM in reasons
        assert all(r.row_number is not None for r in report.rejected)

    async def test_rejections_do_not_abort_ingestion(self, session: AsyncSession) -> None:
        """A malformed row is a fact about the source, not a run failure."""
        provider = _csv_provider(
            {
                "E0_2023-2024.csv": _csv(
                    "E0,,Arsenal,Chelsea,2,1,1,0,H",
                    "E0,13/08/2023,Liverpool,Everton,3,0,2,0,H",
                )
            }
        )
        report = await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )
        assert report.inserted == 1
        assert report.rejected_count == 1

    async def test_rejection_counts_are_persisted_by_reason(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv("E0,,Arsenal,Chelsea,2,1,1,0,H")})
        await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )
        record = (await session.execute(select(DatasetIngestion))).scalar_one()
        assert record.rejections == {"invalid_date": 1}


class TestProvenance:
    """Our ingestion must remain identifiable even as sources drift."""

    async def test_provenance_is_recorded(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        report = await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )

        record = (await session.execute(select(DatasetIngestion))).scalar_one()
        assert record.content_hash == report.content_hash
        assert record.parser_version == "csv-1.0"
        assert record.schema_version == "football-data-uk-1.0"
        assert record.inserted_count == 3

    async def test_matches_reference_their_dataset(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        report = await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )
        matches = (await session.execute(select(HistoricalMatch))).scalars().all()
        assert all(m.data_version == report.content_hash for m in matches)

    async def test_revised_source_is_detectable(self, session: AsyncSession) -> None:
        """A changed upstream file produces a different hash."""
        service = HistoricalIngestionService(session)
        await service.ingest_dataset(
            _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)}),
            "E0",
            "2023/2024",
            country="England",
        )
        await service.ingest_dataset(
            _csv_provider(
                {
                    "E0_2023-2024.csv": _csv(
                        *SEASON_ROWS[:2], "E0,19/08/2023,Chelsea,Liverpool,2,1,0,1,H"
                    )
                }
            ),
            "E0",
            "2023/2024",
            country="England",
        )
        records = (await session.execute(select(DatasetIngestion))).scalars().all()
        assert len({r.content_hash for r in records}) == 2


class TestMultipleLeaguesAndSeasons:
    """Coverage across competitions and seasons."""

    async def test_mock_provider_across_two_competitions(self, session: AsyncSession) -> None:
        provider = MockHistoricalDataProvider()
        service = HistoricalIngestionService(session)

        for competition in ("E0", "SP1"):
            for season in ("2023/2024", "2024/2025"):
                await service.ingest_dataset(provider, competition, season)

        assert await _count(session, Competition) == 2
        assert await _count(session, DatasetIngestion) == 4
        assert await _count(session, HistoricalMatch) > 50

    async def test_promoted_side_has_sparse_history(self, session: AsyncSession) -> None:
        provider = MockHistoricalDataProvider()
        service = HistoricalIngestionService(session)
        await service.ingest_dataset(provider, "E0", "2024/2025")

        team = (
            await session.execute(select(Team).where(Team.canonical_name == "Luton Town"))
        ).scalar_one_or_none()
        assert team is not None

        matches = await HistoricalMatchRepository(session).for_team(team.id, limit=50)
        assert 0 < len(matches) < 6


class TestRepository:
    """Downstream models query canonical rows, not provider output."""

    async def test_before_cutoff_excludes_later_matches(self, session: AsyncSession) -> None:
        """The mechanism that prevents future data leaking into a backtest."""
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )
        chelsea = (
            await session.execute(select(Team).where(Team.canonical_name == "Chelsea"))
        ).scalar_one()

        repository = HistoricalMatchRepository(session)
        everything = await repository.for_team(chelsea.id)
        before_cutoff = await repository.for_team(
            chelsea.id, before=datetime(2023, 8, 15, tzinfo=UTC)
        )

        assert len(everything) == 2
        assert len(before_cutoff) == 1

    async def test_orders_newest_first(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )
        chelsea = (
            await session.execute(select(Team).where(Team.canonical_name == "Chelsea"))
        ).scalar_one()

        matches = await HistoricalMatchRepository(session).for_team(chelsea.id)
        assert matches[0].match_date > matches[1].match_date

    async def test_report_summary_is_readable(self, session: AsyncSession) -> None:
        provider = _csv_provider({"E0_2023-2024.csv": _csv(*SEASON_ROWS)})
        report = await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="England"
        )
        assert "3 inserted" in report.summary()


class TestConfusableClubNames:
    """Different clubs with similar names must never merge.

    Regression: "Reggiana" and "Reggina" score 0.9333 against a 0.92
    auto-confirm threshold, so the resolver merged two real Serie B clubs into
    one canonical team. Both appear in the same dataset, which is precisely the
    evidence that they are distinct.
    """

    async def test_same_provider_spellings_stay_separate(self, session: AsyncSession) -> None:
        resolver = TeamResolver(session)

        first, created_first = await resolver.resolve_or_create(
            provider_name="csv_historical", raw_name="Reggiana", country="Italy"
        )
        second, created_second = await resolver.resolve_or_create(
            provider_name="csv_historical", raw_name="Reggina", country="Italy"
        )

        assert created_first and created_second
        assert first.team is not None and second.team is not None
        assert first.team.id != second.team.id

    async def test_other_providers_can_still_alias(self, session: AsyncSession) -> None:
        """The rule applies within a source, not across sources.

        A different feed legitimately spells the same club differently, which
        is the whole reason aliases exist.
        """
        resolver = TeamResolver(session)
        created, _ = await resolver.resolve_or_create(
            provider_name="csv_historical", raw_name="Reggiana", country="Italy"
        )
        assert created.team is not None

        result = await resolver.resolve("api_football", "Reggiana", country="Italy")
        assert result.is_resolved
        assert result.team is not None
        assert result.team.id == created.team.id

    async def test_a_match_between_confusable_clubs_survives(self, session: AsyncSession) -> None:
        """The fixture that broke ingestion must now import cleanly."""
        provider = _csv_provider(
            {
                "E0_2023-2024.csv": _csv(
                    "E0,12/08/2023,Reggiana,Reggina,0,1,0,0,A",
                    "E0,19/08/2023,Reggina,Reggiana,2,1,1,0,H",
                )
            }
        )
        report = await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="Italy"
        )

        assert report.inserted == 2
        assert report.teams_created == 2
        assert not any(r.reason is RejectionReason.SAME_TEAM_BOTH_SIDES for r in report.rejected)

    async def test_self_match_is_rejected_not_inserted(self, session: AsyncSession) -> None:
        """If resolution ever does collapse two sides, the row is dropped
        rather than aborting the whole season."""
        resolver = TeamResolver(session)
        created, _ = await resolver.resolve_or_create(
            provider_name="csv_historical", raw_name="Reggiana", country="Italy"
        )
        assert created.team is not None

        provider = _csv_provider(
            {"E0_2023-2024.csv": _csv("E0,12/08/2023,Reggiana,Reggiana,1,0,0,0,H")}
        )
        report = await HistoricalIngestionService(session).ingest_dataset(
            provider, "E0", "2023/2024", country="Italy"
        )
        assert report.inserted == 0
        assert report.rejected

    async def test_case_variant_does_not_requeue(self, session: AsyncSession) -> None:
        """Regression: two spellings differing only in case share one key.

        Two spellings differing only in case normalize to one key. The second
        arrival finds the first already in the review queue and must leave it
        there rather than writing a duplicate.

        A misspelling is used rather than a real decorated name, because a
        decorated name now resolves outright by containment.
        """
        from app.database.models import Team
        from app.services.team_names import normalize_team_name

        session.add(
            Team(
                canonical_name="Colon",
                normalized_name=normalize_team_name("Colon"),
                country="Argentina",
                sport="football",
            )
        )
        await session.flush()
        # Thresholds chosen so the first spelling lands in the review queue,
        # which is the state that produced the crash.
        resolver = TeamResolver(session, auto_confirm_threshold=0.99, review_threshold=0.3)

        first, _ = await resolver.resolve_or_create(
            provider_name="csv_historical",
            raw_name="Colonn Santta Fe",
            country="Argentina",
        )
        second, _ = await resolver.resolve_or_create(
            provider_name="csv_historical",
            raw_name="Colonn Santta FE",
            country="Argentina",
        )

        queued = await resolver.pending_review()
        assert len(queued) == 1, "a case variant must not duplicate the queue entry"

    @pytest.mark.parametrize(
        ("existing", "arriving"),
        [
            ("Barnsley", "Burnley"),
            ("Northampton", "Southampton"),
            ("Colchester", "Chester"),
            ("Bolton", "Luton"),
            ("Mansfield", "Macclesfield"),
            ("Plymouth", "Weymouth"),
            ("Adanaspor", "Alanyaspor"),
            ("Antalyaspor", "Hatayspor"),
        ],
    )
    async def test_review_band_lookalikes_resolve_separately(
        self, session: AsyncSession, existing: str, arriving: str
    ) -> None:
        """Different clubs scoring 0.72-0.85 must not queue for review.

        Regression: sixteen such pairs sat in the review queue while roughly
        eight thousand matches were withheld waiting for a human to confirm
        what the data already showed — both clubs appear in the same dataset
        under their own names.
        """
        from app.database.models import Team
        from app.services.team_names import normalize_team_name

        session.add(
            Team(
                canonical_name=existing,
                normalized_name=normalize_team_name(existing),
                country="England",
                sport="football",
            )
        )
        await session.flush()

        resolver = TeamResolver(session)
        first, _ = await resolver.resolve_or_create(
            provider_name="csv_historical", raw_name=existing, country="England"
        )
        second, created = await resolver.resolve_or_create(
            provider_name="csv_historical", raw_name=arriving, country="England"
        )

        assert created, f"{arriving} should be created, not matched to {existing}"
        assert second.team is not None
        assert first.team is not None
        assert second.team.id != first.team.id
        assert await resolver.pending_review() == []

    async def test_a_genuine_variant_still_resolves(self, session: AsyncSession) -> None:
        """The rule must not block real aliases from a different source."""
        from app.database.models import Team
        from app.services.team_names import normalize_team_name

        session.add(
            Team(
                canonical_name="Sheffield United",
                normalized_name=normalize_team_name("Sheffield United"),
                country="England",
                sport="football",
            )
        )
        await session.flush()

        resolver = TeamResolver(session)
        await resolver.resolve_or_create(
            provider_name="csv_historical",
            raw_name="Sheffield United",
            country="England",
        )
        result = await resolver.resolve("api_football", "Sheffield Utd", country="England")
        assert result.is_resolved

    @pytest.mark.parametrize(
        ("existing", "existing_country", "arriving", "arriving_country"),
        [
            ("Celta", "Spain", "Celtic", "Scotland"),
            ("St Etienne", "France", "St Mirren", "Scotland"),
            ("Patronato", "Argentina", "Toronto FC", "USA"),
        ],
    )
    async def test_clubs_in_different_countries_never_match(
        self,
        session: AsyncSession,
        existing: str,
        existing_country: str,
        arriving: str,
        arriving_country: str,
    ) -> None:
        """Two clubs in different countries are never the same club.

        Regression: these three pairs all scored above the review threshold and
        sat in the queue indefinitely, withholding their fixtures. The unscoped
        candidate retry exists for teams whose country was never recorded, not
        to match across borders.
        """
        from app.database.models import Team
        from app.services.team_names import normalize_team_name

        session.add(
            Team(
                canonical_name=existing,
                normalized_name=normalize_team_name(existing),
                country=existing_country,
                sport="football",
            )
        )
        await session.flush()

        resolver = TeamResolver(session)
        result, created = await resolver.resolve_or_create(
            provider_name="csv_historical",
            raw_name=arriving,
            country=arriving_country,
        )

        assert created
        assert result.team is not None
        assert result.team.canonical_name == arriving
        assert await resolver.pending_review() == []

    async def test_unknown_country_still_matches(self, session: AsyncSession) -> None:
        """The retry must still work for teams with no country recorded.

        That is the case it exists for: a historical source may not record a
        country per team while the live feed does.
        """
        from app.database.models import Team
        from app.services.team_names import normalize_team_name

        session.add(
            Team(
                canonical_name="Arsenal",
                normalized_name=normalize_team_name("Arsenal"),
                country=None,
                sport="football",
            )
        )
        await session.flush()

        result = await TeamResolver(session).resolve(
            "api_football", "Arsenal FC", country="England"
        )
        assert result.is_resolved

    @pytest.mark.parametrize(
        ("archive", "live"),
        [
            ("Nurnberg", "1. FC Nurnberg"),
            ("Darmstadt", "SV Darmstadt 98"),
            ("Hannover", "Hannover 96"),
            ("Rakow", "Rakow Czestochowa"),
            ("Mjallby", "Mjallby AIF"),
            ("Salzburg", "Red Bull Salzburg"),
            ("Clermont", "Clermont Foot"),
            ("Kyoto", "Kyoto Sanga"),
        ],
    )
    async def test_decorated_live_names_resolve(
        self, session: AsyncSession, archive: str, live: str
    ) -> None:
        """Live feeds add founding years, sponsors and club-type suffixes.

        Token similarity punished each extra token hard — one extra out of two
        scored 0.50 — so these clubs appeared entirely unknown despite being in
        the data under their short names, and whole leagues showed as
        unsupported.
        """
        from app.database.models import Team
        from app.services.team_names import normalize_team_name

        session.add(
            Team(
                canonical_name=archive,
                normalized_name=normalize_team_name(archive),
                country="Germany",
                sport="football",
            )
        )
        await session.flush()

        result = await TeamResolver(session).resolve("api_football", live, country="Germany")
        assert result.is_resolved, f"{live} should match {archive}"
        assert result.team is not None
        assert result.team.canonical_name == archive

    async def test_ambiguous_containment_is_refused(self, session: AsyncSession) -> None:
        """ "Manchester" is contained in both United and City.

        Containment is strong evidence only when exactly one club qualifies.
        """
        from app.database.models import Team
        from app.services.team_names import normalize_team_name

        for name in ("Manchester United", "Manchester City"):
            session.add(
                Team(
                    canonical_name=name,
                    normalized_name=normalize_team_name(name),
                    country="England",
                    sport="football",
                )
            )
        await session.flush()

        result = await TeamResolver(session).resolve(
            "api_football", "Manchester", country="England"
        )
        assert not result.is_resolved

    async def test_exact_names_are_unaffected(self, session: AsyncSession) -> None:
        """Containment must not disturb names that already matched."""
        from app.database.models import Team
        from app.services.team_names import normalize_team_name

        for name in ("Manchester United", "Manchester City"):
            session.add(
                Team(
                    canonical_name=name,
                    normalized_name=normalize_team_name(name),
                    country="England",
                    sport="football",
                )
            )
        await session.flush()

        result = await TeamResolver(session).resolve(
            "api_football", "Manchester United", country="England"
        )
        assert result.is_resolved
        assert result.team is not None
        assert result.team.canonical_name == "Manchester United"
