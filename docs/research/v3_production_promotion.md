# V3 Production Promotion — `model-only-v3-stack-norm`

**Activated:** 2026-09-21, ~08:54 UTC (first V3 scan stored 08:56:26 UTC).
**Status:** LIVE across worker, BOT, API. V2-DC frozen and one env var from rollback.

## What V3 is (frozen lineage — do not tune)

`model-only-v3-stack-norm`, promoted from EXP-009:

- **Ratio (who wins):** recency-weighted (half-life 365d) **and** opponent-adjusted
  team strengths, 2 fixed-point iterations, per-competition joint estimation,
  ratio clamp [0.2, 5.0].
- **Level (how many goals):** rescaled so the total equals the frozen
  equal-weight control's total — the totals-preserving anchor that keeps the
  goals markets calibrated.
- **Grid:** the canonical Dixon-Coles grid, per-competition rho. Every market
  derives from that one joint distribution, exactly as before.

Single canonical implementation: `app/quant/v3_stack_norm.py` (pure functions
over a competition match pool). The same code produced the research evidence,
the shadow forecasts and the production forecasts.

## How it routes (Stage 2, default-OFF)

- Governed by `FEATURES__ACTIVE_MODEL_VERSION`. Anything other than the V3
  identifier ⇒ the routing is inert and the pipeline is byte-for-byte V2.
- `app/services/v3_pipeline.apply_v3()` runs in the scan loop between
  `analyse_event` and `_store`. When V3 is active and a fixture is
  V3-modellable, it overwrites **only** the model-only view
  (`model_probabilities` + full-precision expected goals) and stamps
  `model_version`. It never touches the published/market posterior — a
  bookmaker number is never relabelled as a model number. Best-effort: any
  failure leaves the V2 analysis untouched (fails closed to V2).
- `selections._forecast` rebuilds the per-competition-rho canonical grid
  **untilted** under V3, so the services grid equals the V3 grid exactly.
- `best_of_day.model_only_version()` returns the V3 string only when the flag
  is V3, so every stored selection names the engine behind its numbers.

## Integrity — proven, not asserted

- **research-code == production-code:** `verify_v3_equivalence.py` — Δλ = 0
  over 101,940 fixtures, both windows.
- **Actual incumbent vs V3:** `incumbent_vs_v3.py` — V3 beats the real live
  blend (Poisson+Elo+form) on Brier both windows (0.6199→0.6146 newest,
  0.6140→0.6089 OOS) and 18 markets better / 0 worse in both.
- **Stage-2 preflight (`preflight_v3.py`) — PASS on 250 real fixtures:**
  - A. canonical-grid invariant (services grid == V3 grid) — max cell |Δ| = 0
  - B. 22-service propagation (every market filled from a V3 grid)
  - C. settlement compatibility (V3 market universe == V2's; predicates unchanged)
  - D. default-OFF equivalence (deploy changes nothing)
  - E. activation + rollback semantics (one reversible flag)

## Lineage (never rewritten)

- Analyses stored before activation stay V2 / legacy; after, V3 — stamped at
  compute time in `stored_analyses.provenance.model_version`.
- A fixture V3 cannot model falls back to the V2 blend and is honestly stamped
  `model-only-v2-dc` (the 08:56 scan: 27 V3 + 21 V2-fallback).
- Published selections keep the version they were published under. Today's card
  (2026-09-21) was published pre-flip at 00:27 UTC and is not restamped; the
  first V3 selections publish on the next `selection_date`.

## Registry

`model_versions` id=1, `model-only-v3-stack-norm`, status **VALIDATED**
(non-PROMOTED — it can never drive the market value-detection path; serving is
governed only by the feature flag). Full lineage + both OOS windows + the
incumbent-vs-V3 metrics recorded in its JSON.

## Rollback

Set `FEATURES__ACTIVE_MODEL_VERSION=model-only-v2-dc` on worker (and BOT/API).
The V2 code path is untouched — `apply_v3` short-circuits — so V2 output is
byte-identical to before Stage 2. Verified by preflight check E (flag flips
V2→V3→V2 on a live service, `is_v3_active()` follows) and by the live V2
baseline captured at 08:53 UTC today from this exact binary.

## Continues after activation

- The V2-vs-challenger prospective shadow arena keeps running (EXP-002 clock not
  reset); its control remains `model-only-v2-dc` by design.
- `/lab` shows a live **Active Production Engine** indicator read from the
  backend flag — observational only; Telegram computes no probabilities.
