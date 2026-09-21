#!/usr/bin/env python3
"""EXP-002 shadow producer — control grid + recency challenger, one thing changed.

For every upcoming fixture this writes two append-only shadow rows into
``research_predictions``, both computed here from the *same* inputs so the only
difference between them is the recency weighting:

* ``model-only-v2-dc`` — the frozen champion's Dixon-Coles grid 1X2 with
  equal-weight ``team_strength`` (identical to ``challenger_eval._control_1x2``).
* ``exp002-recency-hl365`` — the same grid with exponential time-decay
  weighting of the attack/defence estimate (half-life 365 days, the value
  EXP-002 selected on its validation window).

Both use the production team-resolution and history window
(:class:`app.services.team_resolution.TeamResolver`, a three-season lookback and
the same league-average gate as live analysis), so the control row is the frozen
grid on real inputs — not the 3-component production *blend* in
``stored_analyses.model_probabilities``, which is a different model. Pairing the
recency grid against the equal-weight grid is the prospective continuation of the
same experiment that was validated historically.

**Frozen, isolated, append-only.** The champion's logic is unchanged — this
merely records its forecast as a paired baseline. Nothing is written to any
production table. ``generated_at`` is stamped now (< kickoff), rows are written
only for future fixtures, and the unique key ``(experiment_id,
challenger_version, provider_event_id, market, outcome)`` means a re-run before
kickoff never duplicates or rewrites a stored prediction. A prediction, once
stored, is never regenerated.

Run (ideally on a schedule before the day's kickoffs)::

    railway ssh "python scripts/shadow_exp002.py"
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.competitions import code_for_name
from app.core.config import get_settings
from app.database.models import HistoricalMatch, ResearchPrediction, StoredAnalysis
from app.infrastructure.database import Database
from app.quant.grid import build_grid, grid_version
from app.quant.poisson import LeagueAverages, TeamStrength, expected_goals, team_strength
from app.services.team_resolution import TeamResolver

EXPERIMENT = "EXP-002"
CONTROL_VERSION = "model-only-v2-dc"
CHALLENGER_VERSION = "exp002-recency-hl365"
HALF_LIFE = 365.0
MARKET = "1X2"
OUTCOMES = ("home", "draw", "away")
HISTORY_WINDOW_DAYS = 365 * 3
MIN_LEAGUE_RESULTS = 20
RULE = "=" * 92
Probs = tuple[float, float, float]


@dataclass
class TeamHistory:
    """A team's recent matches as dated ``(scored, conceded, ordinal)``."""

    home: list[tuple[int, int, int]]
    away: list[tuple[int, int, int]]

    @property
    def played(self) -> int:
        return len(self.home) + len(self.away)


def _grid_1x2(hs: TeamStrength, as_: TeamStrength, avg: LeagueAverages, code: str | None) -> Probs:
    """1X2 from the canonical Dixon-Coles grid — the shared downstream."""
    lam_h, lam_a = expected_goals(hs, as_, avg)
    grid = build_grid(lam_h, lam_a, corrected=True, competition=code)
    ph = pd = pa = 0.0
    for (h, a), p in grid.items():
        fp = float(p)
        if h > a:
            ph += fp
        elif h == a:
            pd += fp
        else:
            pa += fp
    total = ph + pd + pa
    return (ph / total, pd / total, pa / total)


def _control_strength(hist: TeamHistory, team_id: int, avg: LeagueAverages) -> TeamStrength:
    """Frozen champion: equal-weight attack/defence over the window."""
    home = [(g, c) for g, c, _ in hist.home]
    away = [(g, c) for g, c, _ in hist.away]
    return team_strength(team_id, home, away, avg)


def _recency_strength(
    hist: TeamHistory, team_id: int, avg: LeagueAverages, ref: int, half_life: float
) -> TeamStrength:
    """The one change: exponential time-decay weighting of the same matches."""
    matches = hist.home + hist.away
    played = hist.played
    if played == 0:
        return TeamStrength(team_id, 1.0, 1.0, 0)
    baseline = (avg.home_goals + avg.away_goals) / 2
    if baseline <= 0:
        return TeamStrength(team_id, 1.0, 1.0, played)
    wsum = wscored = wconc = 0.0
    for g, c, o in matches:
        w = 0.5 ** ((ref - o) / half_life)
        wsum += w
        wscored += w * g
        wconc += w * c
    expected = baseline * wsum
    if expected <= 0:
        return TeamStrength(team_id, 1.0, 1.0, played)
    return TeamStrength(team_id, wscored / expected, wconc / expected, played)


async def _league_averages(session: AsyncSession, moment: datetime) -> LeagueAverages | None:
    cutoff = moment - timedelta(days=HISTORY_WINDOW_DAYS)
    result = await session.execute(
        select(HistoricalMatch.home_goals, HistoricalMatch.away_goals).where(
            HistoricalMatch.match_date >= cutoff,
            HistoricalMatch.match_date < moment,
        )
    )
    rows = list(result.all())
    if len(rows) < MIN_LEAGUE_RESULTS:
        return None
    return LeagueAverages(
        home_goals=sum(h for h, _ in rows) / len(rows),
        away_goals=sum(a for _, a in rows) / len(rows),
        matches=len(rows),
    )


