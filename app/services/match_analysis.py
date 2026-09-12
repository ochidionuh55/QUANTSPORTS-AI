"""Live match analysis.

Produces what the beta shows a user: probabilities, expected goals, derived
markets, and an honest statement about how much any of it can be trusted.

**Coverage is graded, never hidden.** A fixture whose teams have no history
gets a status saying so, not a silently-omitted row and not a confident
guess. Four grades:

* ``FULLY_MODELLED`` — both sides have enough history for every component.
* ``PARTIALLY_MODELLED`` — some components available, others dropped.
* ``DATA_ONLY`` — the fixture exists but no component is reliable.
* ``UNSUPPORTED`` — teams could not be resolved at all.

**Value detection stays gated.** Even when the analysis is good, the market
assessment says "no qualified opportunity" unless a promoted model exists.
That is not a placeholder — it is the truthful answer while the gate is
unpassed, and the backtests say it is unpassed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import HistoricalMatch, Team
from app.providers.models import ProviderEvent
from app.quant.elo import EloEngine
from app.quant.ensemble import ComponentEstimate, EnsembleError, build_ensemble
from app.quant.form import MatchOutcome, summarise
from app.quant.form import predict as form_predict
from app.quant.poisson import (
    LeagueAverages,
    MatchProbabilities,
    expected_goals,
    match_probabilities,
    team_strength,
)
from app.quant.probability import MarginMethod, ProbabilityError, fair_probabilities
from app.services.team_resolution import TeamResolver

logger = get_logger(__name__)

HISTORY_WINDOW_DAYS = 365 * 3
"""How far back to look. Three seasons balances sample size against squads,
managers and tactics having changed beyond recognition."""

MIN_MATCHES_FOR_ANALYSIS = 8


class Coverage(StrEnum):
    """How well a fixture can be analysed."""

    FULLY_MODELLED = "fully_modelled"
    PARTIALLY_MODELLED = "partially_modelled"
    DATA_ONLY = "data_only"
    UNSUPPORTED = "unsupported"

    @property
    def badge(self) -> str:
        """Return the user-facing indicator."""
        return {
            Coverage.FULLY_MODELLED: "🟢 Fully modelled",
            Coverage.PARTIALLY_MODELLED: "🟡 Partially modelled",
            Coverage.DATA_ONLY: "🔵 Data only",
            Coverage.UNSUPPORTED: "⚪ Unsupported",
        }[self]


@dataclass
class TeamSnapshot:
    """What is known about one side going into a fixture."""

    source_name: str
    team_id: int | None = None
    matches: int = 0
    goals_scored_per_match: float = 0.0
    goals_conceded_per_match: float = 0.0
    points_per_match: float = 0.0
    elo: float | None = None

    @property
    def is_resolved(self) -> bool:
        """Whether the name mapped to a canonical team."""
        return self.team_id is not None

    @property
    def has_enough_history(self) -> bool:
        """Whether the sample supports modelling."""
        return self.matches >= MIN_MATCHES_FOR_ANALYSIS


@dataclass
class MatchAnalysis:
    """The complete analysis of one fixture."""

    home_name: str
    away_name: str
    kickoff: datetime
    competition: str | None
    coverage: Coverage

    home: TeamSnapshot
    away: TeamSnapshot

    probabilities: dict[str, Decimal] = field(default_factory=dict)
    expected_home_goals: float | None = None
    expected_away_goals: float | None = None
    over_under: dict[str, Decimal] = field(default_factory=dict)
    both_teams_score: Decimal | None = None

    markets: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    """Every supported market, derived from the same distribution.

    Derived rather than modelled separately: double chance is a sum of 1X2
    outcomes and over/under comes from the scoreline grid, so they are exact
    consequences of the match probabilities rather than independent guesses.
    Any market that cannot be derived is simply absent.
    """

    market_probabilities: dict[str, Decimal] | None = None
    market_odds: dict[str, Decimal] | None = None

    components_used: tuple[str, ...] = ()
    components_dropped: tuple[str, ...] = ()

    model_probabilities: dict[str, Decimal] = field(default_factory=dict)
    """Result probabilities from our models alone, with no bookmaker input.

    Kept separate from ``probabilities``, which is the published posterior and
    equals the market prior wherever odds exist. Confusing the two would let a
    market-derived number be presented as a model finding.
    """

    component_views: tuple[dict[str, Decimal], ...] = ()
    """Each component's own result estimate, for measuring agreement.

    Averaging hides disagreement, and disagreement is the honest signal that a
    fixture is hard to call — so the individual views are kept.
    """
    unavailable_reason: str | None = None

    def build_markets(self) -> None:
        """Populate every market that the available estimates support."""
        if not self.probabilities:
            return

        home = self.probabilities["home"]
        draw = self.probabilities["draw"]
        away = self.probabilities["away"]

        self.markets["1X2"] = {"Home": home, "Draw": draw, "Away": away}
        self.markets["Double chance"] = {
            "1X (home or draw)": home + draw,
            "12 (home or away)": home + away,
            "X2 (draw or away)": draw + away,
        }
        self.markets["Result"] = {
            "Home win": home,
            "Draw": draw,
            "Away win": away,
        }

        if self.over_under:
            totals: dict[str, Decimal] = {}
            for line in ("0.5", "1.5", "2.5", "3.5"):
                over = self.over_under.get(f"over_{line}")
                under = self.over_under.get(f"under_{line}")
                if over is not None and under is not None:
                    totals[f"Over {line}"] = over
                    totals[f"Under {line}"] = under
            if totals:
                self.markets["Goals"] = totals

        if self.both_teams_score is not None:
            self.markets["Both teams to score"] = {
                "Yes": self.both_teams_score,
                "No": Decimal(1) - self.both_teams_score,
            }

    @property
    def has_probabilities(self) -> bool:
        """Whether a distribution could be produced."""
        return bool(self.probabilities)

    @property
    def market_assessment(self) -> str:
        """Explain the value position honestly.

        Always "no qualified opportunity" while the gate is unpassed. The
        reason given depends on what is actually true for this fixture, rather
        than a single canned line.
        """
        if not self.has_probabilities:
            return (
                "No market assessment: there is not enough history to estimate "
                "probabilities for this fixture."
            )
        if not self.market_probabilities:
            return "No market assessment: no odds were available for this fixture."
        return (
            "No qualified value opportunity.\n\n"
            "The value engine is disabled. Our models are well calibrated but "
            "have not yet demonstrated an edge over market prices in "
            "backtesting, so we do not publish betting selections."
        )


class MatchAnalysisService:
    """Analyses fixtures using stored history and the quant models."""

    def __init__(self, session: AsyncSession, sport: str = "football") -> None:
        self._session = session
        self._sport = sport

    async def analyse_event(
        self, event: ProviderEvent, now: datetime | None = None
    ) -> MatchAnalysis:
        """Analyse one live fixture."""
        moment = now or datetime.now(UTC)
        competition = event.competition.name if event.competition else None
        country = event.competition.country if event.competition else None

        home = await self._snapshot(event.home_team.name, country, moment)
        away = await self._snapshot(event.away_team.name, country, moment)

        analysis = MatchAnalysis(
            home_name=event.home_team.name,
            away_name=event.away_team.name,
            kickoff=event.start_time,
            competition=competition,
            coverage=Coverage.UNSUPPORTED,
            home=home,
            away=away,
            market_odds=_market_odds(event),
        )

        if not (home.is_resolved and away.is_resolved):
            unresolved = [s.source_name for s in (home, away) if not s.is_resolved]
            # No history does not mean nothing to say. Where the fixture is
            # priced, the market's own implied probabilities are real
            # information, and withholding them leaves a user with an empty
            # screen on a match the whole world is betting on.
            if self._market_only(analysis):
                analysis.coverage = Coverage.DATA_ONLY
                analysis.unavailable_reason = (
                    f"No matched history for {', '.join(unresolved)}, so our "
                    "models could not run. The probabilities below are the "
                    "market's own view with the bookmaker margin removed — not "
                    "a QUANTSPORT estimate."
                )
                return analysis

            analysis.unavailable_reason = (
                f"We could not match {', '.join(unresolved)} to a club in our "
                "records, and no odds were available either, so there is "
                "nothing reliable to show for this fixture."
            )
            return analysis

        if not (home.has_enough_history and away.has_enough_history):
            analysis.coverage = Coverage.DATA_ONLY
            thin = [s for s in (home, away) if not s.has_enough_history]
            if self._market_only(analysis):
                analysis.unavailable_reason = (
                    f"Only {', '.join(str(s.matches) for s in thin)} recent "
                    f"matches on record for {', '.join(s.source_name for s in thin)}, "
                    f"below the {MIN_MATCHES_FOR_ANALYSIS} our models need. The "
                    "probabilities below are the market's own view with the "
                    "bookmaker margin removed — not a QUANTSPORT estimate."
                )
                return analysis
            analysis.unavailable_reason = (
                f"Too few recent matches on record for "
                f"{', '.join(s.source_name for s in thin)} "
                f"({', '.join(str(s.matches) for s in thin)} found, "
                f"{MIN_MATCHES_FOR_ANALYSIS} needed). Estimates would be "
                "unreliable, so none are shown."
            )
            return analysis

        await self._model(analysis, moment)
        analysis.build_markets()
        return analysis

    def _market_only(self, analysis: MatchAnalysis) -> bool:
        """Publish the market's implied probabilities where no model can run.

        The margin is removed the same way it is for a modelled fixture, so the
        numbers are comparable. They are labelled as the market's view rather
        than ours, because that is exactly what they are — presenting a
        bookmaker's price as a QUANTSPORT estimate would be a lie of
        attribution even though every number is real.

        Returns:
            Whether a market view was available and published.
        """
        prior = self._market_prior(analysis)
        if prior is None:
            return False

        analysis.probabilities = prior
        analysis.components_used = ("market",)
        analysis.build_markets()
        return True

    async def _snapshot(
        self, source_name: str, country: str | None, moment: datetime
    ) -> TeamSnapshot:
        """Resolve a team and summarise its recent record."""
        snapshot = TeamSnapshot(source_name=source_name)

        resolver = TeamResolver(self._session)
        resolution = await resolver.resolve(
            "api_football", source_name, sport=self._sport, country=country
        )
        if not resolution.is_resolved or resolution.team is None:
            return snapshot

        snapshot.team_id = resolution.team.id
        matches = await self._recent(resolution.team.id, moment)
        snapshot.matches = len(matches)
        if not matches:
            return snapshot

        scored = conceded = points = 0
        for match in matches:
            at_home = match.home_team_id == resolution.team.id
            for_, against = (
                (match.home_goals, match.away_goals)
                if at_home
                else (match.away_goals, match.home_goals)
            )
            scored += for_
            conceded += against
            points += 3 if for_ > against else (1 if for_ == against else 0)

        count = len(matches)
        snapshot.goals_scored_per_match = scored / count
        snapshot.goals_conceded_per_match = conceded / count
        snapshot.points_per_match = points / count
        return snapshot

    async def _recent(
        self, team_id: int, moment: datetime, limit: int = 60
    ) -> list[HistoricalMatch]:
        """Return a team's recent matches, newest first."""
        cutoff = moment - timedelta(days=HISTORY_WINDOW_DAYS)
        result = await self._session.execute(
            select(HistoricalMatch)
            .where(
                or_(
                    HistoricalMatch.home_team_id == team_id,
                    HistoricalMatch.away_team_id == team_id,
                ),
                HistoricalMatch.match_date >= cutoff,
                HistoricalMatch.match_date < moment,
            )
            .order_by(HistoricalMatch.match_date.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def _model(self, analysis: MatchAnalysis, moment: datetime) -> None:
        """Run the models and attach their output."""
        assert analysis.home.team_id is not None
        assert analysis.away.team_id is not None

        home_matches = await self._recent(analysis.home.team_id, moment)
        away_matches = await self._recent(analysis.away.team_id, moment)
        league = await self._league_averages(moment)

        poisson = self._poisson(analysis, home_matches, away_matches, league)
        components: list[ComponentEstimate] = []

        if poisson is not None:
            analysis.expected_home_goals = round(poisson.lambda_home, 2)
            analysis.expected_away_goals = round(poisson.lambda_away, 2)
            analysis.over_under = poisson.over_under
            analysis.both_teams_score = poisson.both_teams_score
            components.append(
                ComponentEstimate(
                    "poisson",
                    {
                        "home": poisson.home_win,
                        "draw": poisson.draw,
                        "away": poisson.away_win,
                    },
                )
            )

        elo_estimate = self._elo(analysis, home_matches, away_matches, moment)
        if elo_estimate is not None:
            components.append(elo_estimate)

        form_estimate = self._form(analysis, home_matches, away_matches, moment)
        if form_estimate is not None:
            components.append(form_estimate)

        # The model-only view, recorded before any market blending. This is
        # what the Best of the Day services rank on.
        if components:
            blended = {
                key: sum((c.probabilities[key] for c in components), Decimal(0)) / len(components)
                for key in ("home", "draw", "away")
            }
            total = sum(blended.values())
            if total > 0:
                analysis.model_probabilities = {k: v / total for k, v in blended.items()}
            analysis.component_views = tuple(dict(c.probabilities) for c in components)

        prior = self._market_prior(analysis)

        if not components:
            analysis.coverage = Coverage.DATA_ONLY
            analysis.unavailable_reason = (
                "No model could be applied to this fixture with the available " "history."
            )
            return

        # With no promoted model, reliability is zero: the posterior is the
        # market prior where odds exist. Where they do not, the model estimate
        # stands alone and is labelled experimental.
        if prior is not None:
            try:
                ensemble = build_ensemble(components, prior, reliability=0.0)
            except EnsembleError as exc:
                logger.warning("analysis.ensemble_failed", error=str(exc))
                return
            analysis.probabilities = ensemble.posterior
            analysis.components_used = ensemble.components_used
            analysis.components_dropped = ensemble.components_dropped
        else:
            blended = {
                key: sum((c.probabilities[key] for c in components), Decimal(0)) / len(components)
                for key in ("home", "draw", "away")
            }
            total = sum(blended.values())
            analysis.probabilities = {k: v / total for k, v in blended.items()}
            analysis.components_used = tuple(c.name for c in components)

        analysis.coverage = (
            Coverage.FULLY_MODELLED
            if len(analysis.components_used) >= 3
            else Coverage.PARTIALLY_MODELLED
        )

    def _market_prior(self, analysis: MatchAnalysis) -> dict[str, Decimal] | None:
        """Convert quoted odds into a margin-free prior."""
        if not analysis.market_odds:
            return None
        try:
            probabilities = fair_probabilities(
                [
                    analysis.market_odds["home"],
                    analysis.market_odds["draw"],
                    analysis.market_odds["away"],
                ],
                MarginMethod.SHIN,
            )
        except (ProbabilityError, KeyError):
            return None
        prior = dict(zip(("home", "draw", "away"), probabilities, strict=True))
        analysis.market_probabilities = prior
        return prior

    def _poisson(
        self,
        analysis: MatchAnalysis,
        home_matches: list[HistoricalMatch],
        away_matches: list[HistoricalMatch],
        league: LeagueAverages | None,
    ) -> MatchProbabilities | None:
        """Estimate scoring rates and derive every market."""
        if league is None or not league.is_reliable:
            return None
        assert analysis.home.team_id is not None
        assert analysis.away.team_id is not None

        home_strength = team_strength(
            analysis.home.team_id,
            *_split(home_matches, analysis.home.team_id),
            league,
        )
        away_strength = team_strength(
            analysis.away.team_id,
            *_split(away_matches, analysis.away.team_id),
            league,
        )
        if not (home_strength.is_reliable and away_strength.is_reliable):
            return None

        lambda_home, lambda_away = expected_goals(home_strength, away_strength, league)
        return match_probabilities(lambda_home, lambda_away)

    def _elo(
        self,
        analysis: MatchAnalysis,
        home_matches: list[HistoricalMatch],
        away_matches: list[HistoricalMatch],
        moment: datetime,
    ) -> ComponentEstimate | None:
        """Replay recent results into ratings and predict."""
        assert analysis.home.team_id is not None
        assert analysis.away.team_id is not None

        engine = EloEngine()
        seen: set[int] = set()
        results = []
        for match in home_matches + away_matches:
            if match.id in seen:
                continue
            seen.add(match.id)
            results.append(
                (
                    match.home_team_id,
                    match.away_team_id,
                    match.home_goals,
                    match.away_goals,
                    _as_utc(match.match_date),
                )
            )
        if not results:
            return None
        engine.replay(results)

        analysis.home.elo = round(engine.rating(analysis.home.team_id).rating)
        analysis.away.elo = round(engine.rating(analysis.away.team_id).rating)

        if not engine.is_reliable(analysis.home.team_id, analysis.away.team_id):
            return None

        home_win, draw, away_win = engine.predict(analysis.home.team_id, analysis.away.team_id)
        return ComponentEstimate("elo", {"home": home_win, "draw": draw, "away": away_win})

    def _form(
        self,
        analysis: MatchAnalysis,
        home_matches: list[HistoricalMatch],
        away_matches: list[HistoricalMatch],
        moment: datetime,
    ) -> ComponentEstimate | None:
        """Summarise recent form and predict."""
        assert analysis.home.team_id is not None
        assert analysis.away.team_id is not None

        home_form = summarise(
            analysis.home.team_id,
            _outcomes(home_matches, analysis.home.team_id),
            before=moment,
        )
        away_form = summarise(
            analysis.away.team_id,
            _outcomes(away_matches, analysis.away.team_id),
            before=moment,
        )
        if not (home_form.is_reliable and away_form.is_reliable):
            return None

        home_win, draw, away_win = form_predict(home_form, away_form)
        return ComponentEstimate("form", {"home": home_win, "draw": draw, "away": away_win})

    async def _league_averages(self, moment: datetime) -> LeagueAverages | None:
        """Compute baseline scoring rates from recent history."""
        cutoff = moment - timedelta(days=HISTORY_WINDOW_DAYS)
        result = await self._session.execute(
            select(HistoricalMatch.home_goals, HistoricalMatch.away_goals).where(
                HistoricalMatch.match_date >= cutoff,
                HistoricalMatch.match_date < moment,
            )
        )
        rows = list(result.all())
        if len(rows) < 20:
            return None
        return LeagueAverages(
            home_goals=sum(h for h, _ in rows) / len(rows),
            away_goals=sum(a for _, a in rows) / len(rows),
            matches=len(rows),
        )

    async def supported_teams(self, limit: int = 5) -> int:
        """Return how many canonical teams are on record."""
        result = await self._session.execute(select(Team.id).limit(limit * 1000))
        return len(result.scalars().all())


def _as_utc(value: datetime | date) -> datetime:
    """Return a timezone-aware UTC datetime.

    Accepts a plain date because SQLite returns one for some columns where
    PostgreSQL returns a datetime, and comparing the two raises.
    """
    if not isinstance(value, datetime):
        return datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _split(
    matches: list[HistoricalMatch], team_id: int
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Split a team's matches into home and away ``(scored, conceded)``."""
    home = [(m.home_goals, m.away_goals) for m in matches if m.home_team_id == team_id]
    away = [(m.away_goals, m.home_goals) for m in matches if m.away_team_id == team_id]
    return home, away


def _outcomes(matches: list[HistoricalMatch], team_id: int) -> list[MatchOutcome]:
    """Convert stored matches into form outcomes for one team."""
    result = []
    for match in matches:
        at_home = match.home_team_id == team_id
        scored, conceded = (
            (match.home_goals, match.away_goals)
            if at_home
            else (match.away_goals, match.home_goals)
        )
        result.append(
            MatchOutcome(
                played_at=_as_utc(match.match_date),
                scored=scored,
                conceded=conceded,
                at_home=at_home,
            )
        )
    return result


def _market_odds(event: ProviderEvent) -> dict[str, Decimal] | None:
    """Extract 1X2 odds from a provider event."""
    for market in event.markets:
        prices = {o.name.lower(): o.odds for o in market.outcomes}
        if {"home", "draw", "away"} <= set(prices):
            return {k: prices[k] for k in ("home", "draw", "away")}
    return None
