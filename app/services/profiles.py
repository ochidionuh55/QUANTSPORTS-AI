"""Statistical profiles from the historical record.

The part of QUANTSPORT a bookmaker does not offer. Their app tells you the
price; it does not tell you that a side has gone under 2.5 in two thirds of
their home matches across three seasons, or that a league draws 31% of the
time.

We hold 113,029 matches. Every figure here is counted from them — no model, no
forecast, nothing to validate or promote. A user who distrusts our predictions
entirely can still use this, which is precisely why it is worth building.

**Everything is a count, never an estimate.** Where the sample is too small to
mean anything the profile says so rather than reporting a percentage of four
matches as though it were a rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import Competition, HistoricalMatch, Team

logger = get_logger(__name__)

MIN_SAMPLE = 6
"""Below this, a percentage is noise dressed as a statistic."""

DEFAULT_WINDOW_DAYS = 365 * 3
RECENT_FORM_MATCHES = 10


@dataclass
class SplitRecord:
    """Results and goals over a set of matches."""

    played: int = 0
    won: int = 0
    drawn: int = 0
    lost: int = 0
    scored: int = 0
    conceded: int = 0

    over_0_5: int = 0
    over_1_5: int = 0
    over_2_5: int = 0
    over_3_5: int = 0
    both_scored: int = 0
    clean_sheets: int = 0
    failed_to_score: int = 0

    @property
    def is_meaningful(self) -> bool:
        """Whether the sample supports quoting rates."""
        return self.played >= MIN_SAMPLE

    def rate(self, count: int) -> float | None:
        """Return a rate, or ``None`` when the sample is too small."""
        if not self.is_meaningful:
            return None
        return count / self.played

    @property
    def points_per_game(self) -> float | None:
        """Points per match."""
        if not self.played:
            return None
        return (self.won * 3 + self.drawn) / self.played

    @property
    def goals_per_game(self) -> float | None:
        """Combined goals per match."""
        if not self.played:
            return None
        return (self.scored + self.conceded) / self.played

    def add(self, scored: int, conceded: int) -> None:
        """Record one match from this team's perspective."""
        self.played += 1
        self.scored += scored
        self.conceded += conceded
        if scored > conceded:
            self.won += 1
        elif scored == conceded:
            self.drawn += 1
        else:
            self.lost += 1

        total = scored + conceded
        self.over_0_5 += total > 0
        self.over_1_5 += total > 1
        self.over_2_5 += total > 2
        self.over_3_5 += total > 3
        self.both_scored += scored > 0 and conceded > 0
        self.clean_sheets += conceded == 0
        self.failed_to_score += scored == 0


@dataclass
class TeamProfile:
    """Everything we know about one club from the record."""

    team_id: int
    name: str
    country: str | None = None

    overall: SplitRecord = field(default_factory=SplitRecord)
    home: SplitRecord = field(default_factory=SplitRecord)
    away: SplitRecord = field(default_factory=SplitRecord)

    recent_results: list[str] = field(default_factory=list)
    """Most recent first, as ``W``, ``D`` or ``L``."""

    recent_scorelines: list[tuple[str, str, int, int, date]] = field(default_factory=list)
    first_match: date | None = None
    last_match: date | None = None
    competitions: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        """Whether any matches were found."""
        return self.overall.played > 0

    @property
    def form_string(self) -> str:
        """Recent results as a compact string, oldest first."""
        return "".join(reversed(self.recent_results))

    @property
    def home_advantage(self) -> float | None:
        """Points per game at home minus away.

        The number a user actually wants when asking whether a side travels
        badly, and one no bookmaker screen shows.
        """
        if not (self.home.is_meaningful and self.away.is_meaningful):
            return None
        home = self.home.points_per_game
        away = self.away.points_per_game
        if home is None or away is None:
            return None
        return home - away


@dataclass
class HeadToHead:
    """The record between two clubs."""

    home_name: str
    away_name: str
    played: int = 0
    home_wins: int = 0
    draws: int = 0
    away_wins: int = 0
    home_goals: int = 0
    away_goals: int = 0
    over_2_5: int = 0
    both_scored: int = 0
    meetings: list[tuple[date, str, int, int, str]] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        """Whether the clubs have met on record."""
        return self.played > 0

    def rate(self, count: int) -> float | None:
        """Return a rate, or ``None`` when too few meetings."""
        if self.played < 4:
            return None
        return count / self.played


@dataclass
class LeagueProfile:
    """Aggregate character of a competition."""

    name: str
    matches: int = 0
    home_wins: int = 0
    draws: int = 0
    away_wins: int = 0
    goals: int = 0
    over_2_5: int = 0
    both_scored: int = 0
    seasons: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        """Whether any matches were found."""
        return self.matches > 0

    def rate(self, count: int) -> float | None:
        """Return a rate, or ``None`` when the sample is too small."""
        if self.matches < 50:
            return None
        return count / self.matches

    @property
    def goals_per_game(self) -> float | None:
        """Average goals per match."""
        if not self.matches:
            return None
        return self.goals / self.matches


