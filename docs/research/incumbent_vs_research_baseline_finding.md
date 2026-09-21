# Architecture finding: the research "V2-DC control" is NOT the live serving model

**Date:** 2026-09-21
**Found during:** EXP-009 → V3 production-promotion preflight
**Status:** permanent record — correcting an earlier incorrect assumption

## The incorrect assumption

The research programme (EXP-002 recency, EXP-003 opponent-adjusted, EXP-007
stack, EXP-008/EXP-009 totals-preserving) treated `model-only-v2-dc` — the pure
canonical Dixon-Coles **grid** 1X2 (`challenger_eval._control_1x2`) — as the
production champion, i.e. the model users actually receive. **That assumption
was wrong.** `model-only-v2-dc` (pure grid) is a *research construct*. It is not
the model the production pipeline serves.

## What production actually serves (verified in code)

Traced end to end on 2026-09-21:

1. **Published 1X2** (fixture "Today's analysis"): with no promoted model the
   ensemble runs at `reliability = 0`, so the published posterior **equals the
   bookmaker's de-margined market price** where odds exist — not our model at
   all (`match_analysis.MatchAnalysisService._model`, `_market_prior`).
2. **Model-only view** (`stored_analyses.model_probabilities`, what "Best of
   today" ranks on): an **equal-weight blend of three components — Poisson +
   Elo + Form** (`match_analysis._model`), *not* the pure grid.
3. **Canonical grid used by Best-of/services**: the Poisson grid **tilted** so
   its 1X2 marginals match the blend (`selections._forecast`,
   `selections._tilted`), then markets derived from that tilted grid
   (`best_of_day` / `markets.derive_markets`).
4. **Opponent adjustment** (the joint per-competition fixed point EXP-003/009
   use) is **not computed anywhere in the production analysis path** — production
   estimates each team's strength independently and equal-weighted.

## Why it matters

- All challenger evidence to date compares against the **research V2 grid**, not
  the live blend. EXP-009 beating the research V2 grid does **not** by itself
  establish that it beats what users actually receive.
- Promoting EXP-009 as pure-grid **V3** (architecture "A": one canonical path,
  Elo/Form retired from the model-only view) is therefore a real architecture
  change, and the correct promotion baseline is the **actual incumbent blend**,
  not the research control.

## Action taken

- `scripts/incumbent_vs_v3.py` reconstructs the **actual incumbent** using
  production's own Elo/Form/Poisson/tilt code and compares it, on identical
  fixtures and walk-forward/OOS methodology, against the frozen pure V3
  (hl365 · oppadj it2 · totals-preserved). Three paths kept separate
  (published / model-only / canonical grid). V3 is **frozen** — no tuning from
  this comparison.
- Promotion of V3 proceeds **only** if it improves on the *actual* incumbent
  under the predeclared criteria without unacceptable downstream market
  regression. Otherwise: stop, report, do not promote.
- EXP-009 prospective shadow collection continues uninterrupted; its clock is
  not reset by this finding.

## Correct mental model going forward

"Champion" = **what production actually serves** (currently the Poisson+Elo+Form
blend with market-prior published probabilities), not the research pure-grid
control. Any future promotion compares a candidate against that live baseline.
