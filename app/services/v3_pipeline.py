"""V3 production routing — apply the validated V3 engine to a live analysis.

Guarded by ``FEATURES__ACTIVE_MODEL_VERSION``: a **no-op unless V3 is the active
engine**, so the V2 path is untouched by default and deploying this code changes
nothing until the flag is flipped. When V3 is active and the fixture is
V3-modellable, it recomputes the model-only view (1X2 + expected goals) from the
canonical :mod:`app.quant.v3_stack_norm` module over the competition's full
history, and stamps V3 lineage on the analysis.

It rewrites the **model-derived** view — ``model_probabilities`` and expected
goals (the services rebuild the grid from these), plus the goals-family markets
that are genuinely model output: Over/Under and Both-teams-to-score, recomputed
from the **same V3 grid** so they no longer show legacy V2 numbers beside a V3
expected-goals figure. It never touches ``probabilities`` — the published
1X2/Double-chance/Result, which are the bookmaker's de-margined price where odds
exist. A market number is never relabelled as a model number.

Full-precision expected goals are stored (not rounded), so the services grid
rebuilt from them reproduces the canonical V3 grid exactly — the invariant the
Stage-2 preflight checks.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.competitions import code_for_name
from app.core.config import get_settings
from app.database.models import HistoricalMatch
from app.quant import v3_stack_norm as v3
from app.quant.grid import build_match_probabilities
from app.services.match_analysis import MatchAnalysis

V3_VERSION = v3.VERSION
HISTORY_WINDOW_DAYS = 365 * 6  # generous; the V3 estimator down-weights old matches itself


def is_v3_active() -> bool:
    """Whether the production pipeline should serve V3."""
    return get_settings().features.active_model_version == V3_VERSION


async def _competition_id(session: AsyncSession, name: str | None) -> int | None:
    if not name:
        return None
    row = (
        await session.execute(
            text("SELECT id FROM competitions WHERE canonical_name = :n LIMIT 1"),
            {"n": name},
        )
    ).first()
    return int(row[0]) if row else None


async def _competition_pool(
    session: AsyncSession, competition_id: int, moment: datetime
) -> list[tuple[int, int, int, int, int]]:
    cutoff = moment - timedelta(days=HISTORY_WINDOW_DAYS)
    result = await session.execute(
        select(
            HistoricalMatch.home_team_id,
            HistoricalMatch.away_team_id,
            HistoricalMatch.home_goals,
            HistoricalMatch.away_goals,
            HistoricalMatch.match_date,
        ).where(
            HistoricalMatch.competition_id == competition_id,
            HistoricalMatch.match_date >= cutoff,
            HistoricalMatch.match_date < moment,
        )
    )
    return [
        (int(h), int(a), int(hg), int(ag), md.toordinal() if hasattr(md, "toordinal") else 0)
        for h, a, hg, ag, md in result.all()
    ]


async def apply_v3(session: AsyncSession, analysis: MatchAnalysis, moment: datetime) -> bool:
    """Overwrite the model-derived view with V3 when V3 is active. Returns applied?

    Byte-identical no-op when the flag is off or the fixture is not
    V3-modellable — the V2 analysis then stands unchanged.
    """
    if not is_v3_active():
        return False
    home = analysis.home.team_id
    away = analysis.away.team_id
    # Only touch a fixture the V2 path already modelled (resolved + enough history).
    if home is None or away is None or not analysis.model_probabilities:
        return False
    competition_id = await _competition_id(session, analysis.competition)
    if competition_id is None:
        return False
    pool = await _competition_pool(session, competition_id, moment)
    ref = moment.toordinal()
    lambdas = v3.v3_expected_goals(pool, home, away, ref)
    if lambdas is None:
        return False

    code = code_for_name(analysis.competition)
    # One V3 grid, one source of truth: 1X2, totals and BTTS all summed from it,
    # so the goals markets can never drift from the expected-goals figure beside
    # them. corrected=True + per-competition rho matches the canonical grid.
    mp = build_match_probabilities(lambdas[0], lambdas[1], corrected=True, competition=code)
    total = mp.home_win + mp.draw + mp.away_win
    if total <= 0:
        return False
    probs = {
        "home": mp.home_win / total,
        "draw": mp.draw / total,
        "away": mp.away_win / total,
    }

    # Model-only view (1X2 + expected goals) — the V3 engine's own numbers.
    analysis.model_probabilities = probs
    analysis.expected_home_goals = lambdas[0]
    analysis.expected_away_goals = lambdas[1]
    analysis.component_views = (dict(probs),)
    analysis.components_used = ("v3-stack-norm",)
    analysis.model_version = V3_VERSION

    # Model-derived goals markets from the SAME V3 grid. build_markets() then
    # rebuilds Goals/BTTS from these while leaving 1X2/Double-chance/Result on
    # ``analysis.probabilities`` (the market posterior) untouched — that number
    # is the bookmaker's, and it stays labelled as such.
    analysis.over_under = dict(mp.over_under)
    analysis.both_teams_score = mp.both_teams_score
    analysis.build_markets()
    return True
