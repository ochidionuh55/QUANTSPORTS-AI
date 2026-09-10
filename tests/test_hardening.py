"""Hardening tests: aliases, feedback and self-judgement.

The settlement tests are the ones that matter. A performance history that can
be gamed — by settling only good days, or double-counting, or silently dropping
fixtures — is worse than no history at all, because it produces confident wrong
conclusions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.enums import ReviewStatus
from app.database.models import (
    SettledPrediction,
    StoredAnalysis,
    Team,
    TeamAlias,
    User,
    UserFeedback,
)
from app.services.aliases import AliasReviewService, AliasSeeder
from app.services.settlement import (
    FinalScore,
    PerformanceService,
    SettlementService,
)
from app.services.team_names import normalize_team_name
from app.services.team_resolution import TeamResolver

NOW = datetime(2026, 3, 1, 20, 0, tzinfo=UTC)


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


async def _team(session: AsyncSession, name: str) -> Team:
    """Insert a canonical team."""
    team = Team(
        canonical_name=name,
        normalized_name=normalize_team_name(name),
        sport="football",
    )
    session.add(team)
    await session.flush()
    return team


async def _analysis(
    session: AsyncSession,
    fixture_id: str = "1",
    kickoff: datetime | None = None,
    home: float = 0.50,
    draw: float = 0.28,
    away: float = 0.22,
    with_market: bool = True,
    competition: str = "Championship",
) -> StoredAnalysis:
    """Insert a stored analysis ready for settlement."""
    record = StoredAnalysis(
        provider_name="api_football",
        provider_event_id=fixture_id,
        home_name="Alpha",
        away_name="Bravo",
        competition=competition,
        kickoff=kickoff or NOW - timedelta(hours=4),
        coverage="fully_modelled",
        markets={
            "1X2": {"Home": str(home), "Draw": str(draw), "Away": str(away)},
            "Goals": {"Over 2.5": "0.55", "Under 2.5": "0.45"},
            "Both teams to score": {"Yes": "0.52", "No": "0.48"},
        },
        expected_home_goals=1.6,
        expected_away_goals=1.1,
        market_probabilities=(
            {"home": "0.47", "draw": "0.27", "away": "0.26"} if with_market else None
        ),
        home_stats={},
        away_stats={},
        components_used=["poisson", "elo", "form"],
        components_dropped=[],
        computed_at=NOW - timedelta(hours=8),
    )
    session.add(record)
    await session.flush()
    return record


class FakeResults:
    """Returns fixed final scores."""

    def __init__(self, scores: dict[str, tuple[int, int]]) -> None:
        self._scores = scores
        self.requested: list[str] = []

    async def get_results(self, fixture_ids: list[str]) -> list[FinalScore]:
        """Return scores for known fixtures only."""
        self.requested.extend(fixture_ids)
        return [
            FinalScore(fixture_id, home, away)
            for fixture_id in fixture_ids
            if (pair := self._scores.get(fixture_id))
            for home, away in [pair]
        ]


class TestAliasSeeding:
    """Curated aliases fix known spelling differences safely."""

    async def test_creates_alias_for_known_difference(self, session: AsyncSession) -> None:
        await _team(session, "Stoke")
        counts = await AliasSeeder(session).seed({"Stoke City": "Stoke"})

        assert counts["created"] == 1
        alias = (await session.execute(select(TeamAlias))).scalar_one()
        assert alias.review_status is ReviewStatus.MANUALLY_CONFIRMED
        assert alias.is_confirmed

    async def test_seeded_alias_resolves(self, session: AsyncSession) -> None:
        """The point of the exercise: the fixture becomes analysable."""
        team = await _team(session, "Stoke")
        await AliasSeeder(session).seed({"Stoke City": "Stoke"})

        result = await TeamResolver(session).resolve("api_football", "Stoke City")
        assert result.is_resolved
        assert result.team is not None
        assert result.team.id == team.id

    async def test_never_creates_a_team(self, session: AsyncSession) -> None:
        """An alias pointing at nothing would invent identity."""
        counts = await AliasSeeder(session).seed({"Some Club": "Nonexistent"})

        assert counts["created"] == 0
        assert counts["skipped"] == 1
        assert (
            int((await session.execute(select(func.count()).select_from(Team))).scalar_one()) == 0
        )

    async def test_seeding_twice_is_idempotent(self, session: AsyncSession) -> None:
        await _team(session, "Stoke")
        seeder = AliasSeeder(session)
        await seeder.seed({"Stoke City": "Stoke"})
        counts = await seeder.seed({"Stoke City": "Stoke"})

        assert counts["created"] == 0
        assert counts["existing"] == 1

    async def test_spellings_that_normalize_alike_do_not_collide(
        self, session: AsyncSession
    ) -> None:
        """Regression: a unique-constraint violation aborted the whole seed.

        "Paris Saint-Germain" and "Paris Saint Germain" normalize identically,
        and the database check could not see the earlier one because neither
        had been flushed yet.
        """
        await _team(session, "Paris SG")
        counts = await AliasSeeder(session).seed(
            {
                "Paris Saint-Germain": "Paris SG",
                "Paris Saint Germain": "Paris SG",
            }
        )
        assert counts["created"] == 1
        assert counts["existing"] == 1

    async def test_curated_list_has_no_normalization_collisions(self) -> None:
        """The seed list itself must not contain a latent collision."""
        from app.services.aliases import KNOWN_ALIASES

        seen: dict[str, str] = {}
        collisions: list[str] = []
        for feed in KNOWN_ALIASES:
            key = normalize_team_name(feed)
            if key in seen:
                collisions.append(f"{feed} collides with {seen[key]}")
            seen[key] = feed
        assert collisions == [], collisions

    async def test_real_seed_list_is_sane(self) -> None:
        """Every entry must be a plausible identity match, not a near-name."""
        from app.services.aliases import KNOWN_ALIASES

        assert len(KNOWN_ALIASES) > 50
        for feed, canonical in KNOWN_ALIASES.items():
            assert feed and canonical
            assert feed != "" and canonical != ""


class TestAdminReview:
    """An administrator can resolve names without touching source code."""

    async def test_link_creates_confirmed_alias(self, session: AsyncSession) -> None:
        team = await _team(session, "Stoke")
        alias = await AliasReviewService(session).link("Stoke City", team.id)

        assert alias.review_status is ReviewStatus.MANUALLY_CONFIRMED
        assert alias.team_id == team.id

    async def test_link_rejects_unknown_team(self, session: AsyncSession) -> None:
        with pytest.raises(LookupError):
            await AliasReviewService(session).link("Whoever", 999_999)

    async def test_link_overrides_a_pending_suggestion(self, session: AsyncSession) -> None:
        """The reviewer may disagree with the machine's candidate."""
        wrong = await _team(session, "Stockport")
        right = await _team(session, "Stoke")
        service = AliasReviewService(session)

        session.add(
            TeamAlias(
                team_id=wrong.id,
                provider_name="api_football",
                alias="Stoke City",
                normalized_alias=normalize_team_name("Stoke City"),
                review_status=ReviewStatus.PENDING_REVIEW,
            )
        )
        await session.flush()

        alias = await service.link("Stoke City", right.id)
        assert alias.team_id == right.id
        assert alias.review_status is ReviewStatus.MANUALLY_CONFIRMED

    async def test_pending_queue_lists_uncertain_names(self, session: AsyncSession) -> None:
        team = await _team(session, "Stoke")
        session.add(
            TeamAlias(
                team_id=team.id,
                provider_name="api_football",
                alias="Stoke Citty",
                normalized_alias=normalize_team_name("Stoke Citty"),
                review_status=ReviewStatus.PENDING_REVIEW,
            )
        )
        await session.flush()

        assert len(await AliasReviewService(session).pending()) == 1

    async def test_rejected_alias_stays_unusable(self, session: AsyncSession) -> None:
        team = await _team(session, "Stoke")
        alias = await AliasReviewService(session).link("Stoke City", team.id)
        await AliasReviewService(session).reject(alias.id)

        result = await TeamResolver(session).resolve("api_football", "Stoke City")
        assert not result.is_resolved


