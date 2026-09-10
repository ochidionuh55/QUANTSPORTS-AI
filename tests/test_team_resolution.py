"""Team resolution tests.

The important assertions here are the negative ones: that a plausible-looking
but uncertain match is *not* accepted. A wrong team mapping produces no error,
only quietly corrupted ratings, so the review queue must be preferred to a
guess.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.enums import ReviewStatus
from app.database.models import Team
from app.services.team_names import normalize_team_name
from app.services.team_resolution import (
    ResolutionMethod,
    TeamResolver,
    similarity,
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


async def _add_team(session: AsyncSession, name: str, country: str = "England") -> Team:
    """Insert a canonical team with its normalized form."""
    team = Team(
        canonical_name=name,
        normalized_name=normalize_team_name(name),
        country=country,
        sport="football",
    )
    session.add(team)
    await session.flush()
    return team


@pytest_asyncio.fixture
async def premier_league(session: AsyncSession) -> dict[str, Team]:
    """Insert a handful of clubs with genuinely confusable names."""
    names = [
        "Manchester United",
        "Manchester City",
        "Tottenham Hotspur",
        "Nottingham Forest",
        "Wolverhampton Wanderers",
        "Brighton and Hove Albion",
    ]
    return {name: await _add_team(session, name) for name in names}


class TestNormalization:
    """Normalization must collapse spelling noise without losing meaning."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Manchester United FC", "manchester united"),
            ("Man Utd", "man united"),
            ("Atlético Madrid", "atletico madrid"),
            ("Brighton & Hove Albion", "brighton and hove albion"),
            ("  Arsenal   FC  ", "arsenal"),
            ("St. Pauli", "saint pauli"),
            ("1. FC Köln", "1 koln"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert normalize_team_name(raw) == expected

    def test_preserves_age_group_markers(self) -> None:
        """A youth side must never normalize to its senior side."""
        assert normalize_team_name("Chelsea U21") != normalize_team_name("Chelsea")
        assert "u21" in normalize_team_name("Chelsea U21")

    def test_preserves_reserve_markers(self) -> None:
        assert normalize_team_name("Bayern Munich II") != normalize_team_name("Bayern Munich")

    def test_empty_input(self) -> None:
        assert normalize_team_name("") == ""

    def test_name_of_only_noise_survives(self) -> None:
        """A name made entirely of noise must not collapse to empty."""
        assert normalize_team_name("FC") != ""


class TestSimilarity:
    """Scoring must separate genuinely different clubs."""

    def test_identical_scores_one(self) -> None:
        assert similarity("arsenal", "arsenal") == 1.0

    def test_manchester_clubs_are_separated(self) -> None:
        """The classic false positive: shared prefix, different club."""
        score = similarity("manchester united", "manchester city")
        assert score < 0.75

    def test_empty_scores_zero(self) -> None:
        assert similarity("", "arsenal") == 0.0


class TestResolutionStages:
    """Each stage resolves what it should, in order."""

    async def test_exact_canonical_match(
        self, session: AsyncSession, premier_league: dict[str, Team]
    ) -> None:
        resolver = TeamResolver(session)
        result = await resolver.resolve("sporty", "Manchester United FC")

        assert result.is_resolved
        assert result.team is not None
        assert result.team.canonical_name == "Manchester United"
        assert result.method is ResolutionMethod.EXACT_CANONICAL

    async def test_learned_alias_is_reused(
        self, session: AsyncSession, premier_league: dict[str, Team]
    ) -> None:
        resolver = TeamResolver(session)
        await resolver.resolve("sporty", "Manchester United FC")
        second = await resolver.resolve("sporty", "Manchester United FC")

        assert second.method is ResolutionMethod.EXACT_ALIAS
        assert second.confidence == 1.0

    async def test_external_id_match(
        self, session: AsyncSession, premier_league: dict[str, Team]
    ) -> None:
        resolver = TeamResolver(session)
        await resolver.resolve("sporty", "Manchester United", external_team_id="ext-99")
        result = await resolver.resolve(
            "sporty", "Man United Football Club", external_team_id="ext-99"
        )

        assert result.is_resolved
        assert result.method is ResolutionMethod.EXTERNAL_ID


class TestUncertaintyIsNeverGuessed:
    """The core safety property of this service."""

    async def test_confusable_name_is_not_auto_accepted(
        self, session: AsyncSession, premier_league: dict[str, Team]
    ) -> None:
        """ "Manchester" alone must not silently become United or City."""
        resolver = TeamResolver(session)
        result = await resolver.resolve("sporty", "Manchester")

        assert not result.is_resolved

    async def test_unknown_team_is_unresolved(
        self, session: AsyncSession, premier_league: dict[str, Team]
    ) -> None:
        resolver = TeamResolver(session)
        result = await resolver.resolve("sporty", "Shanghai Port")

        assert not result.is_resolved
        assert not result.needs_review

    async def test_no_candidates_at_all(self, session: AsyncSession) -> None:
        resolver = TeamResolver(session)
        result = await resolver.resolve("sporty", "Arsenal")

        assert not result.is_resolved
        assert result.method is ResolutionMethod.NONE

    async def test_empty_name_is_unresolved(self, session: AsyncSession) -> None:
        resolver = TeamResolver(session)
        result = await resolver.resolve("sporty", "   ")
        assert not result.is_resolved

    async def test_thresholds_are_validated(self, session: AsyncSession) -> None:
        with pytest.raises(ValueError, match="review_threshold"):
            TeamResolver(session, auto_confirm_threshold=0.5, review_threshold=0.9)


class TestReviewQueue:
    """Queued aliases must not be usable until a human decides."""

    async def test_pending_alias_does_not_resolve(
        self, session: AsyncSession, premier_league: dict[str, Team]
    ) -> None:
        resolver = TeamResolver(session, auto_confirm_threshold=0.99, review_threshold=0.5)
        first = await resolver.resolve("sporty", "Tottenham Hotspurs")
        assert first.needs_review

        second = await resolver.resolve("sporty", "Tottenham Hotspurs")
        assert not second.is_resolved
        assert second.needs_review

    async def test_confirming_makes_the_alias_usable(
        self, session: AsyncSession, premier_league: dict[str, Team]
    ) -> None:
        resolver = TeamResolver(session, auto_confirm_threshold=0.99, review_threshold=0.5)
        await resolver.resolve("sporty", "Tottenham Hotspurs")

        queued = await resolver.pending_review()
        assert len(queued) == 1

        await resolver.confirm(queued[0].id)
        resolved = await resolver.resolve("sporty", "Tottenham Hotspurs")

        assert resolved.is_resolved
        assert resolved.review_status is ReviewStatus.MANUALLY_CONFIRMED

    async def test_rejected_alias_stays_unresolved(
        self, session: AsyncSession, premier_league: dict[str, Team]
    ) -> None:
        resolver = TeamResolver(session, auto_confirm_threshold=0.99, review_threshold=0.5)
        await resolver.resolve("sporty", "Tottenham Hotspurs")
        queued = await resolver.pending_review()

        await resolver.reject(queued[0].id, note="Different club")
        result = await resolver.resolve("sporty", "Tottenham Hotspurs")

        assert not result.is_resolved
        assert result.review_status is ReviewStatus.REJECTED

    async def test_confirm_can_reassign_to_the_correct_team(
        self, session: AsyncSession, premier_league: dict[str, Team]
    ) -> None:
        resolver = TeamResolver(session, auto_confirm_threshold=0.99, review_threshold=0.5)
        await resolver.resolve("sporty", "Nottingam Forrest")
        queued = await resolver.pending_review()

        correct = premier_league["Nottingham Forest"]
        await resolver.confirm(queued[0].id, team_id=correct.id)

        result = await resolver.resolve("sporty", "Nottingham")
        assert result.is_resolved
        assert result.team is not None
        assert result.team.id == correct.id

    async def test_confirm_unknown_alias(self, session: AsyncSession) -> None:
        resolver = TeamResolver(session)
        with pytest.raises(LookupError):
            await resolver.confirm(999_999)
