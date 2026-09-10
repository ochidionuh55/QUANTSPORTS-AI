"""Prediction settlement and performance tracking.

Every published analysis is compared against the actual result and stored
permanently. The system judges itself continuously, on every fixture, whether
the day went well or badly.

**Why a snapshot, not a join.** Settlement copies the probabilities as
published. The stored analysis is overwritten as odds move and pruned once the
fixture finishes, so a join would lose the forecast that a result belongs to.

**Why the market is stored alongside.** A Brier score of 0.20 is
uninterpretable alone. Against a market baseline of 0.19 it means the model is
worse than the price it would have been bet into. Both are scored on identical
fixtures, so the comparison is honest.

**Why accuracy is reported but never leads.** "The favourite won 62% of the
time" sounds like skill and usually is not — favourites win about that often by
definition. Brier and log loss are the metrics that can distinguish a good
forecaster from a lucky one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import SettledPrediction, StoredAnalysis
from app.providers.errors import ProviderError
from app.quant.metrics import brier_score, calibration, log_loss
from app.services.daily_scan import AnalysisRepository

logger = get_logger(__name__)

SETTLE_AFTER_HOURS = 2.5
"""How long after kickoff a fixture is assumed finished.

Ninety minutes plus stoppage, half-time and a margin for delayed feeds. Too
short and matches settle as draws mid-game; too long and the analysis has
already been pruned.
"""

MODEL_VERSION = "ensemble-v1-market-prior"
"""Recorded on every settlement.

