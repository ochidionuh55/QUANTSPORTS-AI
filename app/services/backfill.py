"""Historical prediction backfill.

Replays completed matches in date order, forecasting each one using only what
was knowable before kickoff, then settles it against the known result. The
output lands in the same ``settled_predictions`` table as live predictions,
marked ``source="backfill"``.

**Reuses the backtest harness rather than duplicating it.** ``_TeamHistory``,
``components_and_goals`` and ``_market_prior`` are the same functions the
validation harness uses, so a backfilled prediction is produced by exactly the
code that was validated. A second prediction path would eventually disagree
with the first, and the disagreement would be invisible.

**The leakage discipline is the whole point.** A fixture is forecast, then
recorded into history and Elo — never the reverse. Getting that backwards makes
a worthless model look clairvoyant, and the mistake leaves no trace in the
output.

**Backfill is never blended with live results.** Same engine and same
discipline, but produced knowing which fixtures exist and which leagues had
coverage. Averaging the two would distort both, so they are always queried
separately.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import SettledPrediction
from app.historical.csv_provider import REFERENCE_BOOKMAKER
from app.historical.models import HistoricalMatch
from app.quant.backtest_runner import (
    MIN_HISTORY_MATCHES,
    _market_prior,
    _team_id,
    _TeamHistory,
    components_and_goals,
)
from app.quant.elo import EloEngine
from app.quant.ensemble import EnsembleError, build_ensemble
from app.quant.probability import MarginMethod
from app.services.settlement import MODEL_VERSION

logger = get_logger(__name__)

BACKFILL_SOURCE = "backfill"
BACKFILL_PROVIDER = "csv_historical"

RELIABILITY = 0.0
"""Model influence over the market prior.

