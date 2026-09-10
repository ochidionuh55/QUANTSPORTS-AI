# QUANTSPORT AI — Permanent Architecture Specification

**Version 2.0** — supersedes the original Project Master Blueprint and Master Prompt.
Status: agreed. Changes to this document require an explicit impact review.

---

## 1. What this system is

A quantitative sports analysis platform. It ingests pre-match markets from a
configurable data provider, estimates outcome probabilities using statistical
models, compares those estimates against market prices, and — once and only
once the models have earned it — surfaces positive expected-value opportunities
through a Telegram interface, optionally producing bookmaker booking codes.

**What it is not:** a tipping service, a guaranteed-return product, or an
automated wagering agent. It never places bets from user accounts. It never
claims profitability. Probability outputs are estimates with quantified
uncertainty.

---

## 2. Governing principles

These are non-negotiable and are enforced in code, not merely documented.

1. **The model must earn the right to disagree with the market.** Market prices
   are the prior. Deviation must be justified by measured historical
   calibration.
2. **No user-facing edge output before validation.** `value_detection_enabled`
   is false until a named model version passes the promotion gate.
3. **Never force a selection.** When nothing passes the filters, the answer is
   `NO QUALIFIED OPPORTUNITY FOUND.`
4. **Confidence means measured uncertainty**, never a marketing number.
5. **Providers are replaceable.** No business logic knows which bookmaker or
   vendor is behind the data.
6. **Credits are never deducted before successful completion.**
7. **Every prediction is reproducible** from its recorded inputs and version.
8. **No future information, ever**, in training, validation or backtesting.

---

## 3. Contradiction analysis

The revised architecture introduced eleven conflicts with the original
blueprint. Each is resolved below. **Items 3.1 and 3.7 are the two that can
still sink the project and require decisions before Phase 2 completes.**

### 3.1 The market is both the prior and the thing being beaten — UNRESOLVED

If the market prior and the priced odds come from the same bookmaker, expected
value is measured against itself. Shrinking the model toward SportyBet's own
implied probabilities and then computing `EV = p·odds − 1` against SportyBet's
odds drives EV structurally toward zero. Any residual edge is model error, not
market error.

**Resolution required.** The prior must come from a sharper reference than the
book being bet into. The standard structure is:

```
Sharp reference (Pinnacle / market consensus)  →  margin removal  →  fair prior
                                                                        │
Own models  ──────────────────────────────────────────────────────────► posterior
                                                                        │
Soft book (SportyBet) odds  ────────────────────────────────────────►  EV
```

This changes `odds_snapshots` (it needs a `provider_role` distinguishing
`reference` from `tradeable`) and adds a second odds source to Phase 10.
**Decide before Phase 2 schema is frozen.** Deferring means a migration later.

### 3.2 Telegram foundation precedes everything it displays

Phase 3 ships the bot, but wallet is Phase 12 and predictions are Phase 9.
**Resolution:** Phase 3 delivers `/start`, registration, `/help`, the
responsible-use notice and age gate only. Feature buttons render conditionally
on feature flags and are absent until their phase lands.

### 3.3 Credits gate scans that exist before the wallet does

Scanning (Phase 6) and the opportunity scanner (Phase 9) precede monetisation
(Phase 12). **Resolution:** ledger tables exist from Phase 2; scans are
admin-only and free until Phase 12. Pricing is configuration, not code.

### 3.4 Confidence score is deferred but is referenced as a filter

The original selection modes filter on "minimum confidence 70" and
`predictions.confidence_score` was non-null. **Resolution:**
`confidence_score` is nullable. Selection modes filter on probability, edge and
EV only until calibration data exists. No score is emitted before it can be
measured.

### 3.5 Value detection is disabled, but the business model sells scans

If users cannot receive edge output, there is no product to monetise before the
gate passes. **Resolution:** monetisation stays behind the gate. Do not sell a
"scan" that cannot return a selection. If a pre-gate product is wanted, it must
be an explicitly research-framed statistical readout with no recommendation —
and that is a business decision to make deliberately, not a fallback.

### 3.6 Booking codes have nothing to book

Phase 11 depends on selections produced by flagged-off Phase 9.
**Resolution:** Phase 11 builds and tests selection mapping against fixture
data from `MockProvider`. Live code generation activates with the flag.

### 3.7 Odds coverage vastly exceeds model coverage

Free historical CSVs cover roughly 20–25 leagues. A bookmaker's fixture list
covers hundreds. **Resolution:** every match carries a `is_modellable` flag
derived from whether sufficient historical data exists for both teams. Matches
outside the modelled universe are never scanned into predictions and are
reported as unmodelled — never silently omitted, which would look like a filter
result rather than a coverage gap.

### 3.8 Point-in-time data is not fully achievable in the MVP

Free CSV datasets are revised in place and carry no vintage history, so
"data as it existed at prediction time" cannot be perfectly reconstructed.
**Resolution:** every ingestion records a `data_version` and ingestion
timestamp; the limitation is documented and accepted for the MVP; the
`HistoricalDataProvider` interface allows a vintaged paid source later.

### 3.9 Closing-line capture requires polling the scanner cannot provide

