"""Research Lab data access — reads the isolated research store, only.

This is the backend the admin-only Telegram Research Lab consumes. All reading
and comparison of shadow predictions happens here; the bot layer never computes
a probability, it renders what this returns. Every query targets
``research_predictions`` / ``research_experiments`` — never a production table —
so the Lab can never surface a customer-facing selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from app.database.models import ResearchExperiment, ResearchPrediction

CONTROL_VERSION = "model-only-v2-dc"
CONTROL_LABEL = "V2-DC"
MARKET = "1X2"
OUTCOMES = ("home", "draw", "away")


@dataclass
class Fixture:
    """One fixture's shadow forecasts: the control and any challengers."""

    provider_event_id: str
    home_name: str
    away_name: str
    competition: str | None
    kickoff: datetime
    control: dict[str, float]
    challengers: dict[str, dict[str, float]] = field(default_factory=dict)
    home_goals: int | None = None
    away_goals: int | None = None
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


class ResearchLab:
    """Read-only view over the research store for the admin Lab surface."""

    def __init__(self, session: object) -> None:
        self._session = session

    async def _fixtures(self, *, upcoming: bool | None, settled: bool | None) -> list[Fixture]:
        stmt = select(ResearchPrediction).where(
            ResearchPrediction.mode == "shadow",
            ResearchPrediction.market == MARKET,
        )
        rows = list((await self._session.execute(stmt)).scalars().all())  # type: ignore[attr-defined]

        now = datetime.now(UTC)
        by_fixture: dict[str, Fixture] = {}
        probs: dict[str, dict[str, dict[str, float]]] = {}
        for r in rows:
            fx = by_fixture.get(r.provider_event_id)
            if fx is None:
                fx = Fixture(
                    provider_event_id=r.provider_event_id,
                    home_name=r.home_name,
                    away_name=r.away_name,
                    competition=r.competition,
                    kickoff=r.kickoff,
                    control={},
                )
                by_fixture[r.provider_event_id] = fx
            versions = probs.setdefault(r.provider_event_id, {})
            versions.setdefault(r.challenger_version, {})[r.outcome] = r.probability
            if r.settled_at is not None:
                fx.settled = True
                fx.home_goals = r.home_goals
                fx.away_goals = r.away_goals

        result: list[Fixture] = []
        for pe, fx in by_fixture.items():
            versions = probs[pe]
            fx.control = versions.get(CONTROL_VERSION, {})
            fx.challengers = {v: p for v, p in versions.items() if v != CONTROL_VERSION}
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
        """Upcoming fixtures with a stored shadow forecast, soonest first."""
        return (await self._fixtures(upcoming=True, settled=None))[:limit]

    async def disagreements(self, limit: int = 10) -> list[Fixture]:
        """Upcoming fixtures with a challenger, ranked by largest gap vs control."""
        upcoming = await self._fixtures(upcoming=True, settled=None)
        fixtures = [f for f in upcoming if f.has_challenger]
        fixtures.sort(key=lambda f: abs(f.largest_disagreement()[2]), reverse=True)
        return fixtures[:limit]

    async def recent_settled(self, limit: int = 10) -> list[Fixture]:
        """Fixtures whose shadow forecasts have a result, most recent first."""
        fixtures = await self._fixtures(upcoming=None, settled=True)
        fixtures.sort(key=lambda f: f.kickoff, reverse=True)
        return fixtures[:limit]

    async def experiments(self) -> list[ResearchExperiment]:
        """Every registered research experiment, newest first."""
        rows = await self._session.execute(  # type: ignore[attr-defined]
            select(ResearchExperiment).order_by(ResearchExperiment.updated_at.desc())
        )
        return list(rows.scalars().all())