class ProfileService:
    """Builds statistical profiles from stored history."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_teams(self, query: str, limit: int = 8) -> list[Team]:
        """Search clubs by name fragment."""
        needle = query.strip().lower()
        if len(needle) < 2:
            return []
        result = await self._session.execute(
            select(Team)
            .where(func.lower(Team.canonical_name).contains(needle))
            .order_by(Team.canonical_name)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def team_profile(
        self,
        team_id: int,
        window_days: int = DEFAULT_WINDOW_DAYS,
        now: datetime | None = None,
    ) -> TeamProfile | None:
        """Build a club's profile from its recent matches."""
        team = await self._session.get(Team, team_id)
        if team is None:
            return None

        moment = now or datetime.now(UTC)
        cutoff = (moment - timedelta(days=window_days)).date()

        result = await self._session.execute(
            select(HistoricalMatch, Competition.canonical_name)
            .outerjoin(Competition, HistoricalMatch.competition_id == Competition.id)
            .where(
                or_(
                    HistoricalMatch.home_team_id == team_id,
                    HistoricalMatch.away_team_id == team_id,
                ),
                HistoricalMatch.match_date >= cutoff,
            )
            .order_by(HistoricalMatch.match_date.desc())
        )
        rows = list(result.all())

        profile = TeamProfile(team_id=team_id, name=team.canonical_name, country=team.country)
        if not rows:
            return profile

        opponent_ids = {
            m.away_team_id if m.home_team_id == team_id else m.home_team_id for m, _ in rows
        }
        opponents = await self._names(opponent_ids)
        competitions: set[str] = set()

        for match, competition in rows:
            at_home = match.home_team_id == team_id
            scored, conceded = (
                (match.home_goals, match.away_goals)
                if at_home
                else (match.away_goals, match.home_goals)
            )

            profile.overall.add(scored, conceded)
            (profile.home if at_home else profile.away).add(scored, conceded)

            if competition:
                competitions.add(str(competition))

            if len(profile.recent_results) < RECENT_FORM_MATCHES:
                profile.recent_results.append(
                    "W" if scored > conceded else ("D" if scored == conceded else "L")
                )
                opponent_id = match.away_team_id if at_home else match.home_team_id
                profile.recent_scorelines.append(
                    (
                        "H" if at_home else "A",
                        opponents.get(opponent_id, "Unknown"),
                        scored,
                        conceded,
                        match.match_date,
                    )
                )

        profile.last_match = rows[0][0].match_date
        profile.first_match = rows[-1][0].match_date
        profile.competitions = sorted(competitions)
        return profile

    async def head_to_head(
        self, home_team_id: int, away_team_id: int, limit: int = 10
    ) -> HeadToHead:
        """Return the record between two clubs.

        Counted from the perspective of the nominated home side, so "home wins"
        means that club winning wherever the match was played — otherwise the
        figure would mix two different questions.
        """
        home = await self._session.get(Team, home_team_id)
        away = await self._session.get(Team, away_team_id)
        record = HeadToHead(
            home_name=home.canonical_name if home else "Unknown",
            away_name=away.canonical_name if away else "Unknown",
        )

        result = await self._session.execute(
            select(HistoricalMatch)
            .where(
                or_(
                    (HistoricalMatch.home_team_id == home_team_id)
                    & (HistoricalMatch.away_team_id == away_team_id),
                    (HistoricalMatch.home_team_id == away_team_id)
                    & (HistoricalMatch.away_team_id == home_team_id),
                )
            )
            .order_by(HistoricalMatch.match_date.desc())
        )

        for match in result.scalars().all():
            first_at_home = match.home_team_id == home_team_id
            first_goals, second_goals = (
                (match.home_goals, match.away_goals)
                if first_at_home
                else (match.away_goals, match.home_goals)
            )

            record.played += 1
            record.home_goals += first_goals
            record.away_goals += second_goals
            if first_goals > second_goals:
                record.home_wins += 1
            elif first_goals == second_goals:
                record.draws += 1
            else:
                record.away_wins += 1

            record.over_2_5 += (match.home_goals + match.away_goals) > 2
            record.both_scored += match.home_goals > 0 and match.away_goals > 0

            if len(record.meetings) < limit:
                record.meetings.append(
                    (
                        match.match_date,
                        "H" if first_at_home else "A",
                        first_goals,
                        second_goals,
                        record.away_name,
                    )
                )

        return record

    async def league_profile(
        self,
        competition_name: str,
        window_days: int = DEFAULT_WINDOW_DAYS,
        now: datetime | None = None,
    ) -> LeagueProfile:
        """Build a competition's aggregate character."""
        moment = now or datetime.now(UTC)
        cutoff = (moment - timedelta(days=window_days)).date()

        result = await self._session.execute(
            select(HistoricalMatch)
            .join(Competition, HistoricalMatch.competition_id == Competition.id)
            .where(
                Competition.canonical_name == competition_name,
                HistoricalMatch.match_date >= cutoff,
            )
        )

        profile = LeagueProfile(name=competition_name)
        seasons: set[str] = set()
        for match in result.scalars().all():
            profile.matches += 1
            profile.goals += match.home_goals + match.away_goals
            if match.home_goals > match.away_goals:
                profile.home_wins += 1
            elif match.home_goals == match.away_goals:
                profile.draws += 1
            else:
                profile.away_wins += 1
            profile.over_2_5 += (match.home_goals + match.away_goals) > 2
            profile.both_scored += match.home_goals > 0 and match.away_goals > 0
            if match.season:
                seasons.add(match.season)

        profile.seasons = sorted(seasons)
        return profile

    async def _names(self, team_ids: set[int]) -> dict[int, str]:
        """Look up club names in one query."""
        if not team_ids:
            return {}
        result = await self._session.execute(
            select(Team.id, Team.canonical_name).where(Team.id.in_(team_ids))
        )
        return {row[0]: row[1] for row in result.all()}

    async def competitions_with_data(self) -> list[tuple[str, int]]:
        """Return competitions and how many matches each holds."""
        result = await self._session.execute(
            select(Competition.canonical_name, func.count(HistoricalMatch.id))
            .join(HistoricalMatch, HistoricalMatch.competition_id == Competition.id)
            .group_by(Competition.canonical_name)
            .order_by(func.count(HistoricalMatch.id).desc())
        )
        return [(str(name), int(count)) for name, count in result.all()]
