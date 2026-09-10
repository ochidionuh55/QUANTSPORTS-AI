"""The three daily boards.

Three different questions. If they collapse into three views of the same list,
the product is back to one universal narrative wearing three hats — so the
tests check that each board selects on its own criterion and that none of them
invents a selection to fill space.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.base import Base
from app.database.models import HistoricalMatch, StoredAnalysis, Team
from app.services.boards import (
    BANKER,
    BANKER_MIN_PROBABILITY,
    PATTERN,
    SHARP,
    TRACK_DESCRIPTIONS,
    TRACK_LABELS,
    BoardService,
)
from app.services.team_names import normalize_team_name

NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)


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


def _analysis(
    fixture_id: str = "1",
    hours: int = 6,
    home: str = "0.72",
    draw: str = "0.16",
    away: str = "0.12",
    coverage: str = "fully_modelled",
    matches: int = 40,
    home_id: int | None = None,
    away_id: int | None = None,
) -> StoredAnalysis:
    """Build a stored analysis."""
    return StoredAnalysis(
        provider_name="api_football",
        provider_event_id=fixture_id,
        home_name=f"Alpha{fixture_id}",
        away_name=f"Bravo{fixture_id}",
        competition="Championship",
        kickoff=NOW + timedelta(hours=hours),
        coverage=coverage,
        markets={"1X2": {"Home": home, "Draw": draw, "Away": away}},
        market_probabilities={"home": home, "draw": draw, "away": away},
        home_stats={"resolved": True, "matches": matches, "team_id": home_id},
        away_stats={"resolved": True, "matches": matches, "team_id": away_id},
        components_used=["poisson", "elo", "form"],
        components_dropped=[],
        computed_at=NOW,
    )


class TestBankerBoard:
    """Most likely, from well-evidenced fixtures only."""

    async def test_selects_high_probability_outcomes(self, session: AsyncSession) -> None:
        boards = await BoardService(session).build([_analysis()], now=NOW)
        entries = boards[BANKER]

        assert entries
        assert entries[0].probability >= BANKER_MIN_PROBABILITY

    async def test_ignores_low_probability_fixtures(self, session: AsyncSession) -> None:
        record = _analysis(home="0.45", draw="0.30", away="0.25")
        boards = await BoardService(session).build([record], now=NOW)
        assert boards[BANKER] == []

    async def test_requires_full_coverage(self, session: AsyncSession) -> None:
        """A confident number from a thin fixture is not a banker."""
        record = _analysis(coverage="data_only")
        boards = await BoardService(session).build([record], now=NOW)
        assert boards[BANKER] == []

    async def test_requires_a_real_sample(self, session: AsyncSession) -> None:
        record = _analysis(matches=5)
        boards = await BoardService(session).build([record], now=NOW)
        assert boards[BANKER] == []

    async def test_ranked_by_probability(self, session: AsyncSession) -> None:
        records = [
            _analysis("low", home="0.70", draw="0.18", away="0.12"),
            _analysis("high", home="0.86", draw="0.09", away="0.05"),
        ]
        boards = await BoardService(session).build(records, now=NOW)
        assert boards[BANKER][0].fixture_id == "high"

    async def test_one_entry_per_fixture(self, session: AsyncSession) -> None:
        """Three markets on one match is one idea padded out."""
        record = _analysis()
        record.markets = {
            "1X2": {"Home": "0.72", "Draw": "0.16", "Away": "0.12"},
            "Double chance": {
                "1X (home or draw)": "0.88",
                "12 (home or away)": "0.84",
                "X2 (draw or away)": "0.28",
            },
        }
        boards = await BoardService(session).build([record], now=NOW)
        assert len({e.fixture_id for e in boards[BANKER]}) == len(boards[BANKER])


class TestSharpBoard:
    """Most informative, not most probable."""

    async def test_prefers_unusual_over_likely(self, session: AsyncSession) -> None:
        """A 62% draw carries more information than an 88% double chance."""
        likely = _analysis("likely", home="0.62", draw="0.22", away="0.16")
        unusual = _analysis("unusual", home="0.20", draw="0.62", away="0.18")

        boards = await BoardService(session).build([likely, unusual], now=NOW)
        assert boards[SHARP]
        assert boards[SHARP][0].fixture_id == "unusual"

    async def test_reports_the_base_rate(self, session: AsyncSession) -> None:
        record = _analysis("1", home="0.20", draw="0.62", away="0.18")
        boards = await BoardService(session).build([record], now=NOW)
        assert boards[SHARP][0].base_rate < 0.35


class TestPatternBoard:
    """Counted history only — no model."""

    async def _club(self, session: AsyncSession, name: str) -> Team:
        team = Team(
            canonical_name=name,
            normalized_name=normalize_team_name(name),
            country="England",
            sport="football",
        )
        session.add(team)
        await session.flush()
        return team

    async def _low_scoring_history(
        self, session: AsyncSession, home: Team, away: Team, count: int = 26
    ) -> None:
        """Both clubs playing consistently low-scoring matches."""
        for index in range(count):
            session.add(
                HistoricalMatch(
                    provider_name="csv",
                    provider_match_id=f"p-{home.id}-{index}",
                    home_team_id=home.id,
                    away_team_id=away.id,
                    season="2025/2026",
                    match_date=(NOW - timedelta(days=10 + index)).date(),
                    home_goals=1,
                    away_goals=0,
                    data_version="v",
                    ingested_at=NOW,
                )
            )
        await session.flush()

    async def test_finds_agreeing_histories(self, session: AsyncSession) -> None:
        home = await self._club(session, "Alpha")
        away = await self._club(session, "Bravo")
        await self._low_scoring_history(session, home, away)

        record = _analysis("1", home_id=home.id, away_id=away.id)
        session.add(record)
        await session.flush()

        boards = await BoardService(session).build([record], now=NOW)
        entries = boards[PATTERN]
        assert entries
        assert entries[0].outcome == "Under 2.5"

    async def test_rationale_cites_counted_matches(self, session: AsyncSession) -> None:
        """A reader must be able to verify it by counting."""
        home = await self._club(session, "Alpha")
        away = await self._club(session, "Bravo")
        await self._low_scoring_history(session, home, away)

        record = _analysis("1", home_id=home.id, away_id=away.id)
        session.add(record)
        await session.flush()

        entry = (await BoardService(session).build([record], now=NOW))[PATTERN][0]
        assert "%" in entry.rationale
        assert "no model" in entry.rationale.lower()
        assert entry.factors["home_matches"] >= 20

    async def test_requires_history_for_both_sides(self, session: AsyncSession) -> None:
        record = _analysis("1", home_id=None, away_id=None)
        boards = await BoardService(session).build([record], now=NOW)
        assert boards[PATTERN] == []

    async def test_thin_history_is_excluded(self, session: AsyncSession) -> None:
        home = await self._club(session, "Alpha")
        away = await self._club(session, "Bravo")
        await self._low_scoring_history(session, home, away, count=5)

        record = _analysis("1", home_id=home.id, away_id=away.id)
        session.add(record)
        await session.flush()

        boards = await BoardService(session).build([record], now=NOW)
        assert boards[PATTERN] == []


class TestBoardIntegrity:
    """Rules that apply to every board."""

    async def test_started_fixtures_are_never_selected(self, session: AsyncSession) -> None:
        record = _analysis("1", hours=-2)
        boards = await BoardService(session).build([record], now=NOW)
        assert all(entries == [] for entries in boards.values())

    async def test_an_empty_card_produces_empty_boards(self, session: AsyncSession) -> None:
        """Nothing is invented to fill space."""
        boards = await BoardService(session).build([], now=NOW)
        assert set(boards) == {BANKER, SHARP, PATTERN}
        assert all(entries == [] for entries in boards.values())

    async def test_every_board_is_described(self) -> None:
        """A user must be able to tell what each board means."""
        for track in (BANKER, SHARP, PATTERN):
            assert TRACK_LABELS[track]
            assert len(TRACK_DESCRIPTIONS[track]) > 40

    async def test_banker_description_warns_against_value_reading(self) -> None:
        """Most likely is not best value, and the product must say so."""
        assert "value" in TRACK_DESCRIPTIONS[BANKER].lower()

    async def test_pattern_description_disclaims_modelling(self) -> None:
        assert "no model" in TRACK_DESCRIPTIONS[PATTERN].lower()

    async def test_boards_select_differently(self, session: AsyncSession) -> None:
        """Different criteria must be able to reach different answers.

        They can legitimately agree — a fixture may be both the most likely and
        the most informative. But a 72% home win and a 62% draw should diverge:
        the draw carries more information (0.54 against 0.31) while the home
        win is more probable.
        """
        records = [
            _analysis("banker", home="0.72", draw="0.16", away="0.12"),
            _analysis("sharp", home="0.20", draw="0.62", away="0.18"),
        ]
        for record in records:
            session.add(record)
        await session.flush()

        boards = await BoardService(session).build(records, now=NOW)
        assert boards[BANKER][0].fixture_id == "banker"
        assert boards[SHARP][0].fixture_id == "sharp"