A performance history spanning a model change is uninterpretable without it,
and the change is exactly when someone will want to compare.
"""


@dataclass(frozen=True)
class FinalScore:
    """A completed match result."""

    provider_event_id: str
    home_goals: int
    away_goals: int

    @property
    def result(self) -> str:
        """Return ``home``, ``draw`` or ``away``."""
        if self.home_goals > self.away_goals:
            return "home"
        if self.home_goals < self.away_goals:
            return "away"
        return "draw"


class ResultsSource(Protocol):
    """Anything that can report finished scores."""

    async def get_results(self, fixture_ids: list[str]) -> list[FinalScore]:
        """Return final scores for the given fixtures."""
        ...


@dataclass
class SettlementReport:
    """What one settlement run did."""

    candidates: int = 0
    settled: int = 0
    unavailable: int = 0
    already_settled: int = 0
    errors: int = 0

    def summary(self) -> str:
        """Return a one-line human summary."""
        return (
            f"{self.candidates} due, {self.settled} settled, "
            f"{self.already_settled} already done, "
            f"{self.unavailable} no result yet, {self.errors} errors"
        )


class SettlementService:
    """Settles finished fixtures against their published analysis."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def settle(self, source: ResultsSource, now: datetime | None = None) -> SettlementReport:
        """Compare finished fixtures against what we predicted.

        Args:
            source: Provider able to report final scores.
            now: Clock override, for tests.
        """
        moment = now or datetime.now(UTC)
        report = SettlementReport()

        due = await self._due(moment)
        report.candidates = len(due)
        if not due:
            return report

        pending: list[StoredAnalysis] = []
        for record in due:
            if await self._is_settled(record):
                report.already_settled += 1
                continue
            pending.append(record)

        if not pending:
            return report

        try:
            scores = await source.get_results([r.provider_event_id for r in pending])
        except ProviderError as exc:
            logger.warning("settlement.results_failed", error=str(exc))
            report.errors += 1
            return report

        by_id = {score.provider_event_id: score for score in scores}
        for record in pending:
            score = by_id.get(record.provider_event_id)
            if score is None:
                report.unavailable += 1
                continue
            try:
                self._record(record, score, moment)
                report.settled += 1
            except (KeyError, TypeError, ValueError) as exc:
                # A malformed stored analysis must not stop the rest settling.
                logger.warning(
                    "settlement.record_failed",
                    fixture=record.provider_event_id,
                    error=str(exc),
                )
                report.errors += 1

        await self._session.flush()
        logger.info("settlement.completed", summary=report.summary())
        return report

    async def _due(self, moment: datetime) -> list[StoredAnalysis]:
        """Return analysed fixtures that should have finished by now."""
        cutoff = moment - timedelta(hours=SETTLE_AFTER_HOURS)
        result = await self._session.execute(
            select(StoredAnalysis).where(StoredAnalysis.kickoff <= cutoff)
        )
        # Only fixtures we actually forecast can be scored. An unmodellable
        # fixture has nothing to compare a result against.
        return [r for r in result.scalars().all() if r.markets]

    async def _is_settled(self, record: StoredAnalysis) -> bool:
        """Whether this fixture has already been settled."""
        result = await self._session.execute(
            select(SettledPrediction.id).where(
                SettledPrediction.provider_name == record.provider_name,
                SettledPrediction.provider_event_id == record.provider_event_id,
            )
        )
        return result.first() is not None

    def _record(self, record: StoredAnalysis, score: FinalScore, moment: datetime) -> None:
        """Write one settlement row."""
        markets = _markets_of(record)
        one_x_two = markets.get("1X2", {})
        home = float(one_x_two["Home"])
        draw = float(one_x_two["Draw"])
        away = float(one_x_two["Away"])

        over = markets.get("Goals", {}).get("Over 2.5")
        btts = markets.get("Both teams to score", {}).get("Yes")

        probabilities = {"home": home, "draw": draw, "away": away}
        favourite = max(probabilities, key=lambda k: probabilities[k])
        total_goals = score.home_goals + score.away_goals
        raw_market = record.market_probabilities or {}
        market: dict[str, object] = dict(raw_market) if isinstance(raw_market, dict) else {}

        self._session.add(
            SettledPrediction(
                provider_name=record.provider_name,
                provider_event_id=record.provider_event_id,
                home_name=record.home_name,
                away_name=record.away_name,
                competition=record.competition,
                kickoff=record.kickoff,
                coverage=record.coverage,
                model_version=MODEL_VERSION,
                components_used=list(record.components_used or []),
                predicted_home=home,
                predicted_draw=draw,
                predicted_away=away,
                predicted_over_2_5=float(over) if over is not None else None,
                predicted_btts=float(btts) if btts is not None else None,
                expected_home_goals=record.expected_home_goals,
                expected_away_goals=record.expected_away_goals,
                market_home=_maybe_float(market.get("home")),
                market_draw=_maybe_float(market.get("draw")),
                market_away=_maybe_float(market.get("away")),
                home_goals=score.home_goals,
                away_goals=score.away_goals,
                actual_result=score.result,
                predicted_favourite=favourite,
                favourite_won=favourite == score.result,
                over_2_5_hit=total_goals > 2.5,
                btts_hit=score.home_goals > 0 and score.away_goals > 0,
                settled_at=moment,
            )
        )


@dataclass
class PerformanceSummary:
    """Measured performance over a set of settled predictions."""

    sample: int = 0
    favourite_accuracy: float | None = None
    brier: float | None = None
    market_brier: float | None = None
    log_loss_value: float | None = None
    market_log_loss: float | None = None
    calibration_error: float | None = None
    calibration_noise_floor: float | None = None
    over_2_5_accuracy: float | None = None
    over_2_5_brier: float | None = None
    btts_accuracy: float | None = None
    btts_brier: float | None = None
    breakdown: dict[str, dict[str, float | int]] = field(default_factory=dict)

    @property
    def brier_skill(self) -> float | None:
        """Fractional improvement over the market, or ``None`` without a baseline."""
        if self.brier is None or not self.market_brier:
            return None
        return (self.market_brier - self.brier) / self.market_brier

    @property
    def verdict(self) -> str:
        """Plain-language reading of the numbers."""
        if self.sample < 50:
            return (
                f"Only {self.sample} settled predictions. Too few to say "
                "anything meaningful — several hundred are needed before a "
                "difference this small is distinguishable from chance."
            )
        skill = self.brier_skill
        if skill is None:
            return (
                "No market baseline recorded for these fixtures, so the score "
                "cannot be interpreted."
            )
        if skill > 0.005:
            return (
                f"Model is scoring {skill:+.2%} better than the market on "
                f"{self.sample} predictions. Encouraging, but confirm over a "
                "longer period before trusting it."
            )
        if skill < -0.005:
            return (
                f"Model is scoring {skill:+.2%} against the market. The market "
                "is the better forecaster on this sample."
            )
        return (
            f"Model and market are within {abs(skill):.2%} of each other on "
            f"{self.sample} predictions — no measurable difference."
        )