Zero, matching the live path. The models have not passed the validation gate,
so they do not move the price — and a backfill run with a different setting
would not describe the system users actually see.
"""


@dataclass
class BackfillReport:
    """What one backfill run produced."""

    competition: str = ""
    matches_seen: int = 0
    predicted: int = 0
    stored: int = 0
    already_present: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        """Record a skipped fixture."""
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def summary(self) -> str:
        """Return a one-line human summary."""
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(self.skipped.items()))
        return (
            f"{self.competition}: {self.matches_seen} matches, "
            f"{self.stored} stored, {self.already_present} already present"
            + (f"; skipped: {reasons}" if reasons else "")
        )


class BackfillService:
    """Replays history into settled predictions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def backfill(
        self,
        matches: list[HistoricalMatch],
        competition_name: str,
        margin_method: MarginMethod = MarginMethod.SHIN,
        reference_bookmaker: str = REFERENCE_BOOKMAKER,
        min_history: int = MIN_HISTORY_MATCHES,
        progress: Callable[[int, int], None] | None = None,
    ) -> BackfillReport:
        """Forecast and settle every match in date order.

        Args:
            matches: Completed matches, any order; sorted internally.
            competition_name: Label stored on each prediction.
            margin_method: Strategy for turning odds into a market prior.
            reference_bookmaker: Which book supplies the prior.
            min_history: Matches each side needs before being forecast.
            progress: Optional callback receiving ``(done, total)``.

        Returns:
            A report of what was stored and what was skipped.
        """
        ordered = sorted(matches, key=lambda m: m.match_date)
        report = BackfillReport(competition=competition_name)
        history = _TeamHistory()
        elo = EloEngine()

        existing = await self._existing_ids([m.external_id for m in ordered])
        total = len(ordered)

        for index, match in enumerate(ordered, start=1):
            report.matches_seen += 1
            if progress is not None and index % 250 == 0:
                progress(index, total)

            home = match.home_team.source_name
            away = match.away_team.source_name
            kickoff = match.kickoff or datetime.combine(
                match.match_date, datetime.min.time(), tzinfo=UTC
            )

            if match.external_id in existing:
                report.already_present += 1
                self._learn(history, elo, match, home, away, kickoff)
                continue

            prior = _market_prior(match, reference_bookmaker, margin_method)
            if prior is None:
                report.skip("no_reference_odds")
                self._learn(history, elo, match, home, away, kickoff)
                continue

            if min(history.played(home), history.played(away)) < min_history:
                report.skip("insufficient_history")
                self._learn(history, elo, match, home, away, kickoff)
                continue

            components, goal_model = components_and_goals(history, elo, home, away, kickoff)
            if components is None:
                report.skip("no_reliable_component")
                self._learn(history, elo, match, home, away, kickoff)
                continue

            try:
                ensemble = build_ensemble(components, prior, RELIABILITY)
            except EnsembleError:
                report.skip("ensemble_error")
                self._learn(history, elo, match, home, away, kickoff)
                continue

            self._store(
                match,
                competition_name,
                ensemble.posterior,
                prior,
                goal_model,
                tuple(ensemble.components_used),
                kickoff,
            )
            report.predicted += 1
            report.stored += 1

            # Only now does the model learn what happened. Reversing these two
            # lines is the single mistake that would invalidate everything.
            self._learn(history, elo, match, home, away, kickoff)

        await self._session.flush()
        logger.info("backfill.completed", summary=report.summary())
        return report

    @staticmethod
    def _learn(
        history: _TeamHistory,
        elo: EloEngine,
        match: HistoricalMatch,
        home: str,
        away: str,
        kickoff: datetime,
    ) -> None:
        """Add a completed match to the models' knowledge."""
        history.record(match)
        elo.record(_team_id(home), _team_id(away), match.home_goals, match.away_goals, kickoff)

    async def _existing_ids(self, external_ids: list[str]) -> set[str]:
        """Return which fixtures are already backfilled.

        Checked in one query rather than per match: re-running a backfill is
        the normal case, and a query per fixture would make the common path the
        slowest one.
        """
        if not external_ids:
            return set()
        found: set[str] = set()
        for start in range(0, len(external_ids), 500):
            batch = external_ids[start : start + 500]
            result = await self._session.execute(
                select(SettledPrediction.provider_event_id).where(
                    SettledPrediction.provider_name == BACKFILL_PROVIDER,
                    SettledPrediction.provider_event_id.in_(batch),
                )
            )
            found.update(result.scalars().all())
        return found

    def _store(
        self,
        match: HistoricalMatch,
        competition_name: str,
        posterior: dict[str, Decimal],
        prior: dict[str, Decimal],
        goal_model: object,
        components: tuple[str, ...],
        kickoff: datetime,
    ) -> None:
        """Write one backfilled prediction with its known outcome."""
        home = float(posterior["home"])
        draw = float(posterior["draw"])
        away = float(posterior["away"])
        probabilities = {"home": home, "draw": draw, "away": away}
        favourite = max(probabilities, key=lambda k: probabilities[k])

        actual = (
            "home"
            if match.home_goals > match.away_goals
            else ("away" if match.home_goals < match.away_goals else "draw")
        )
        total_goals = match.home_goals + match.away_goals

        over = btts = expected_home = expected_away = None
        if goal_model is not None:
            over = float(goal_model.over_under.get("over_2.5", 0))  # type: ignore[attr-defined]
            btts = float(goal_model.both_teams_score)  # type: ignore[attr-defined]
            expected_home = round(goal_model.lambda_home, 2)  # type: ignore[attr-defined]
            expected_away = round(goal_model.lambda_away, 2)  # type: ignore[attr-defined]

        self._session.add(
            SettledPrediction(
                provider_name=BACKFILL_PROVIDER,
                provider_event_id=match.external_id,
                source=BACKFILL_SOURCE,
                home_name=match.home_team.source_name,
                away_name=match.away_team.source_name,
                competition=competition_name,
                kickoff=kickoff,
                coverage="fully_modelled" if len(components) >= 3 else "partially_modelled",
                model_version=MODEL_VERSION,
                components_used=list(components),
                predicted_home=home,
                predicted_draw=draw,
                predicted_away=away,
                predicted_over_2_5=over,
                predicted_btts=btts,
                expected_home_goals=expected_home,
                expected_away_goals=expected_away,
                market_home=float(prior["home"]),
                market_draw=float(prior["draw"]),
                market_away=float(prior["away"]),
                home_goals=match.home_goals,
                away_goals=match.away_goals,
                actual_result=actual,
                predicted_favourite=favourite,
                favourite_won=favourite == actual,
                over_2_5_hit=total_goals > 2.5,
                btts_hit=match.home_goals > 0 and match.away_goals > 0,
                settled_at=datetime.now(UTC),
            )
        )
