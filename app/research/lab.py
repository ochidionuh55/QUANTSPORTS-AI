"""Research Lab data access — reads the isolated research store, only.

This is the backend the admin-only Telegram Research Lab consumes. All reading
and comparison of shadow predictions happens here; the bot layer never computes
a probability, it renders what this returns. Every query targets
``research_predictions`` / ``research_experiments`` — never a production table —
so the Lab can never surface a customer-facing selection.

**Comparisons are scoped to one experiment.** A fixture card pairs a control
with its challengers *within the same* ``experiment_id``, because an experiment
writes its own control (the frozen champion recomputed on the same inputs as the
challenger, so the only difference is the thing under test). Control rows from a
different, inert experiment can never contaminate the comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.database.models import ResearchExperiment, ResearchPrediction

CONTROL_VERSION = "model-only-v2-dc"
MARKET = "1X2"
OUTCOMES = ("home", "draw", "away")

# Display labels. A version not listed falls back to a tidied form of its id.
VERSION_LABELS = {
    "model-only-v2-dc": "Production V2-DC",
    "exp002-recency-hl365": "EXP-002-365d",
    "exp009-stack-norm-hl365-it2": "EXP-009",
}


def label_for(version: str) -> str:
    """Human label for a model version string."""
    if version in VERSION_LABELS:
        return VERSION_LABELS[version]
    return version.replace("model-only-", "").upper()


@dataclass
class Fixture:
    """One fixture's shadow forecasts for one experiment: control + challengers."""

    experiment_id: str
    provider_event_id: str
    home_name: str
    away_name: str
    competition: str | None
    kickoff: datetime
    generated_at: datetime
    control: dict[str, float]
    challengers: dict[str, dict[str, float]] = field(default_factory=dict)
    home_goals: int | None = None
    away_goals: int | None = None
    settled_outcome: str | None = None
    settled: bool = False

    def deltas(self, version: str) -> dict[str, float]:
        """Challenger minus control, in probability points, per outcome."""
        chal = self.challengers.get(version, {})
        return {o: chal.get(o, 0.0) - self.control.get(o, 0.0) for o in OUTCOMES}

    def largest_disagreement(self) -> tuple[str, str, float]:
        """Return (version, outcome, signed delta) of the biggest gap, or empty."""
        best = ("", "", 0.0)
        for version in self.challengers:
            for outcome, d in self.deltas(version).items():
                if abs(d) > abs(best[2]):
                    best = (version, outcome, d)
        return best

    @property
    def has_challenger(self) -> bool:
        return bool(self.challengers)

    @property
    def has_control(self) -> bool:
        return bool(self.control)


class ResearchLab:
    """Read-only view over the research store for the admin Lab surface."""

    def __init__(self, session: object) -> None:
        self._session = session

    async def _comparisons(
        self, *, upcoming: bool | None, settled: bool | None
    ) -> list[Fixture]:
        """Build one Fixture per (experiment, event) that has a challenger.

        Control and challengers are paired strictly within an experiment, so the
        comparison is always the-frozen-champion-vs-its-challenger and never
        crosses experiments.
        """
        stmt = select(ResearchPrediction).where(
            ResearchPrediction.mode == "shadow",
            ResearchPrediction.market == MARKET,
        )
        rows = list((await self._session.execute(stmt)).scalars().all())  # type: ignore[attr-defined]

        now = datetime.now(UTC)
        fixtures: dict[tuple[str, str], Fixture] = {}
        probs: dict[tuple[str, str], dict[str, dict[str, float]]] = {}

        for r in rows:
            key = (r.experiment_id, r.provider_event_id)
            fx = fixtures.get(key)
            if fx is None:
                fx = Fixture(
                    experiment_id=r.experiment_id,
                    provider_event_id=r.provider_event_id,
                    home_name=r.home_name,
                    away_name=r.away_name,
                    competition=r.competition,
                    kickoff=r.kickoff,
                    generated_at=r.generated_at,
                    control={},
                )
                fixtures[key] = fx
            fx.generated_at = min(fx.generated_at, r.generated_at)
            probs.setdefault(key, {}).setdefault(r.challenger_version, {})[r.outcome] = (
                r.probability
            )
            if r.settled_at is not None:
                fx.settled = True
                fx.home_goals = r.home_goals
                fx.away_goals = r.away_goals
                fx.settled_outcome = r.settled_outcome

        result: list[Fixture] = []
        for key, fx in fixtures.items():
            versions = probs[key]
            fx.control = versions.get(CONTROL_VERSION, {})
            fx.challengers = {v: p for v, p in versions.items() if v != CONTROL_VERSION}
            # A comparison card needs both a control and at least one challenger.
            if not (fx.has_control and fx.has_challenger):
                continue
            if upcoming is True and fx.kickoff <= now:
                continue
            if upcoming is False and fx.kickoff > now:
                continue
            if settled is True and not fx.settled:
                continue
            if settled is False and fx.settled:
                continue
            result.append(fx)
        result.sort(key=lambda f: f.kickoff)
        return result

    async def today(self, limit: int = 20) -> list[Fixture]:
        """Upcoming fixtures with a control+challenger comparison, soonest first."""
        return (await self._comparisons(upcoming=True, settled=None))[:limit]

    async def disagreements(self, limit: int = 10) -> list[Fixture]:
        """Upcoming comparisons ranked by the largest absolute probability gap."""
        upcoming = await self._comparisons(upcoming=True, settled=None)
        upcoming.sort(key=lambda f: abs(f.largest_disagreement()[2]), reverse=True)
        return upcoming[:limit]

    async def recent_settled(self, limit: int = 10) -> list[Fixture]:
        """Settled comparisons, most recent kickoff first — forecasts unmodified."""
        settled = await self._comparisons(upcoming=None, settled=True)
        settled.sort(key=lambda f: f.kickoff, reverse=True)
        return settled[:limit]

    async def experiments(self) -> list[ResearchExperiment]:
        """Every registered research experiment, newest first."""
        rows = await self._session.execute(  # type: ignore[attr-defined]
            select(ResearchExperiment).order_by(ResearchExperiment.updated_at.desc())
        )
        return list(rows.scalars().all())

    async def shadow_counts(self) -> dict[str, dict[str, int]]:
        """Per challenger version: distinct shadow fixtures and settled count.

        Keyed by ``challenger_version`` so Experiment Status can show a live
        sample size next to each experiment's historical result.
        """
        total_stmt = (
            select(
                ResearchPrediction.challenger_version,
                func.count(func.distinct(ResearchPrediction.provider_event_id)),
            )
            .where(
                ResearchPrediction.mode == "shadow",
                ResearchPrediction.market == MARKET,
            )
            .group_by(ResearchPrediction.challenger_version)
        )
        settled_stmt = (
            select(
                ResearchPrediction.challenger_version,
                func.count(func.distinct(ResearchPrediction.provider_event_id)),
            )
            .where(
                ResearchPrediction.mode == "shadow",
                ResearchPrediction.market == MARKET,
                ResearchPrediction.settled_at.isnot(None),
            )
            .group_by(ResearchPrediction.challenger_version)
        )
        totals = dict((await self._session.execute(total_stmt)).all())  # type: ignore[attr-defined]
        settled = dict((await self._session.execute(settled_stmt)).all())  # type: ignore[attr-defined]
        return {
            v: {"fixtures": totals.get(v, 0), "settled": settled.get(v, 0)}
            for v in set(totals) | set(settled)
        }
