# Phase 1 — Acceptance Criteria

Phase 1 is infrastructure only. No prediction logic, no provider integration,
no database models, no user-facing bot features.

## Verification

Run `make check` for the offline gate, then `make up` for the live stack.

### A. Quality gate (offline, no services required)

| # | Criterion | Command | Status |
|---|---|---|---|
| A1 | Lint passes with security and async rules enabled | `ruff check .` | ✅ pass |
| A2 | Formatting is canonical | `ruff format --check .` | ✅ 31 files |
| A3 | Strict type checking passes | `mypy app` | ✅ 25 files |
| A4 | Test suite passes | `pytest` | ✅ 54 tests |
| A5 | Tests run without Postgres or Redis | `pytest` offline | ✅ pass |

### B. Configuration

| # | Criterion | Status |
|---|---|---|
| B1 | All config loads from environment; no secrets in source | ✅ |
| B2 | `.env.example` documents every variable | ✅ |
| B3 | Credentials are redacted from DSNs, `repr` and health output | ✅ tested |
| B4 | Production refuses to start without a Postgres password | ✅ tested |
| B5 | Bot refuses to start without a Telegram token | ✅ tested |
| B6 | Nested `__` env vars and CSV admin IDs parse correctly | ✅ tested |

### C. Governance enforced in code

| # | Criterion | Status |
|---|---|---|
| C1 | `value_detection_enabled` defaults false | ✅ tested |
| C2 | Enabling it without a promoted model version fails startup | ✅ tested |
| C3 | Booking codes require value detection | ✅ tested |
| C4 | Responsible-use notice exists with age statement, no profit claims | ✅ tested |
| C5 | `NO QUALIFIED OPPORTUNITY FOUND.` is a frozen constant | ✅ tested |

### D. Observability

| # | Criterion | Status |
|---|---|---|
| D1 | Logs are JSON with level, timestamp, service, version | ✅ tested |
| D2 | Correlation IDs propagate and return on `X-Correlation-ID` | ✅ tested |
| D3 | Inbound correlation IDs are preserved | ✅ tested |
| D4 | Log level is respected | ✅ tested |
| D5 | Stack traces never reach clients | ✅ handler in place |

### E. Health and probes

| # | Criterion | Status |
|---|---|---|
| E1 | `/health/live` returns 200 regardless of dependencies | ✅ tested |
| E2 | Liveness touches zero dependencies | ✅ tested (call count) |
| E3 | `/health/ready` returns 503 when Postgres or Redis is down | ✅ tested |
| E4 | `/health` reports siblings as non-required → `degraded` | ✅ tested |
| E5 | `/system/info` exposes flags and no credentials | ✅ tested |

### F. Three-process operation

| # | Criterion | Status |
|---|---|---|
| F1 | API, bot and worker are separate compose services | ✅ |
| F2 | All three share one image; roles differ by command only | ✅ |
| F3 | Bot and worker publish TTL'd Redis heartbeats | ✅ tested |
| F4 | Worker jobs hold a distributed lock; lock released on failure | ✅ tested |
| F5 | SIGTERM shuts down cleanly; shutdown runs even if startup raises | ✅ tested |
| F6 | Postgres and Redis have compose healthchecks; app waits on them | ✅ |
| F7 | Containers run as a non-root user | ✅ uid 1000 |

### G. Live stack (requires Docker)

| # | Criterion | How to verify |
|---|---|---|
| G1 | `make up` brings up five services | `make ps` |
| G2 | Readiness is 200 against live services | `curl localhost:8000/health/ready` |
| G3 | Worker heartbeat appears within 60s | `make health` shows `worker-process: up` |
| G4 | Worker self-check logs every 60s | `docker compose logs worker` |
| G5 | `docker compose stop` exits cleanly, no SIGKILL | check exit codes |

> G1–G5 could not be executed in the build environment (no Docker daemon).
> They are the only outstanding Phase 1 checks and must be run on your machine.

## Explicitly out of scope for Phase 1

Database models and migrations · provider integrations · quant models ·
bot commands and menus · wallet logic · booking codes · payments.

## Sign-off

Phase 1 is complete when A–F are green (they are) and G1–G5 have been
confirmed locally.
