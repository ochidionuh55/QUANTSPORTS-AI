"""End-to-end backtest runner.

Walks a season of real results in date order, and at each fixture asks: using
only what was knowable *before kickoff*, what would the model have said, and how
does that compare to what the market said?

::

    for each match, chronologically
        build Elo / Poisson / form estimates from matches BEFORE this one
        take the market prior from the reference book's closing odds
        blend, shrink by reliability
        record the forecast alongside the actual result
        THEN apply the result to the models

The order of those last two steps is the whole point. Recording the forecast
before the models see the result is what makes the exercise honest. Reverse it
and the model appears clairvoyant.

**Every fixture produces three forecasts** — home, draw and away — because a
1X2 market is three binary questions, and scoring them separately is what
calibration requires.

**Fixtures are skipped, not guessed.** A match with no reference odds, or with
either side lacking enough history, contributes nothing. Skips are counted and
reported: a run that evaluated 200 of 2000 fixtures is not a validation, and
the skip breakdown says why.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from app.core.logging import get_logger
from app.historical.csv_provider import REFERENCE_BOOKMAKER, TRADEABLE_BOOKMAKER
from app.historical.models import HistoricalMatch, MatchResult
from app.quant.backtest import (
    BacktestResult,
    DatedForecast,
    PromotionDecision,
    assess_for_promotion,
    walk_forward,
)
from app.quant.dixon_coles import DEFAULT_RHO
from app.quant.dixon_coles import match_probabilities as dixon_coles_probabilities
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
from app.quant.probability import MarginMethod, fair_probabilities

logger = get_logger(__name__)

OUTCOME_KEYS = ("home", "draw", "away")

MIN_HISTORY_MATCHES = 8
"""Matches each side needs before a fixture is evaluated.

Below this the models are mostly returning their priors, and including such
fixtures measures the starting values rather than the model.
"""


@dataclass
class RunSummary:
    """What a backtest run covered and what it had to skip."""

    matches_seen: int = 0
    forecasts_made: int = 0
    skipped: Counter[str] = field(default_factory=Counter)

    @property
    def coverage(self) -> float:
        """Fraction of fixtures that produced forecasts."""
        if not self.matches_seen:
            return 0.0
        return (self.forecasts_made / len(OUTCOME_KEYS)) / self.matches_seen

    def summary(self) -> str:
        """Return a one-line human summary."""
        reasons = ", ".join(f"{k}={v}" for k, v in self.skipped.most_common())
        return (
            f"{self.matches_seen} fixtures, "
            f"{self.forecasts_made} forecasts "
            f"({self.coverage:.0%} coverage)" + (f"; skipped: {reasons}" if reasons else "")
        )


@dataclass
class BacktestReport:
    """Everything a run produced."""

    run: RunSummary
    result: BacktestResult
    decision: PromotionDecision
    reference_bookmaker: str
    tradeable_bookmaker: str
    margin_method: str
    reliability: float

    def report(self) -> str:
        """Return a full human-readable verdict."""
        lines = [
            "BACKTEST REPORT",
            f"  data      : {self.run.summary()}",
            f"  reference : {self.reference_bookmaker} "
            f"(margin removed with {self.margin_method})",
            f"  tradeable : {self.tradeable_bookmaker}",
            f"  reliability: {self.reliability:.3f}",
        ]
        if self.result.overall:
            lines.append(f"  scores    : {self.result.overall.summary()}")
            lines.append(f"  consistency: {self.result.consistency:.0%}")
        lines.append("")
        lines.append(self.decision.report())
        return "\n".join(lines)


def _result_vector(match: HistoricalMatch) -> dict[str, int]:
    """Return the realised outcome as three binary labels."""
    result = match.result
    return {
        "home": 1 if result is MatchResult.HOME else 0,
        "draw": 1 if result is MatchResult.DRAW else 0,
        "away": 1 if result is MatchResult.AWAY else 0,
    }


class _TeamHistory:
    """Accumulates per-team results as the walk proceeds.

    Only ever appended to *after* a fixture has been forecast, so a lookup can
    never return information from the match being predicted.
    """

    def __init__(self) -> None:
        self.home_matches: dict[str, list[tuple[int, int]]] = {}
        self.away_matches: dict[str, list[tuple[int, int]]] = {}
        self.form: dict[str, list[MatchOutcome]] = {}
        self.results: list[tuple[int, int]] = []

    def played(self, team: str) -> int:
        """Return how many matches a team has on record."""
        return len(self.home_matches.get(team, [])) + len(self.away_matches.get(team, []))

    def record(self, match: HistoricalMatch) -> None:
        """Add a completed match to both sides' histories."""
        home = match.home_team.source_name
        away = match.away_team.source_name
        moment = match.kickoff or datetime.combine(
            match.match_date, datetime.min.time(), tzinfo=UTC
        )

        self.home_matches.setdefault(home, []).append((match.home_goals, match.away_goals))
        self.away_matches.setdefault(away, []).append((match.away_goals, match.home_goals))
        self.form.setdefault(home, []).append(
            MatchOutcome(moment, match.home_goals, match.away_goals, at_home=True)
        )
        self.form.setdefault(away, []).append(
            MatchOutcome(moment, match.away_goals, match.home_goals, at_home=False)
        )
        self.results.append((match.home_goals, match.away_goals))


