# Validation — canonical grid and the Dixon-Coles correction

**Date:** 17 September 2026
**Question:** should the Dixon-Coles low-score correction become the production
scoreline engine?
**Answer:** not yet. Merge the architecture, ship with the correction **off**,
revisit after the outstanding work below.

---

## 1. What was measured

Two engines, forecasting identical fixtures from identical scoring rates:

| | Engine |
|---|---|
| **A — baseline** | Independent Poisson (`app.quant.poisson.score_matrix`) — what production used before this change |
| **B — candidate** | Dixon-Coles corrected grid (`app.quant.dixon_coles.score_matrix`, ρ = −0.13) |

The only difference between them is the correction. Rates, fixture list,
ordering and eligibility rules are shared.

## 2. Dataset and method

| | |
|---|---|
| **Source** | 384 competition CSVs in `data/`, football only |
| **Competitions** | 38 |
| **Date range** | 2016-08-15 → 2026-05-31 |
| **Forecasts** | 88,808 per engine |
| **Protocol** | Strictly chronological walk-forward. Matches read in date order; team rates estimated only from matches already played. No fixture is scored by a model that has seen it. |
| **Eligibility** | ≥ 60 league matches in the baseline, ≥ 5 prior matches per side, league and both team strengths flagged reliable |
| **Rate model** | Production functions — `team_strength()` and `expected_goals()` from `app.quant.poisson`, including the home-advantage term and the home/away split in league baselines |
| **Bootstrap** | Paired, 2,000 resamples, seed 20260917 |

**Reproduce:**

```bash
python scripts/audit_grid.py
python scripts/audit_grid.py --leagues E0,D1,SP1,I1 --json results.json
```

No database or network required; the script runs against the committed CSVs.

### 2.1 A harness error, recorded because it nearly became a finding

The first version of this audit used a simplified rate model: a symmetric
attack/defence ratio with **no home-advantage term** and **no home/away split**
in the league baseline. It reported a 12–14 point home-win under-prediction and
a matching away-win over-prediction, which would have been the largest bias in
the model by a factor of six.

That bias was in the harness, not in production. Rerunning through
`team_strength()` and `expected_goals()` — the functions the live scanner
actually calls — gives a home bias of **−0.4%**.

The lesson is recorded here rather than quietly fixed: a validation harness
that reimplements the thing it validates measures the reimplementation.
`scripts/audit_grid.py` now calls the production functions directly and carries
a comment saying why.

## 3. Headline results

| Metric | A: Poisson | B: Dixon-Coles | Change | |
|---|---|---|---|---|
| **Brier** (lower better) | 0.629443 | 0.629374 | −0.000069 | better |
| **Log loss** (lower better) | 1.050290 | **1.052566** | **+0.002276** | **worse** |
| **ECE** (lower better) | 0.042797 | 0.039095 | −0.003701 | better |

Brier and ECE improve. Log loss degrades. The correction moves mass toward
draws, which helps aggregate calibration and hurts the fixtures where it was
already confident and right.

## 4. Outcome bias — the question this change was raised to answer

Predicted rate minus observed rate. Negative = under-predicted.

| Outcome | Observed | A: Poisson | bias | B: Dixon-Coles | bias |
|---|---|---|---|---|---|
| Home | 43.8% | 43.4% | −0.4% | 42.0% | −1.8% |
| **Draw** | **26.3%** | **24.3%** | **−2.0%** | **27.0%** | **+0.8%** |
| Away | 29.9% | 32.3% | +2.4% | 30.9% | +1.0% |

**The draw bias is fixed.** −2.0% → +0.8%, a reduction in absolute error from
2.0 points to 0.8. Away-win over-prediction also improves, 2.4% → 1.0%.

**But home-win under-prediction worsens**, −0.4% → −1.8%. The correction adds
draw mass by taking it from both result legs, and the home leg was already
slightly short.

This is the clearest result in the audit: the correction does what it was
added to do, and introduces a smaller error elsewhere while doing it.

## 5. Is the improvement distinguishable from chance?

Paired bootstrap on per-forecast Brier differences, 2,000 resamples:

