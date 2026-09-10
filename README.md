# QUANTSPORT AI

Mathematical intelligence for sports markets.

A quantitative sports analysis platform that estimates match outcome
probabilities from statistical models, compares them against market prices, and
surfaces positive expected-value opportunities through Telegram.

> **Status: Phase 1 — infrastructure.** There is no prediction logic, no
> provider integration and no user-facing bot functionality yet. See
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full plan.

> **Responsible use.** Outputs are statistical estimates, not predictions. They
> carry uncertainty and offer no guarantee of any return. Nothing here is
> financial advice. Use is restricted to people aged 18 or over, in
> jurisdictions where sports betting is legal.

---

## Quick start

```bash
git clone <your-repo-url> quantsport_ai
cd quantsport_ai

cp .env.example .env       # then fill in your values
make up                    # build and start the full stack

curl localhost:8000/health | python3 -m json.tool
```

### Windows (PowerShell)

Windows has no `make`. Use the bundled task runner instead — same commands:

```powershell
cd C:\path\to\quantsport_ai
Copy-Item .env.example .env

# One-time: allow local scripts to run in this session
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

.\make.ps1 up
.\make.ps1 health
.\make.ps1 help      # full task list
```

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(running) and Python 3.12+ on PATH.

The bot service exits without `TELEGRAM__BOT_TOKEN`. Comment it out of
`docker-compose.yml` until you have a token from [@BotFather](https://t.me/botfather),
or run `make up-infra` and start the API locally instead.

### Local development without Docker

```bash
python3.12 -m venv .venv && source .venv/bin/activate
make install
make up-infra              # Postgres and Redis only
uvicorn app.main:app --reload
```

---

## Commands

```
make help        List all commands
make up          Build and start the stack
make down        Stop the stack
make clean       Stop and delete volumes (destroys local data)
make logs        Tail all service logs
make health      Query the API health endpoint

make check       Full quality gate: lint, types, tests
make test        Run tests
make coverage    Tests with coverage report
make lint        Ruff check and format check
make format      Auto-fix and format
make typecheck   Mypy strict
```

---

## Architecture

Three processes share one image and one configuration:

| Service | Role |
|---|---|
| **api** | FastAPI. Health, admin and internal endpoints. The only HTTP surface. |
| **bot** | Aiogram. Telegram polling and lightweight validation. Never runs heavy work. |
| **worker** | APScheduler. Scans, ingestion, quantitative computation, backtests. |

Backed by PostgreSQL (system of record) and Redis (cache, job state,
distributed locks, cross-process heartbeats). **Redis is mandatory, not
optional.**

Because the bot and worker have no HTTP surface, each publishes a short-lived
heartbeat to Redis and the API reports on it. If a process dies, its key expires
and `/health` degrades.

### Health endpoints

| Endpoint | Semantics |
|---|---|
| `GET /health/live` | Process liveness. Touches no dependency; always 200 while running. |
| `GET /health/ready` | 503 when Postgres or Redis is unreachable. |
| `GET /health` | Detailed report including bot and worker heartbeats. |
| `GET /system/info` | Version and feature-flag state. No credentials. |

Liveness and readiness are deliberately separate: a database blip must not
cause the orchestrator to restart otherwise-healthy containers.

---

## Feature flags and the model gate

Two governance decisions are enforced by the application at startup rather than
left to discipline:

```bash
FEATURES__VALUE_DETECTION_ENABLED=false
FEATURES__PROMOTED_MODEL_VERSION=
```

- `value_detection_enabled` **cannot** be set true without naming the model
  version that passed backtest validation. The process refuses to start.
- `booking_codes_enabled` requires `value_detection_enabled`.

User-facing edge output stays off until a model demonstrates, on a strictly
chronological held-out period, that it beats or usefully complements the
market's own implied probabilities. ROI is a secondary metric and never the
promotion criterion.

---

## Configuration

All configuration is environment-driven, with nested keys using a double
underscore:

```bash
POSTGRES__HOST=db
REDIS__PORT=6379
FEATURES__RESEARCH_MODE=true
OBSERVABILITY__LOG_LEVEL=INFO
```

See [`.env.example`](.env.example) for every variable. No secret ever has a
production default, and connection strings are redacted in logs, in `repr()`
and in all health output.

---

## Logging

Structured throughout: JSON in deployed environments, human-readable locally.
Every line carries the service role, environment, version and — where one
exists — a correlation ID that follows a request across processes. The API
accepts and echoes `X-Correlation-ID`.

```json
{"event": "http.request", "method": "GET", "path": "/health",
 "status_code": 200, "duration_ms": 4.1, "correlation_id": "9f2c...",
 "service": "api", "environment": "production", "level": "info"}
```

---

## Project layout

```
app/
  core/            config, logging, health model, constants, version
  api/             FastAPI app, routes, middleware, dependencies
  bot/             Aiogram service entrypoint
  worker/          APScheduler service entrypoint
  infrastructure/  Postgres, Redis, heartbeats
  utils/           lifecycle and shared helpers
tests/             unit tests; run without external services
docker/            Dockerfile (development and production targets)
docs/              architecture spec and phase acceptance criteria
```

---

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — permanent specification,
  contradiction analysis, phase roadmap
- [`docs/PHASE_1_ACCEPTANCE.md`](docs/PHASE_1_ACCEPTANCE.md) — Phase 1
  acceptance criteria and verification status