class TestFeedbackPersistence:
    """Feedback belongs in the database, not the log."""

    async def test_feedback_is_stored(self, session: AsyncSession) -> None:
        user = User(telegram_id=1, credits=0)
        session.add(user)
        await session.flush()

        session.add(UserFeedback(user_id=user.id, provider_event_id="42", verdict="up"))
        await session.flush()

        stored = (await session.execute(select(UserFeedback))).scalar_one()
        assert stored.verdict == "up"
        assert stored.provider_event_id == "42"
        assert stored.created_at is not None

    async def test_feedback_is_append_only(self, session: AsyncSession) -> None:
        """A changed mind adds a row; the earlier opinion survives."""
        user = User(telegram_id=1, credits=0)
        session.add(user)
        await session.flush()

        for verdict in ("up", "down"):
            session.add(UserFeedback(user_id=user.id, provider_event_id="42", verdict=verdict))
        await session.flush()

        rows = (await session.execute(select(UserFeedback))).scalars().all()
        assert {r.verdict for r in rows} == {"up", "down"}


class TestSettlement:
    """Every published analysis is compared against what happened."""

    async def test_settles_a_finished_fixture(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        report = await SettlementService(session).settle(FakeResults({"1": (2, 1)}), now=NOW)

        assert report.settled == 1
        row = (await session.execute(select(SettledPrediction))).scalar_one()
        assert row.home_goals == 2
        assert row.actual_result == "home"
        assert row.predicted_favourite == "home"
        assert row.favourite_won is True

    async def test_records_derived_market_outcomes(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        await SettlementService(session).settle(FakeResults({"1": (2, 1)}), now=NOW)

        row = (await session.execute(select(SettledPrediction))).scalar_one()
        assert row.over_2_5_hit is True
        assert row.btts_hit is True

    async def test_records_a_loss_as_readily_as_a_win(self, session: AsyncSession) -> None:
        """No cherry-picking: bad days are recorded identically."""
        await _analysis(session, "1")
        await SettlementService(session).settle(FakeResults({"1": (0, 3)}), now=NOW)

        row = (await session.execute(select(SettledPrediction))).scalar_one()
        assert row.actual_result == "away"
        assert row.favourite_won is False

    async def test_market_baseline_is_stored(self, session: AsyncSession) -> None:
        """Without it, a Brier score cannot be interpreted."""
        await _analysis(session, "1")
        await SettlementService(session).settle(FakeResults({"1": (1, 1)}), now=NOW)

        row = (await session.execute(select(SettledPrediction))).scalar_one()
        assert row.market_home == pytest.approx(0.47)

    async def test_model_version_is_recorded(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        await SettlementService(session).settle(FakeResults({"1": (1, 1)}), now=NOW)

        row = (await session.execute(select(SettledPrediction))).scalar_one()
        assert row.model_version

    async def test_settling_twice_does_not_duplicate(self, session: AsyncSession) -> None:
        """Double-counting would silently distort every statistic."""
        await _analysis(session, "1")
        service = SettlementService(session)
        source = FakeResults({"1": (2, 1)})

        await service.settle(source, now=NOW)
        second = await service.settle(source, now=NOW)

        assert second.already_settled == 1
        assert second.settled == 0
        count = int(
            (
                await session.execute(select(func.count()).select_from(SettledPrediction))
            ).scalar_one()
        )
        assert count == 1

    async def test_unfinished_fixtures_are_not_settled(self, session: AsyncSession) -> None:
        """Settling mid-match would record a false result permanently."""
        await _analysis(session, "1", kickoff=NOW - timedelta(minutes=30))
        report = await SettlementService(session).settle(FakeResults({"1": (1, 0)}), now=NOW)
        assert report.candidates == 0
        assert report.settled == 0

    async def test_missing_result_is_reported_not_invented(self, session: AsyncSession) -> None:
        await _analysis(session, "1")
        report = await SettlementService(session).settle(FakeResults({}), now=NOW)

        assert report.unavailable == 1
        assert report.settled == 0

    async def test_unmodellable_fixtures_are_skipped(self, session: AsyncSession) -> None:
        """A fixture with no forecast has nothing to score."""
        session.add(
            StoredAnalysis(
                provider_name="api_football",
                provider_event_id="9",
                home_name="A",
                away_name="B",
                kickoff=NOW - timedelta(hours=4),
                coverage="unsupported",
                markets={},
                home_stats={},
                away_stats={},
                components_used=[],
                components_dropped=[],
                computed_at=NOW,
            )
        )
        await session.flush()

        report = await SettlementService(session).settle(FakeResults({"9": (1, 0)}), now=NOW)
        assert report.candidates == 0

    async def test_results_are_requested_in_one_batch(self, session: AsyncSession) -> None:
        """One request per fixture would exhaust the daily allowance."""
        for index in range(5):
            await _analysis(session, str(index))
        source = FakeResults({str(i): (1, 0) for i in range(5)})

        await SettlementService(session).settle(source, now=NOW)
        assert len(source.requested) == 5


class TestPerformanceTracking:
    """The system judges itself over time."""

    async def _settle_many(self, session: AsyncSession, results: list[tuple[int, int]]) -> None:
        """Settle a batch of fixtures with given scores."""
        scores = {}
        for index, score in enumerate(results):
            await _analysis(session, str(index))
            scores[str(index)] = score
        await SettlementService(session).settle(FakeResults(scores), now=NOW)

    async def test_reports_sample_and_accuracy(self, session: AsyncSession) -> None:
        await self._settle_many(session, [(2, 0), (1, 1), (0, 2), (3, 1)])
        summary = await PerformanceService(session).summarise()

        assert summary.sample == 4
        assert summary.favourite_accuracy == pytest.approx(0.5)

    async def test_scores_model_against_market(self, session: AsyncSession) -> None:
        await self._settle_many(session, [(2, 0)] * 10)
        summary = await PerformanceService(session).summarise()

        assert summary.brier is not None
        assert summary.market_brier is not None
        assert summary.brier_skill is not None

    async def test_small_samples_are_called_out(self, session: AsyncSession) -> None:
        """The most important sentence the product can say about itself."""
        await self._settle_many(session, [(2, 0)] * 3)
        summary = await PerformanceService(session).summarise()

        assert "Too few" in summary.verdict

    async def test_derived_markets_are_scored(self, session: AsyncSession) -> None:
        await self._settle_many(session, [(2, 1), (0, 0), (3, 2), (1, 0)])
        summary = await PerformanceService(session).summarise()

        assert summary.over_2_5_accuracy is not None
        assert summary.btts_accuracy is not None
        assert summary.over_2_5_brier is not None

    async def test_breakdown_by_league(self, session: AsyncSession) -> None:
        await _analysis(session, "1", competition="Championship")
        await _analysis(session, "2", competition="La Liga")
        await SettlementService(session).settle(FakeResults({"1": (2, 0), "2": (0, 1)}), now=NOW)

        groups = await PerformanceService(session).by_competition()
        assert set(groups) == {"Championship", "La Liga"}
        assert all(g.sample == 1 for g in groups.values())

    async def test_breakdown_by_coverage(self, session: AsyncSession) -> None:
        await self._settle_many(session, [(2, 0), (1, 1)])
        groups = await PerformanceService(session).by_coverage()
        assert "fully_modelled" in groups

    async def test_breakdown_by_model_version(self, session: AsyncSession) -> None:
        await self._settle_many(session, [(2, 0)])
        groups = await PerformanceService(session).by_model_version()
        assert len(groups) == 1

    async def test_periods_cover_day_week_month_and_all_time(self, session: AsyncSession) -> None:
        await self._settle_many(session, [(2, 0), (1, 1)])
        periods = await PerformanceService(session).periods(now=NOW)

        assert set(periods) == {"today", "this week", "this month", "all time"}
        assert periods["all time"].sample == 2

    async def test_older_fixtures_fall_out_of_the_daily_window(self, session: AsyncSession) -> None:
        await _analysis(session, "old", kickoff=NOW - timedelta(days=20))
        await _analysis(session, "new", kickoff=NOW - timedelta(hours=4))
        await SettlementService(session).settle(
            FakeResults({"old": (1, 0), "new": (1, 0)}), now=NOW
        )

        periods = await PerformanceService(session).periods(now=NOW)
        assert periods["today"].sample == 1
        assert periods["all time"].sample == 2

    async def test_empty_history_is_handled(self, session: AsyncSession) -> None:
        summary = await PerformanceService(session).summarise()
        assert summary.sample == 0
        assert summary.brier is None


class TestPruningSupersededReviews:
    """Stale queue entries must not block fixtures forever.

    A queued alias short-circuits resolution, so an entry created under older
    logic keeps withholding matches even after the logic improves.
    """

    async def test_superseded_entry_is_removed(self, session: AsyncSession) -> None:
        from app.services.aliases import prune_superseded_reviews

        barnsley = await _team(session, "Barnsley")
        session.add(
            TeamAlias(
                team_id=barnsley.id,
                provider_name="csv_historical",
                alias="Barnsley",
                normalized_alias="barnsley",
                review_status=ReviewStatus.AUTO_CONFIRMED,
                is_confirmed=True,
            )
        )
        session.add(
            TeamAlias(
                team_id=barnsley.id,
                provider_name="csv_historical",
                alias="Burnley",
                normalized_alias="burnley",
                review_status=ReviewStatus.PENDING_REVIEW,
                is_confirmed=False,
            )
        )
        await session.flush()

        removed = await prune_superseded_reviews(session)
        assert removed == ["Burnley"]
        assert await AliasReviewService(session).pending() == []

    async def test_genuine_doubt_is_left_alone(self, session: AsyncSession) -> None:
        """Only entries the data now answers are cleared."""
        from app.services.aliases import prune_superseded_reviews

        team = await _team(session, "Stoke")
        session.add(
            TeamAlias(
                team_id=team.id,
                provider_name="csv_historical",
                alias="Stoke Citty",
                normalized_alias="stoke citty",
                review_status=ReviewStatus.PENDING_REVIEW,
                is_confirmed=False,
            )
        )
        await session.flush()

        assert await prune_superseded_reviews(session) == []
        assert len(await AliasReviewService(session).pending()) == 1

    async def test_pruned_name_then_resolves_as_its_own_club(self, session: AsyncSession) -> None:
        """The point of the exercise: the withheld fixture becomes analysable."""
        from app.services.aliases import prune_superseded_reviews

        barnsley = await _team(session, "Barnsley")
        session.add(
            TeamAlias(
                team_id=barnsley.id,
                provider_name="csv_historical",
                alias="Barnsley",
                normalized_alias="barnsley",
                review_status=ReviewStatus.AUTO_CONFIRMED,
                is_confirmed=True,
            )
        )
        session.add(
            TeamAlias(
                team_id=barnsley.id,
                provider_name="csv_historical",
                alias="Burnley",
                normalized_alias="burnley",
                review_status=ReviewStatus.PENDING_REVIEW,
                is_confirmed=False,
            )
        )
        await session.flush()
        await prune_superseded_reviews(session)

        result, created = await TeamResolver(session).resolve_or_create(
            provider_name="csv_historical", raw_name="Burnley", country="England"
        )
        assert created
        assert result.team is not None
        assert result.team.id != barnsley.id
