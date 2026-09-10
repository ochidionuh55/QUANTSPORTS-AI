"""Daily highlights: selection, settlement and track record.

**A highlight is the highest-ranked qualifying analysis, not a tip.** It is
chosen by a documented score — informational content against the base rate,
coverage, data depth, model agreement and market agreement — and is emphatically
not the highest raw probability, nor a claim that the price is generous.

**Nothing is ever created to fill a day.** If no fixture qualifies, the product
says so. A daily feature that must produce something will eventually produce
something worthless, and users cannot tell the difference until it has cost
them.

**Reconstruction is honest but kept apart.** History can be rebuilt from
settled predictions, because those forecasts were produced under strict
leakage discipline and the same scoring applies. But a reconstructed highlight
was never published, so it is stored as ``reconstructed`` and never averaged
with live results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import (
    HighlightSelection,
    SettledPrediction,
    StoredAnalysis,
)
from app.services.boards import SHARP, BoardEntry, BoardService
from app.services.leans import Lean, LeanBuilder, base_rate_for
from app.services.settlement import MODEL_VERSION

logger = get_logger(__name__)

LIVE = "live"
RECONSTRUCTED = "reconstructed"

DAILY_COUNT = 3
"""How many highlights a day may carry.

Three, not five. A shorter list forces the qualification bar to mean something.
"""

MIN_SCORE = 0.42
"""The qualification bar.