def _team_id(name: str) -> int:
    """Return a stable integer id for a team name.

    Elo keys on ids; within a run, a hash of the canonical source name is a
    sufficient and deterministic stand-in.
    """
    return abs(hash(name)) % (10**9)


def build_forecasts(
    matches: list[HistoricalMatch],
    reliability: float = 0.0,
    margin_method: MarginMethod = MarginMethod.SHIN,
    reference_bookmaker: str = REFERENCE_BOOKMAKER,
    min_history: int = MIN_HISTORY_MATCHES,
    weights: dict[str, float] | None = None,
    allowed_components: frozenset[str] | None = None,
    goal_model: str = "poisson",
    rho: float = DEFAULT_RHO,
) -> tuple[list[DatedForecast], RunSummary]:
    """Replay matches chronologically, forecasting each before recording it.

    Args:
        matches: Completed matches, any order; sorted internally by date.
        reliability: How far the model may move the market prior, in ``[0, 1]``.
        margin_method: Strategy for converting reference odds into a prior.
        reference_bookmaker: Which book supplies the prior.
        min_history: Matches each side needs before being forecast.
        weights: Ensemble component weights.
        allowed_components: Restrict to these components, e.g.
            ``frozenset({"poisson"})``. Used to attribute skill to individual
            models rather than only scoring the ensemble, since an ensemble
            that beats the market tells you nothing about which part earned it.

    Returns:
        ``(forecasts, run_summary)``.
    """
    ordered = sorted(matches, key=lambda m: m.match_date)
    history = _TeamHistory()
    elo = EloEngine()
    forecasts: list[DatedForecast] = []
    run = RunSummary()

    for match in ordered:
        run.matches_seen += 1
        home = match.home_team.source_name
        away = match.away_team.source_name
        kickoff = match.kickoff or datetime.combine(
            match.match_date, datetime.min.time(), tzinfo=UTC
        )

        prior = _market_prior(match, reference_bookmaker, margin_method)
        if prior is None:
            run.skipped["no_reference_odds"] += 1
            history.record(match)
            elo.record(
                _team_id(home),
                _team_id(away),
                match.home_goals,
                match.away_goals,
                kickoff,
            )
            continue

        if min(history.played(home), history.played(away)) < min_history:
            run.skipped["insufficient_history"] += 1
            history.record(match)
            elo.record(
                _team_id(home),
                _team_id(away),
                match.home_goals,
                match.away_goals,
                kickoff,
            )
            continue

        components, _ = components_and_goals(
            history, elo, home, away, kickoff, goal_model=goal_model, rho=rho
        )
        if components is not None and allowed_components is not None:
            components = [c for c in components if c.name in allowed_components] or None
        if components is None:
            run.skipped["no_reliable_component"] += 1
        else:
            try:
                ensemble = build_ensemble(components, prior, reliability, weights)
            except EnsembleError:
                run.skipped["ensemble_error"] += 1
            else:
                actual = _result_vector(match)
                for key in OUTCOME_KEYS:
                    forecasts.append(
                        DatedForecast(
                            occurred_at=kickoff,
                            model_probability=float(ensemble.posterior[key]),
                            market_probability=float(prior[key]),
                            outcome=actual[key],
                            label=f"{home} v {away} [{key}]",
                        )
                    )
                run.forecasts_made += len(OUTCOME_KEYS)

        # Only now does the model learn what happened.
        history.record(match)
        elo.record(_team_id(home), _team_id(away), match.home_goals, match.away_goals, kickoff)

    logger.info("backtest.forecasts_built", **{"summary": run.summary()})
    return forecasts, run