```
mean difference : -0.000069   (negative favours Dixon-Coles)
95% interval    : [-0.000327, +0.000183]
verdict         : straddles zero
```

**No.** On 88,808 forecasts the Brier improvement cannot be distinguished from
chance. The interval comfortably contains zero.

## 6. Consistency across competitions and time

**Per-competition:** Brier improved in **21 of 38**. That is close to a coin
flip and does not describe a change that helps generally.

Draw bias, by contrast, moved toward zero in **every** competition — but in 16
of them it overshot into over-prediction:

| Competition | Draw bias A → B |
|---|---|
| I2 (Serie B) | −6.4% → −3.4% |
| SC1 | −5.9% → −3.1% |
| D2 | −4.2% → −1.5% |
| D1 | −4.0% → −1.6% |
| E0 (Premier League) | −1.0% → **+1.6%** |
| E1 | −1.0% → **+1.8%** |
| IRL | −0.2% → **+2.5%** |
| JAP | −0.1% → **+2.7%** |

The pattern is legible: a single global ρ = −0.13 is roughly right for leagues
that were badly under-predicting draws and too aggressive for leagues that were
nearly calibrated already.

**Chronological windows** (four equal periods): Brier improved in 2 of 4, by
margins between −0.00043 and +0.00006. No consistent direction.

## 7. Calibration by band

Both engines are over-confident at the top and under-confident at the bottom —
the standard shape, and largely unchanged by the correction. B is very slightly
better in the low bands where the extra draw mass lands.

## 8. Decision

Against the stated rule — *if Dixon-Coles does not improve the appropriate
out-of-sample metrics, do not force it into production simply because
theoretically it should help* — the evidence does not clear the bar:

- Brier improvement is **not distinguishable from chance**
- Log loss is **worse**
- Helps in **21 of 38** competitions, i.e. roughly a coin flip
- Over-corrects in leagues that were already close to calibrated

It does fix the specific bias it was raised for, and ECE improves. That is
enough to keep working on it, not enough to make it the production engine.

**Shipping decision:**

| | |
|---|---|
| **Canonical grid (`app.quant.grid`)** | **Merge.** Pure architecture; with the correction off it reproduces the previous engine byte-identically, pinned by `test_disabled_reproduces_the_previous_engine_exactly`. |
| **Dixon-Coles correction** | **Off in production.** Set `QUANT_DIXON_COLES_ENABLED=0`. |

## 9. What would change the answer

1. **Per-league ρ.** `estimate_rho()` already exists and fits ρ by maximum
   likelihood with a 200-match floor. The per-competition table above is what a
   single global ρ looks like; a fitted one should stop the overshoot in E0,
   E1, IRL and JAP. This is the most promising next step.
2. **Re-audit after per-league ρ**, same protocol, before any production
   change.
3. **Then** Phase A3 weight tuning. Tuning ensemble weights on top of an
   unsettled scoreline engine would tune against a moving target.

## 10. Model versions

Published selections are never restamped. A selection carries the version that
produced it, permanently.

| Engine | Component | Grid version | Published selection version |
|---|---|---|---|
| Independent Poisson | `poisson` | `grid-v1-poisson` | `model-only-v1` |
| Dixon-Coles | `poisson_dc` | `grid-v2-poisson-dc` | `model-only-v2-dc` |

The version is read at publication time, so toggling the switch takes effect on
the next scan without a deploy and without the recorded version disagreeing
with the engine that produced the numbers.

## 11. Regression found during integration

Ensemble weights are keyed on the component's **name**. Naming the Poisson
component after the engine — `poisson_dc` — resolves to a configured weight of
**0.0** and silently transfers its entire 0.50 share to Elo and Form:

```
component named 'poisson'     -> {'poisson': 0.50,    'elo': 0.35, 'form': 0.15}
component named 'poisson_dc'  -> {'poisson_dc': 0.00, 'elo': 0.70, 'form': 0.30}
```

The full suite passed with this in place. The component keeps the name
`poisson`; the engine generation is recorded in the version string instead.
Pinned by `TestEnsembleWeightingSurvives`.

## 12. Scope

This audit measures **calibration only**. It makes no comparison against
bookmaker prices and no claim about edge. The value-detection gate is
unchanged.