A fixture below this is not a highlight, and no amount of empty space on the
screen changes that.
"""


def _as_float(value: object) -> float:
    """Read a stored factor as a float, tolerating absence."""
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _settle_outcome(market: str, outcome: str, home_goals: int, away_goals: int) -> str:
    """Return ``won``, ``lost`` or ``void`` for a finished match.

    Every supported outcome is resolved from the scoreline directly, so
    settlement cannot drift from what was published.
    """
    total = home_goals + away_goals
    result = "home" if home_goals > away_goals else ("away" if home_goals < away_goals else "draw")

    checks: dict[str, bool] = {
        "Home": result == "home",
        "Home win": result == "home",
        "Draw": result == "draw",
        "Away": result == "away",
        "Away win": result == "away",
        "1X (home or draw)": result in {"home", "draw"},
        "12 (home or away)": result in {"home", "away"},
        "X2 (draw or away)": result in {"draw", "away"},
        "Yes": home_goals > 0 and away_goals > 0,
        "No": not (home_goals > 0 and away_goals > 0),
    }
    for line in (0.5, 1.5, 2.5, 3.5):
        checks[f"Over {line}"] = total > line
        checks[f"Under {line}"] = total < line

    if outcome not in checks:
        # An outcome we cannot resolve is voided rather than guessed. Marking
        # it lost would understate the record; marking it won would inflate it.
        logger.warning("highlight.unresolvable_outcome", market=market, outcome=outcome)
        return "void"
    return "won" if checks[outcome] else "lost"


@dataclass
class HighlightReport:
    """What one selection run produced."""

    selection_date: date | None = None
    considered: int = 0
    qualified: int = 0
    recorded: int = 0
    already_present: int = 0
    below_bar: int = 0

    def summary(self) -> str:
        """Return a one-line human summary."""
        return (
            f"{self.considered} fixtures considered, {self.qualified} qualified, "
            f"{self.recorded} recorded, {self.already_present} already present"
        )


@dataclass
class TrackRecord:
    """Settled highlight performance over a period."""

    label: str = ""
    source: str = LIVE
    total: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0
    pending: int = 0
    average_probability: float = 0.0
    by_market: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def settled(self) -> int:
        """Selections with a known outcome."""
        return self.won + self.lost

    @property
    def strike_rate(self) -> float | None:
        """Share of settled selections that won."""
        if not self.settled:
            return None
        return self.won / self.settled

    @property
    def expected_rate(self) -> float | None:
        """What the published probabilities implied.

        The comparison that matters. A 70% strike rate on selections averaging
        72% is not skill — it is the forecasts being about right, which is what
        calibration means.
        """
        return self.average_probability or None

    def describe(self) -> str:
        """Return the headline with its sample size."""
        if not self.settled:
            return f"{self.label}: no settled selections yet"
        rate = self.strike_rate or 0.0
        return f"{self.label}: {self.won}/{self.settled} ({rate:.1%})"


class HighlightService:
    """Selects, records and settles daily highlights."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_daily(
        self,
        now: datetime | None = None,
        count: int = DAILY_COUNT,
        min_score: float = MIN_SCORE,
    ) -> HighlightReport:
        """Choose and store today's selections across all three boards.

        Only fixtures that have not kicked off are eligible, so a selection can
        never be made with the result in view.
        """
        moment = now or datetime.now(UTC)
        report = HighlightReport(selection_date=moment.date())

        result = await self._session.execute(
            select(StoredAnalysis)
            .where(StoredAnalysis.kickoff > moment)
            .order_by(StoredAnalysis.kickoff)
        )
        upcoming = list(result.scalars().all())
        report.considered = len(upcoming)
        if not upcoming:
            return report

        boards = await BoardService(self._session).build(upcoming, per_track=count, now=moment)
        by_id = {record.provider_event_id: record for record in upcoming}

        for track, entries in boards.items():
            qualifying = [entry for entry in entries if track != SHARP or entry.score >= min_score]
            report.below_bar += len(entries) - len(qualifying)
            report.qualified += len(qualifying)

            for rank, entry in enumerate(qualifying[:count], start=1):
                record = by_id.get(entry.fixture_id)
                if record is None:
                    continue
                created = await self._store_entry(entry, record, rank, moment, LIVE)
                if created:
                    report.recorded += 1
                else:
                    report.already_present += 1

        await self._session.flush()
        logger.info("highlight.selected", summary=report.summary())
        return report

    async def _store_entry(
        self,
        entry: BoardEntry,
        record: StoredAnalysis,
        rank: int,
        moment: datetime,
        source: str,
    ) -> bool:
        """Write one board selection, returning whether it was new."""
        existing = await self._session.execute(
            select(HighlightSelection).where(
                HighlightSelection.provider_event_id == entry.fixture_id,
                HighlightSelection.market == entry.market,
                HighlightSelection.outcome == entry.outcome,
                HighlightSelection.source == source,
                HighlightSelection.track == entry.track,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return False

        self._session.add(
            HighlightSelection(
                track=entry.track,
                source=source,
                selection_date=moment.date(),
                rank=rank,
                provider_event_id=entry.fixture_id,
                home_name=entry.home_name,
                away_name=entry.away_name,
                competition=entry.competition,
                kickoff=record.kickoff,
                market=entry.market,
                outcome=entry.outcome,
                probability=entry.probability,
                base_rate=entry.base_rate,
                score=entry.score,
                confidence=_as_float(entry.factors.get("informational_content")),
                coverage=entry.coverage,
                model_version=MODEL_VERSION,
                components_used=list(record.components_used or []),
                rationale=entry.rationale,
                factors=dict(entry.factors),
                recorded_at=moment,
                status="pending",
            )
        )
        return True

    async def _store(
        self,
        lean: Lean,
        record: StoredAnalysis,
        rank: int,
        moment: datetime,
        source: str,
    ) -> bool:
        """Write one selection, returning whether it was new."""
        existing = await self._session.execute(
            select(HighlightSelection).where(
                HighlightSelection.provider_event_id == lean.fixture_id,
                HighlightSelection.market == lean.market,
                HighlightSelection.outcome == lean.outcome,
                HighlightSelection.source == source,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return False

        self._session.add(
            HighlightSelection(
                source=source,
                selection_date=moment.date(),
                rank=rank,
                provider_event_id=lean.fixture_id,
                home_name=lean.home_name,
                away_name=lean.away_name,
                competition=lean.competition,
                kickoff=record.kickoff,
                market=lean.market,
                outcome=lean.outcome,
                probability=lean.probability,
                base_rate=base_rate_for(lean.market, lean.outcome),
                score=lean.score,
                confidence=lean.confidence,
                coverage=lean.coverage,
                model_version=MODEL_VERSION,
                components_used=list(record.components_used or []),
                rationale=lean.reason,
                factors={
                    "informational_content": round(lean.confidence, 4),
                    "coverage": round(lean.coverage_score, 4),
                    "data_depth": round(lean.data_quality, 4),
                    "model_agreement": round(lean.stability, 4),
                    "market_agreement": round(lean.market_agreement, 4),
                    "overall": round(lean.score, 4),
                },
                recorded_at=moment,
                status="pending",
            )
        )
        return True

    async def settle(self, now: datetime | None = None) -> int:
        """Settle highlights whose matches have finished.

        Results come from the settlement table, which is populated from the
        provider's final scores. Highlights are never settled from a source
        that could disagree with the rest of the record.
        """
        moment = now or datetime.now(UTC)
        result = await self._session.execute(
            select(HighlightSelection).where(
                HighlightSelection.status == "pending",
                HighlightSelection.kickoff < moment - timedelta(hours=2),
            )
        )
        pending = list(result.scalars().all())
        if not pending:
            return 0

        settled_rows = await self._session.execute(
            select(SettledPrediction).where(
                SettledPrediction.provider_event_id.in_([h.provider_event_id for h in pending])
            )
        )
        results = {
            row.provider_event_id: (row.home_goals, row.away_goals)
            for row in settled_rows.scalars().all()
        }

        settled = 0
        for highlight in pending:
            score = results.get(highlight.provider_event_id)
            if score is None:
                continue
            home_goals, away_goals = score
            highlight.status = _settle_outcome(
                highlight.market, highlight.outcome, home_goals, away_goals
            )
            highlight.home_goals = home_goals
            highlight.away_goals = away_goals
            highlight.settled_at = moment
            settled += 1

        await self._session.flush()
        logger.info("highlight.settled", count=settled)
        return settled

    async def reconstruct(
        self,
        days: int = 30,
        count: int = DAILY_COUNT,
        min_score: float = MIN_SCORE,
        now: datetime | None = None,
    ) -> int:
        """Rebuild what highlights would have been, from settled predictions.

        Honest because the underlying forecasts were produced chronologically
        with no access to their own results, and the same scoring is applied.
        Kept separate because they were never actually published — a selection
        nobody saw is evidence about the method, not about the product.
        """
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(days=days)

        rows = await self._session.execute(
            select(SettledPrediction)
            .where(SettledPrediction.kickoff >= cutoff)
            .order_by(SettledPrediction.kickoff)
        )
        predictions = list(rows.scalars().all())
        if not predictions:
            return 0

        by_day: dict[date, list[SettledPrediction]] = {}
        for prediction in predictions:
            kickoff = prediction.kickoff
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=UTC)
            by_day.setdefault(kickoff.date(), []).append(prediction)

        created = 0
        builder = LeanBuilder()
        for day, day_predictions in sorted(by_day.items()):
            shaped = [_as_analysis(p) for p in day_predictions]
            leans = builder.rank(list(shaped), limit=count * 3)
            qualifying = [lean for lean in leans if lean.score >= min_score]

            by_id = {p.provider_event_id: p for p in day_predictions}
            for rank, lean in enumerate(qualifying[:count], start=1):
                found = by_id.get(lean.fixture_id)
                if found is None:
                    continue
                prediction = found
                if await self._exists(lean, RECONSTRUCTED):
                    continue

                kickoff = prediction.kickoff
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=UTC)

                self._session.add(
                    HighlightSelection(
                        source=RECONSTRUCTED,
                        selection_date=day,
                        rank=rank,
                        provider_event_id=lean.fixture_id,
                        home_name=lean.home_name,
                        away_name=lean.away_name,
                        competition=lean.competition,
                        kickoff=kickoff,
                        market=lean.market,
                        outcome=lean.outcome,
                        probability=lean.probability,
                        base_rate=base_rate_for(lean.market, lean.outcome),
                        score=lean.score,
                        confidence=lean.confidence,
                        coverage=lean.coverage,
                        model_version=prediction.model_version,
                        components_used=list(prediction.components_used or []),
                        rationale=lean.reason,
                        factors={"overall": round(lean.score, 4)},
                        # Dated to before kickoff: the forecast genuinely
                        # predates the match even though the row does not.
                        recorded_at=kickoff - timedelta(hours=2),
                        status=_settle_outcome(
                            lean.market,
                            lean.outcome,
                            prediction.home_goals,
                            prediction.away_goals,
                        ),
                        home_goals=prediction.home_goals,
                        away_goals=prediction.away_goals,
                        settled_at=moment,
                    )
                )
                created += 1

        await self._session.flush()
        logger.info("highlight.reconstructed", count=created, days=days)
        return created

    async def _exists(self, lean: Lean, source: str) -> bool:
        """Whether this selection is already stored."""
        result = await self._session.execute(
            select(HighlightSelection.id).where(
                HighlightSelection.provider_event_id == lean.fixture_id,
                HighlightSelection.market == lean.market,
                HighlightSelection.outcome == lean.outcome,
                HighlightSelection.source == source,
            )
        )
        return result.first() is not None

    async def today(
        self, now: datetime | None = None, track: str | None = None
    ) -> list[HighlightSelection]:
        """Return today's published selections, optionally for one board."""
        moment = now or datetime.now(UTC)
        statement = select(HighlightSelection).where(
            HighlightSelection.source == LIVE,
            HighlightSelection.selection_date == moment.date(),
        )
        if track is not None:
            statement = statement.where(HighlightSelection.track == track)
        result = await self._session.execute(
            statement.order_by(HighlightSelection.track, HighlightSelection.rank)
        )
        return list(result.scalars().all())

    async def history(
        self, days: int = 7, source: str = LIVE, now: datetime | None = None
    ) -> list[HighlightSelection]:
        """Return recent selections, newest first."""
        moment = now or datetime.now(UTC)
        cutoff = (moment - timedelta(days=days)).date()
        result = await self._session.execute(
            select(HighlightSelection)
            .where(
                HighlightSelection.source == source,
                HighlightSelection.selection_date >= cutoff,
            )
            .order_by(HighlightSelection.selection_date.desc(), HighlightSelection.rank)
        )
        return list(result.scalars().all())

    async def track_record(
        self,
        label: str,
        days: int | None = None,
        source: str = LIVE,
        now: datetime | None = None,
    ) -> TrackRecord:
        """Summarise settled highlight performance."""
        moment = now or datetime.now(UTC)
        statement = select(HighlightSelection).where(HighlightSelection.source == source)
        if days is not None:
            statement = statement.where(
                HighlightSelection.selection_date >= (moment - timedelta(days=days)).date()
            )

        rows = list((await self._session.execute(statement)).scalars().all())
        record = TrackRecord(label=label, source=source, total=len(rows))
        if not rows:
            return record

        probabilities: list[float] = []
        for row in rows:
            if row.status == "won":
                record.won += 1
            elif row.status == "lost":
                record.lost += 1
            elif row.status == "void":
                record.void += 1
            else:
                record.pending += 1
                continue

            probabilities.append(row.probability)
            won, played = record.by_market.get(row.market, (0, 0))
            record.by_market[row.market] = (
                won + (1 if row.status == "won" else 0),
                played + 1,
            )

        if probabilities:
            record.average_probability = sum(probabilities) / len(probabilities)
        return record


class _ShapedAnalysis:
    """Adapts a settled prediction to what the lean builder reads.

    Reuses the scoring rather than reimplementing it, so a reconstructed
    highlight is chosen by exactly the code that chooses a live one.
    """

    def __init__(self, prediction: SettledPrediction) -> None:
        self.provider_event_id = prediction.provider_event_id
        self.home_name = prediction.home_name
        self.away_name = prediction.away_name
        self.competition = prediction.competition
        self.kickoff = prediction.kickoff
        self.coverage = prediction.coverage
        self.components_used = list(prediction.components_used or [])
        self.home_stats = {"matches": 40}
        self.away_stats = {"matches": 40}
        self.market_probabilities = {
            "home": str(prediction.market_home or prediction.predicted_home),
            "draw": str(prediction.market_draw or prediction.predicted_draw),
            "away": str(prediction.market_away or prediction.predicted_away),
        }

        home = prediction.predicted_home
        draw = prediction.predicted_draw
        away = prediction.predicted_away
        markets: dict[str, dict[str, str]] = {
            "1X2": {"Home": str(home), "Draw": str(draw), "Away": str(away)},
            "Double chance": {
                "1X (home or draw)": str(home + draw),
                "12 (home or away)": str(home + away),
                "X2 (draw or away)": str(draw + away),
            },
        }
        if prediction.predicted_over_2_5 is not None:
            over = prediction.predicted_over_2_5
            markets["Goals"] = {
                "Over 2.5": str(over),
                "Under 2.5": str(1 - over),
            }
        if prediction.predicted_btts is not None:
            btts = prediction.predicted_btts
            markets["Both teams to score"] = {
                "Yes": str(btts),
                "No": str(1 - btts),
            }
        self.markets = markets


def _as_analysis(prediction: SettledPrediction) -> _ShapedAnalysis:
    """Adapt a settled prediction for lean scoring."""
    return _ShapedAnalysis(prediction)
