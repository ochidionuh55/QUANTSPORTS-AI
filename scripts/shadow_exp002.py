#!/usr/bin/env python3
"""Shadow producer — control grid + recency + totals-preserving stack challengers.

For every upcoming fixture this writes append-only shadow rows into
``research_predictions`` under the ``EXP-002`` arena, all computed here from the
*same* inputs so the only difference between them is the modelling change under
test:

* ``model-only-v2-dc``        — frozen champion: equal-weight Dixon-Coles grid.
* ``exp002-recency-hl365``    — recency-weighted strength (half-life 365).
* ``exp009-stack-norm-hl365-it2`` — EXP-009, frozen exactly as tested: recency
  (hl 365) + opponent-adjusted (2 fixed-point iterations) **ratio**, with the
  total-goals level rescaled to the calibrated control's total. This is the
  candidate that passed historical, untouched-OOS and the full-market audit;
  the shadow clock is its last gate before any promotion consideration.

All three share the same team resolution (``learn=False``), the same three-season
history window and the same league averages, so within a fixture the arms differ
only in the strength model — a genuine "one thing changed" comparison. The
opponent-adjusted arm needs a joint fixed point over a competition's teams, so a
per-competition recent-match pool is built once per run and reused.

**Frozen, isolated, append-only.** Production is untouched; the champion's logic
is unchanged — this only records forecasts as paired baselines. ``generated_at``
is stamped now (< kickoff); rows are written only for future fixtures; the
unique key ``(experiment_id, challenger_version, provider_event_id, market,
outcome)`` means a re-run before kickoff never duplicates or rewrites a stored
prediction. A prediction, once stored, is never regenerated.

Run (ideally on a schedule before the day's kickoffs)::

    railway ssh "python scripts/shadow_exp002.py"
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select, text
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
RECENCY_VERSION = "exp002-recency-hl365"
STACK_VERSION = "exp009-stack-norm-hl365-it2"
HALF_LIFE = 365.0
STACK_ITERATIONS = 2
MARKET = "1X2"
OUTCOMES = ("home", "draw", "away")
HISTORY_WINDOW_DAYS = 365 * 3
MIN_LEAGUE_RESULTS = 20
MIN_COMP_MATCHES = 20
RATIO_FLOOR, RATIO_CEIL = 0.2, 5.0
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


def _clamp(x: float) -> float:
    return max(RATIO_FLOOR, min(RATIO_CEIL, x))


def _grid_1x2_from_lambda(lam_h: float, lam_a: float, code: str | None) -> Probs:
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


def _grid_1x2(hs: TeamStrength, as_: TeamStrength, avg: LeagueAverages, code: str | None) -> Probs:
    lam_h, lam_a = expected_goals(hs, as_, avg)
    return _grid_1x2_from_lambda(lam_h, lam_a, code)


def _control_strength(hist: TeamHistory, team_id: int, avg: LeagueAverages) -> TeamStrength:
    """Frozen champion: equal-weight attack/defence over the window."""
    home = [(g, c) for g, c, _ in hist.home]
    away = [(g, c) for g, c, _ in hist.away]
    return team_strength(team_id, home, away, avg)


def _recency_strength(
    hist: TeamHistory, team_id: int, avg: LeagueAverages, ref: int, half_life: float
) -> TeamStrength:
    """Exponential time-decay weighting of the same matches (EXP-002)."""
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


def _adjusted_strengths(
    matches: list[tuple[int, int, int, int, int]], avg: LeagueAverages, ref: int
) -> dict[int, TeamStrength]:
    """Recency-weighted opponent-adjusted strengths (EXP-009's ratio engine).

    Fixed point over one competition's recent matches, each weighted by the same
    exponential decay used elsewhere, for ``STACK_ITERATIONS`` passes.
    """

    def w(o: int) -> float:
        return float(0.5 ** ((ref - o) / HALF_LIFE))

    wscored: dict[int, float] = defaultdict(float)
    wconc: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    for h, a, hg, ag, o in matches:
        ww = w(o)
        wscored[h] += ww * hg
        wconc[h] += ww * ag
        counts[h] += 1
        wscored[a] += ww * ag
        wconc[a] += ww * hg
        counts[a] += 1
    teams = list(counts)
    attack = dict.fromkeys(teams, 1.0)
    defence = dict.fromkeys(teams, 1.0)
    for _it in range(STACK_ITERATIONS):
        denom_a: dict[int, float] = defaultdict(float)
        denom_d: dict[int, float] = defaultdict(float)
        for h, a, _hg, _ag, o in matches:
            ww = w(o)
            denom_a[h] += ww * avg.home_goals * defence[a]
            denom_d[h] += ww * avg.away_goals * attack[a]
            denom_a[a] += ww * avg.away_goals * defence[h]
            denom_d[a] += ww * avg.home_goals * attack[h]
        attack = {t: _clamp(wscored[t] / denom_a[t]) if denom_a[t] > 0 else 1.0 for t in teams}
        defence = {t: _clamp(wconc[t] / denom_d[t]) if denom_d[t] > 0 else 1.0 for t in teams}
    return {t: TeamStrength(t, attack[t], defence[t], counts[t]) for t in teams}


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


async def _competition_strengths(
    session: AsyncSession, comp_ids: set[int], avg: LeagueAverages, moment: datetime, ref: int
) -> dict[int, dict[int, TeamStrength]]:
    """Opponent-adjusted strengths per competition, from its recent matches.

    Loads **one competition at a time** and discards its matches after computing
    the fixed point, so memory stays bounded regardless of how many competitions
    or seasons are in play — a single bulk load of every competition's history
    is what an earlier version choked on.
    """
    out: dict[int, dict[int, TeamStrength]] = {}
    cutoff = moment - timedelta(days=HISTORY_WINDOW_DAYS)
    for cid in comp_ids:
        result = await session.execute(
            select(
                HistoricalMatch.home_team_id,
                HistoricalMatch.away_team_id,
                HistoricalMatch.home_goals,
                HistoricalMatch.away_goals,
                HistoricalMatch.match_date,
            ).where(
                HistoricalMatch.competition_id == cid,
                HistoricalMatch.match_date >= cutoff,
                HistoricalMatch.match_date < moment,
            )
        )
        matches = [
            (int(h), int(a), int(hg), int(ag),
             mdate.toordinal() if hasattr(mdate, "toordinal") else 0)
            for h, a, hg, ag, mdate in result.all()
        ]
        if len(matches) >= MIN_COMP_MATCHES:
            out[cid] = _adjusted_strengths(matches, avg, ref)
    return out


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
    ref = now.date().toordinal()

    scanned = paired = written = stack_written = 0
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

        comp_name_to_id = {
            str(r[1]): int(r[0])
            for r in (await session.execute(text("SELECT id, canonical_name FROM competitions")))
            .fetchall()
        }

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

        # Pass 1: resolve teams and collect the competitions we will need.
        resolved: list[tuple[StoredAnalysis, int, int, int | None]] = []
        for record in upcoming:
            home_res = await resolver.resolve(
                "api_football", record.home_name, sport="football",
                country=record.country, learn=False,
            )
            away_res = await resolver.resolve(
                "api_football", record.away_name, sport="football",
                country=record.country, learn=False,
            )
            if not (home_res.is_resolved and away_res.is_resolved):
                skipped_unresolved += 1
                continue
            assert home_res.team is not None and away_res.team is not None
            comp_id = comp_name_to_id.get(record.competition) if record.competition else None
            resolved.append((record, home_res.team.id, away_res.team.id, comp_id))

        needed = {cid for *_, cid in resolved if cid is not None}
        try:
            comp_strengths = await _competition_strengths(session, needed, league, now, ref)
        except Exception:  # noqa: BLE001 — EXP-009 is best-effort; control+recency must still write
            traceback.print_exc()
            print("  EXP-009 strengths step failed; writing control + recency only.")
            comp_strengths = {}

        # Pass 2: compute all arms and write.
        for record, home_id, away_id, comp_id in resolved:
            home_hist = await history_for(home_id)
            away_hist = await history_for(away_id)
            hs = _control_strength(home_hist, home_id, league)
            as_ = _control_strength(away_hist, away_id, league)
            if not (hs.is_reliable and as_.is_reliable):
                skipped_thin += 1
                continue

            code = code_for_name(record.competition) if record.competition else None
            arms: list[tuple[str, Probs]] = [
                (CONTROL_VERSION, _grid_1x2(hs, as_, league, code)),
                (
                    RECENCY_VERSION,
                    _grid_1x2(
                        _recency_strength(home_hist, home_id, league, ref, HALF_LIFE),
                        _recency_strength(away_hist, away_id, league, ref, HALF_LIFE),
                        league,
                        code,
                    ),
                ),
            ]

            # EXP-009: recency + opponent-adjusted ratio, control's total.
            strengths = comp_strengths.get(comp_id) if comp_id is not None else None
            if strengths and home_id in strengths and away_id in strengths:
                sl_h, sl_a = expected_goals(strengths[home_id], strengths[away_id], league)
                cl_h, cl_a = expected_goals(hs, as_, league)
                total = sl_h + sl_a
                if total > 0:
                    scale = (cl_h + cl_a) / total
                    stack = _grid_1x2_from_lambda(sl_h * scale, sl_a * scale, code)
                    arms.append((STACK_VERSION, stack))

            paired += 1
            for version, probs in arms:
                for row in _rows_for(record, version, probs, now):
                    if (version, row.provider_event_id, row.outcome) in existing:
                        skipped_existing += 1
                        continue
                    session.add(row)
                    written += 1
                    if version == STACK_VERSION:
                        stack_written += 1

        await session.commit()

    await database.disconnect()
    print(RULE)
    print("SHADOW PREDICT — EXP-002 arena")
    print(f"  {CONTROL_VERSION} · {RECENCY_VERSION} · {STACK_VERSION}")
    print(f"  grid {grid_version()} · hl{int(HALF_LIFE)}d · stack it{STACK_ITERATIONS} norm")
    print(RULE)
    print(f"\n  as of                        : {now:%Y-%m-%d %H:%M} UTC")
    print(f"  upcoming fixtures scanned    : {scanned:,}")
    print(f"  fixtures modelled            : {paired:,}")
    print(f"  shadow rows written          : {written:,}  (EXP-009 rows: {stack_written:,})")
    print(f"  rows already present (kept)  : {skipped_existing:,}")
    print(f"  skipped — teams unresolved   : {skipped_unresolved:,}")
    print(f"  skipped — too little history : {skipped_thin:,}")
    print("\n  Append-only. Wrote only research_predictions (EXP-002). No production change.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