class PerformanceService:
    """Computes performance statistics from settled predictions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def summarise(
        self,
        since: datetime | None = None,
        competition: str | None = None,
        coverage: str | None = None,
        model_version: str | None = None,
        source: str | None = "live",
    ) -> PerformanceSummary:
        """Summarise performance over a filtered set of settlements.

        Args:
            source: ``live``, ``backfill``, or ``None`` for both. Defaults to
                live, because blending the two is almost never what a caller
                wants and doing it accidentally would misrepresent both.
        """
        statement = select(SettledPrediction)
        if source is not None:
            statement = statement.where(SettledPrediction.source == source)
        if since is not None:
            statement = statement.where(SettledPrediction.kickoff >= since)
        if competition is not None:
            statement = statement.where(SettledPrediction.competition == competition)
        if coverage is not None:
            statement = statement.where(SettledPrediction.coverage == coverage)
        if model_version is not None:
            statement = statement.where(SettledPrediction.model_version == model_version)

        rows = list((await self._session.execute(statement)).scalars().all())
        return self._summarise_rows(rows)

    def _summarise_rows(self, rows: list[SettledPrediction]) -> PerformanceSummary:
        """Compute every metric from a list of settlements."""
        summary = PerformanceSummary(sample=len(rows))
        if not rows:
            return summary

        summary.favourite_accuracy = sum(1 for r in rows if r.favourite_won) / len(rows)

        # 1X2 is scored as three binary forecasts, which is what calibration
        # requires and what makes the market comparison like-for-like.
        model_probabilities: list[float] = []
        market_probabilities: list[float] = []
        outcomes: list[int] = []
        for row in rows:
            for name, predicted, quoted in (
                ("home", row.predicted_home, row.market_home),
                ("draw", row.predicted_draw, row.market_draw),
                ("away", row.predicted_away, row.market_away),
            ):
                model_probabilities.append(_clamp(predicted))
                outcomes.append(1 if row.actual_result == name else 0)
                market_probabilities.append(
                    _clamp(quoted) if quoted is not None else _clamp(predicted)
                )

        summary.brier = brier_score(model_probabilities, outcomes)
        summary.log_loss_value = log_loss(model_probabilities, outcomes)

        if any(r.market_home is not None for r in rows):
            summary.market_brier = brier_score(market_probabilities, outcomes)
            summary.market_log_loss = log_loss(market_probabilities, outcomes)

        report = calibration(model_probabilities, outcomes)
        summary.calibration_error = report.expected_calibration_error
        summary.calibration_noise_floor = report.noise_floor

        summary.over_2_5_accuracy, summary.over_2_5_brier = _binary_market(
            [(r.predicted_over_2_5, r.over_2_5_hit) for r in rows]
        )
        summary.btts_accuracy, summary.btts_brier = _binary_market(
            [(r.predicted_btts, r.btts_hit) for r in rows]
        )
        return summary

    async def by_competition(
        self, since: datetime | None = None, source: str | None = "live"
    ) -> dict[str, PerformanceSummary]:
        """Return performance split by league."""
        return await self._grouped(SettledPrediction.competition, since, source)

    async def by_coverage(
        self, since: datetime | None = None, source: str | None = "live"
    ) -> dict[str, PerformanceSummary]:
        """Return performance split by coverage grade.

        Fully modelled fixtures should score better than partial ones. If they
        do not, the coverage grading is not measuring what it claims.
        """
        return await self._grouped(SettledPrediction.coverage, since, source)

    async def by_model_version(
        self, since: datetime | None = None, source: str | None = "live"
    ) -> dict[str, PerformanceSummary]:
        """Return performance split by model version."""
        return await self._grouped(SettledPrediction.model_version, since, source)

    async def _grouped(
        self, column: object, since: datetime | None, source: str | None = "live"
    ) -> dict[str, PerformanceSummary]:
        """Group settlements by a column and summarise each group."""
        statement = select(SettledPrediction)
        if source is not None:
            statement = statement.where(SettledPrediction.source == source)
        if since is not None:
            statement = statement.where(SettledPrediction.kickoff >= since)
        rows = list((await self._session.execute(statement)).scalars().all())

        buckets: dict[str, list[SettledPrediction]] = {}
        for row in rows:
            key = str(getattr(row, column.key) or "unknown")  # type: ignore[attr-defined]
            buckets.setdefault(key, []).append(row)
        return {k: self._summarise_rows(v) for k, v in sorted(buckets.items())}

    async def periods(
        self, now: datetime | None = None, source: str | None = "live"
    ) -> dict[str, PerformanceSummary]:
        """Return daily, weekly, monthly and all-time performance."""
        moment = now or datetime.now(UTC)
        windows = {
            "today": moment - timedelta(days=1),
            "this week": moment - timedelta(days=7),
            "this month": moment - timedelta(days=30),
            "all time": None,
        }
        return {label: await self.summarise(since=since) for label, since in windows.items()}

    async def counts_by_source(self) -> dict[str, int]:
        """Return how many settlements exist per source."""
        result = await self._session.execute(select(SettledPrediction.source))
        counts: dict[str, int] = {}
        for value in result.scalars().all():
            counts[value] = counts.get(value, 0) + 1
        return counts

    async def recent(self, limit: int = 10) -> list[SettledPrediction]:
        """Return the most recently settled predictions."""
        result = await self._session.execute(
            select(SettledPrediction).order_by(SettledPrediction.kickoff.desc()).limit(limit)
        )
        return list(result.scalars().all())


def _markets_of(record: StoredAnalysis) -> dict[str, dict[str, str]]:
    """Return a typed view of a record's stored markets.

    The column is JSON, so its static type is ``object``; narrowing here keeps
    the cast in one place rather than at every lookup.
    """
    raw = record.markets or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): {str(k): str(v) for k, v in outcomes.items()}
        for name, outcomes in raw.items()
        if isinstance(outcomes, dict)
    }


def _clamp(value: float) -> float:
    """Keep a probability strictly inside ``(0, 1)`` for scoring."""
    return min(max(value, 1e-9), 1 - 1e-9)


def _maybe_float(value: object) -> float | None:
    """Parse a stored probability string, tolerating absence."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _binary_market(
    pairs: list[tuple[float | None, bool | None]],
) -> tuple[float | None, float | None]:
    """Return accuracy and Brier score for a binary market.

    Accuracy is whether the more likely side happened, which is the intuitive
    reading. Brier is the one that actually measures forecast quality.
    """
    usable = [(p, h) for p, h in pairs if p is not None and h is not None]
    if not usable:
        return None, None
    probabilities = [_clamp(p) for p, _ in usable]
    outcomes = [1 if h else 0 for _, h in usable]
    correct = sum(
        1
        for p, h in usable
        if (p >= 0.5) == h  # type: ignore[operator]
    )
    return correct / len(usable), brier_score(probabilities, outcomes)


async def pending_settlement_count(session: AsyncSession, now: datetime | None = None) -> int:
    """Return how many analysed fixtures are awaiting settlement."""
    service = SettlementService(session)
    return len(await service._due(now or datetime.now(UTC)))


__all__ = [
    "MODEL_VERSION",
    "SETTLE_AFTER_HOURS",
    "AnalysisRepository",
    "FinalScore",
    "PerformanceService",
    "PerformanceSummary",
    "ResultsSource",
    "SettlementReport",
    "SettlementService",
    "pending_settlement_count",
]