def _market_prior(
    match: HistoricalMatch, bookmaker: str, method: MarginMethod
) -> dict[str, Decimal] | None:
    """Convert a bookmaker's odds into a margin-free prior."""
    quoted = match.odds_for(bookmaker)
    if quoted is None:
        return None
    try:
        probabilities = fair_probabilities(list(quoted), method)
    except Exception:  # noqa: BLE001 - a bad price must skip, not abort the run
        return None
    return dict(zip(OUTCOME_KEYS, probabilities, strict=True))


def _components(
    history: _TeamHistory,
    elo: EloEngine,
    home: str,
    away: str,
    kickoff: datetime,
) -> list[ComponentEstimate] | None:
    """Build model estimates from history preceding this fixture."""
    components, _ = components_and_goals(history, elo, home, away, kickoff)
    return components


def components_and_goals(
    history: _TeamHistory,
    elo: EloEngine,
    home: str,
    away: str,
    kickoff: datetime,
    goal_model: str = "poisson",
    rho: float = DEFAULT_RHO,
) -> tuple[list[ComponentEstimate] | None, MatchProbabilities | None]:
    """Return model estimates and the goal model behind them.

    Split out so the backfill can publish over/under and both-teams-to-score
    from the same distribution the live path uses, rather than deriving them a
    second way and risking two answers to the same question.

    Args:
        goal_model: ``poisson`` or ``dixon_coles``. The component keeps the
            name ``poisson`` either way, so ensemble weights configured for one
            apply unchanged to the other and the comparison stays like-for-like.
        rho: Dixon-Coles correlation parameter, ignored for plain Poisson.
    """
    components: list[ComponentEstimate] = []
    goals: MatchProbabilities | None = None

    if len(history.results) >= 20:
        averages = LeagueAverages(
            home_goals=sum(h for h, _ in history.results) / len(history.results),
            away_goals=sum(a for _, a in history.results) / len(history.results),
            matches=len(history.results),
        )
        home_strength = team_strength(
            _team_id(home),
            history.home_matches.get(home, []),
            history.away_matches.get(home, []),
            averages,
        )
        away_strength = team_strength(
            _team_id(away),
            history.home_matches.get(away, []),
            history.away_matches.get(away, []),
            averages,
        )
        if home_strength.is_reliable and away_strength.is_reliable:
            lambda_home, lambda_away = expected_goals(home_strength, away_strength, averages)
            poisson = (
                dixon_coles_probabilities(lambda_home, lambda_away, rho)
                if goal_model == "dixon_coles"
                else match_probabilities(lambda_home, lambda_away)
            )
            goals = poisson
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

    if elo.is_reliable(_team_id(home), _team_id(away), moment=kickoff):
        home_win, draw, away_win = elo.predict(_team_id(home), _team_id(away), moment=kickoff)
        components.append(
            ComponentEstimate("elo", {"home": home_win, "draw": draw, "away": away_win})
        )

    home_form = summarise(_team_id(home), history.form.get(home, []), before=kickoff, at_home=True)
    away_form = summarise(_team_id(away), history.form.get(away, []), before=kickoff, at_home=False)
    if home_form.is_reliable and away_form.is_reliable:
        home_win, draw, away_win = form_predict(home_form, away_form)
        components.append(
            ComponentEstimate("form", {"home": home_win, "draw": draw, "away": away_win})
        )

    return (components or None), goals