Opening and closing snapshots need continuous scheduled capture independent of
user activity, which multiplies provider request volume and cost.
**Resolution:** `scheduled_odds_capture_enabled` is a separate flag, owned by
the worker, activated in Phase 6, with capture limited to the modellable
fixture subset.

### 3.10 Idempotency scope was undefined

**Resolution:** `idempotency_key` is unique per `(user_id, operation_type,
key)`, enforced by a database constraint, with a retention window. A repeated
key returns the original result rather than re-executing.

### 3.11 The worker computes results but the bot owns the Telegram session

**Resolution:** the worker sends user-facing messages directly using its own
`Bot` instance. The bot process owns polling only. Both read the same token.
No message queue is introduced for this in v1.

---

## 4. System architecture

```
                    TELEGRAM USERS
                          │
                          ▼
        ┌───────────── BOT (aiogram) ─────────────┐
        │  polling, validation, lightweight reads │
        └────────────────┬────────────────────────┘
                         │ enqueue
                         ▼
        ┌──────────── REDIS (mandatory) ──────────┐
        │  cache · job state · locks · heartbeats │
        └────────────────┬────────────────────────┘
                         │
        ┌────────────────▼────────────────────────┐
        │  WORKER (APScheduler)                   │
        │  scans · ingestion · quant · backtests  │──► sends results to Telegram
        └────────────────┬────────────────────────┘
                         │
        ┌────────────────▼────────────────────────┐
        │  POSTGRESQL — system of record          │
        └────────────────┬────────────────────────┘
                         │
        ┌────────────────▼────────────────────────┐
        │  API (FastAPI) — health, admin, internal│
        └─────────────────────────────────────────┘

PROVIDER LAYER (all replaceable, all normalised)
  MarketDataProvider     → MockProvider · SportyProvider · future vendors
  HistoricalDataProvider → MockHistorical · CSVHistorical · APIHistorical
  BookingProvider        → MockBooking · third-party booking-code service

QUANT PIPELINE
  raw odds → margin removal (Proportional | Power | Shin) → fair prior
  history  → Poisson · Elo · Form                        → model estimate
  prior + estimate → log-odds combination + shrinkage    → posterior
  posterior vs tradeable odds → EV → filters → correlation control → ranking
```

---

## 5. Data model (Phase 2 scope)

Core entities beyond the original blueprint:

| Table | Purpose |
|---|---|
| `teams` | Canonical team identity |
| `team_aliases` | Provider name → canonical ID, with confidence and confirmation |
| `historical_matches` | Results and goals; logically separate from live fixtures |
| `model_versions` | Config, feature set, margin method, ensemble method, metrics |
| `odds_snapshots` | Opening/closing/interim prices; see 3.1 re `provider_role` |
| `wallet_transactions` | Append-only ledger; `users.credits` is a derived cache |

Every `predictions` row records `model_version_id`, the `odds_snapshot_id` used,
the margin method, the market prior, the model adjustment and the final
probability, so any prediction can be reconstructed exactly.

Concurrency: balance operations use `SELECT ... FOR UPDATE`. Reservation states
are `RESERVED → COMPLETED | RELEASED | FAILED`.

---

## 6. Validation gate

Value detection stays disabled until a model version demonstrates, on a
strictly chronological held-out test period, meaningful improvement or
complementary information relative to the market baseline.

Primary metrics: Brier score, log loss, expected calibration error, reliability
diagrams. Secondary: ROI, closing-line value. **ROI is never the promotion
criterion** — it is too noisy over realistic sample sizes to distinguish skill
from variance.

No random train/test splits. Chronological splits only.

Promotion is recorded as `FEATURES__PROMOTED_MODEL_VERSION`. The application
refuses to start with value detection enabled and no promoted version named.

---

## 7. Phase order

| Phase | Deliverable |
|---|---|
| 1 | Infrastructure ✅ |
| 2 | Database and core domain models |
| 3 | Telegram foundation (registration, age gate, disclaimer only) |
| 4 | Market data provider abstraction + MockProvider |
| 4B | HistoricalDataProvider + Mock + CSV implementations |
| 5 | Team identity resolution + historical ingestion |
| 6 | Match scanning + odds snapshots + freshness management |
| 7 | Core models: implied probability, margin removal, Poisson, EV |
| 7B | Backtesting harness + calibration framework |
| 8 | Elo, form, market-prior combination, shrinkage |
| 8B | **Model validation and promotion gate** |
| 9 | Opportunity scanner + correlation control (flagged) |
| 10 | Real market data provider integration |
| 11 | Booking code provider integration |
| 12 | Wallet, monetisation, payments |
| 13 | Analytics dashboard |

---

## 8. Legal and responsible use

Present from Phase 1: 18+ statement, responsible-use notice, explicit
"estimates, not predictions" framing, no profit claims anywhere in copy.

The system must not be designed around bypassing bookmaker authentication,
security or access controls. Automated access to a bookmaker is likely contrary
to its terms of service; provider isolation limits the technical blast radius of
losing access but does not resolve the business dependency.

Before Phase 12, verify in writing that the chosen payment processor permits
gambling-adjacent merchants in the operating jurisdiction.