async def _history(session: AsyncSession, team_id: int, moment: datetime) -> TeamHistory:
    cutoff = moment - timedelta(days=HISTORY_WINDOW_DAYS)
    result = await session.execute(
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
        .limit(60)
    )
    home: list[tuple[int, int, int]] = []
    away: list[tuple[int, int, int]] = []
    for m in result.scalars().all():
        o = m.match_date.toordinal() if hasattr(m.match_date, "toordinal") else 0
        if m.home_team_id == team_id:
            home.append((m.home_goals, m.away_goals, o))
        else:
            away.append((m.away_goals, m.home_goals, o))
    return TeamHistory(home=home, away=away)


def _rows_for(
    record: StoredAnalysis, version: str, probs: Probs, now: datetime
) -> list[ResearchPrediction]:
    return [
        ResearchPrediction(
            experiment_id=EXPERIMENT,
            challenger_version=version,
            mode="shadow",
            provider_event_id=record.provider_event_id,
            competition=record.competition,
            home_name=record.home_name,
            away_name=record.away_name,
            kickoff=record.kickoff,
            market=MARKET,
            outcome=outcome,
            probability=probs[i],
            generated_at=now,
        )
        for i, outcome in enumerate(OUTCOMES)
    ]


async def main() -> int:
    database = Database(get_settings())
    await database.connect()
    now = datetime.now(UTC)
    ref_ordinal = now.date().toordinal()

    scanned = paired = written = 0
    skipped_existing = skipped_unresolved = skipped_thin = 0

    async with database.session() as session:
        upcoming = list(
            (await session.execute(select(StoredAnalysis).where(StoredAnalysis.kickoff > now)))
            .scalars()
            .all()
        )
        scanned = len(upcoming)

        league = await _league_averages(session, now)
        if league is None or not league.is_reliable:
            print("League averages unavailable/unreliable; nothing written.")
            await database.disconnect()
            return 1

        # Preload existing keys for this experiment so a re-run adds only what
        # is missing and never rewrites a stored prediction.
        existing_rows = await session.execute(
            select(
                ResearchPrediction.challenger_version,
                ResearchPrediction.provider_event_id,
                ResearchPrediction.outcome,
            ).where(
                ResearchPrediction.experiment_id == EXPERIMENT,
                ResearchPrediction.market == MARKET,
            )
        )
        existing = {(r[0], r[1], r[2]) for r in existing_rows.all()}

        resolver = TeamResolver(session)
        history_cache: dict[int, TeamHistory] = {}

        async def history_for(team_id: int) -> TeamHistory:
            if team_id not in history_cache:
                history_cache[team_id] = await _history(session, team_id, now)
            return history_cache[team_id]

        for record in upcoming:
            # learn=False: resolution must not persist a new alias — a research
            # producer never mutates a production table, not even as a side effect.
            home_res = await resolver.resolve(
                "api_football",
                record.home_name,
                sport="football",
                country=record.country,
                learn=False,
            )
            away_res = await resolver.resolve(
                "api_football",
                record.away_name,
                sport="football",
                country=record.country,
                learn=False,
            )
            if not (home_res.is_resolved and away_res.is_resolved):
                skipped_unresolved += 1
                continue
            assert home_res.team is not None and away_res.team is not None
            home_hist = await history_for(home_res.team.id)
            away_hist = await history_for(away_res.team.id)

            hs = _control_strength(home_hist, home_res.team.id, league)
            as_ = _control_strength(away_hist, away_res.team.id, league)
            if not (hs.is_reliable and as_.is_reliable):
                skipped_thin += 1
                continue

            code = code_for_name(record.competition) if record.competition else None
            control = _grid_1x2(hs, as_, league, code)
            challenger = _grid_1x2(
                _recency_strength(home_hist, home_res.team.id, league, ref_ordinal, HALF_LIFE),
                _recency_strength(away_hist, away_res.team.id, league, ref_ordinal, HALF_LIFE),
                league,
                code,
            )
            paired += 1

            for version, probs in (
                (CONTROL_VERSION, control),
                (CHALLENGER_VERSION, challenger),
            ):
                for row in _rows_for(record, version, probs, now):
                    if (version, row.provider_event_id, row.outcome) in existing:
                        skipped_existing += 1
                        continue
                    session.add(row)
                    written += 1

        await session.commit()

    await database.disconnect()
    print(RULE)
    print(f"SHADOW PREDICT — EXP-002  control {CONTROL_VERSION} vs {CHALLENGER_VERSION}")
    print(f"grid {grid_version()} · half-life {int(HALF_LIFE)}d · one thing changed: recency")
    print(RULE)
    print(f"\n  as of                        : {now:%Y-%m-%d %H:%M} UTC")
    print(f"  upcoming fixtures scanned    : {scanned:,}")
    print(f"  fixtures paired (both models): {paired:,}")
    print(f"  shadow rows written          : {written:,}")
    print(f"  rows already present (kept)  : {skipped_existing:,}")
    print(f"  skipped — teams unresolved   : {skipped_unresolved:,}")
    print(f"  skipped — too little history : {skipped_thin:,}")
    print("\n  Append-only. Wrote only research_predictions (EXP-002). No production change.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