def run_backtest(
    matches: list[HistoricalMatch],
    model_version: str,
    reliability: float = 0.0,
    margin_method: MarginMethod = MarginMethod.SHIN,
    folds: int = 4,
    min_train: int = 300,
    weights: dict[str, float] | None = None,
    reference_bookmaker: str = REFERENCE_BOOKMAKER,
    tradeable_bookmaker: str = TRADEABLE_BOOKMAKER,
) -> BacktestReport:
    """Run a full backtest and assess the model for promotion.

    Args:
        matches: Completed matches with closing odds.
        model_version: Identifier recorded with the decision.
        reliability: Model influence over the prior. Zero means the model is
            evaluated as pure market, which is the correct null baseline.
        margin_method: Margin removal strategy for the prior.
        folds: Walk-forward windows.
        min_train: Forecasts before the first test window.
        weights: Ensemble component weights.
        reference_bookmaker: Book supplying the prior.
        tradeable_bookmaker: Book whose prices would be bet into.

    Returns:
        A report containing coverage, scores and the promotion decision.

    Raises:
        ValueError: If too few forecasts were produced to evaluate.
    """
    forecasts, run = build_forecasts(
        matches,
        reliability=reliability,
        margin_method=margin_method,
        reference_bookmaker=reference_bookmaker,
        weights=weights,
    )

    if len(forecasts) < min_train + folds:
        raise ValueError(
            f"Only {len(forecasts)} forecasts were produced from "
            f"{run.matches_seen} fixtures, which is not enough to evaluate. "
            f"{run.summary()}"
        )

    result = walk_forward(forecasts, folds=folds, min_train=min_train)
    decision = assess_for_promotion(model_version, result)

    return BacktestReport(
        run=run,
        result=result,
        decision=decision,
        reference_bookmaker=reference_bookmaker,
        tradeable_bookmaker=tradeable_bookmaker,
        margin_method=str(margin_method),
        reliability=reliability,
    )


def sweep_reliability(
    matches: list[HistoricalMatch],
    candidates: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5),
    margin_method: MarginMethod = MarginMethod.SHIN,
    folds: int = 4,
    min_train: int = 300,
    reference_bookmaker: str = REFERENCE_BOOKMAKER,
) -> list[tuple[float, float, float]]:
    """Measure how model influence affects skill.

    Reliability of zero is the market baseline by construction, so its skill is
    zero. If skill does not rise above it, the model carries no information the
    market lacks, and the honest conclusion is to ship nothing.

    Returns:
        ``(reliability, brier_skill, log_loss_skill)`` per candidate.
    """
    findings: list[tuple[float, float, float]] = []
    for reliability in candidates:
        try:
            report = run_backtest(
                matches,
                model_version=f"sweep-{reliability}",
                reliability=reliability,
                margin_method=margin_method,
                folds=folds,
                min_train=min_train,
                reference_bookmaker=reference_bookmaker,
            )
        except ValueError as exc:
            # Surfaced, not swallowed. Silently skipping made a run that
            # produced no forecasts at all — usually the wrong bookmaker name —
            # look identical to a run that simply found nothing.
            logger.warning(
                "backtest.sweep_point_failed",
                reliability=reliability,
                reason=str(exc),
            )
            raise
        if report.result.overall is None:
            continue
        findings.append(
            (
                reliability,
                report.result.overall.brier_skill,
                report.result.overall.log_loss_skill,
            )
        )
    return findings
