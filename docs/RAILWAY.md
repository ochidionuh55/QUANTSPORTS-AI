# Deploying QUANTSPORT on Railway

Railway runs each process as its own service, so the compose file does not
apply. Three services share one repository and one database.

Roughly **$10–20/month** for three always-on services plus Postgres and Redis —
more than a €4 VM, but no card verification and no server to maintain.

---

## 1. Create the project

At [railway.app](https://railway.app), sign in with GitHub, then **New Project
→ Deploy from GitHub repo** and pick this repository.

Push it first if it is not on GitHub yet:

```powershell
cd "$HOME\Projects\quantsport_ai"
git init
git add .
git commit -m "QUANTSPORT"
gh repo create quantsport --private --source=. --push
```

## 2. Add the databases

In the project: **New → Database → PostgreSQL**, then again for **Redis**.
Railway provisions both and injects their connection variables.

## 3. Configure the API service

Railway creates one service from the repo. Under **Variables**:

```
ENVIRONMENT=production
DEBUG=false
OBSERVABILITY__LOG_FORMAT=json
SERVICE_ROLE=api

TELEGRAM__BOT_TOKEN=<from @BotFather>
API_FOOTBALL_KEY=<from api-football.com>

POSTGRES__HOST=${{Postgres.PGHOST}}
POSTGRES__PORT=${{Postgres.PGPORT}}
POSTGRES__USER=${{Postgres.PGUSER}}
POSTGRES__PASSWORD=${{Postgres.PGPASSWORD}}
POSTGRES__DATABASE=${{Postgres.PGDATABASE}}

REDIS__HOST=${{Redis.REDISHOST}}
REDIS__PORT=${{Redis.REDISPORT}}
REDIS__PASSWORD=${{Redis.REDISPASSWORD}}
```

The `${{Postgres.*}}` syntax is Railway's variable reference — it fills in the
real values at deploy time, so no credential is ever typed into a file.

## 4. Add the bot and worker services

**New → GitHub Repo**, same repository, twice more. For each, set the same
variables, then change:

| Service | `SERVICE_ROLE` | Start command |
|---|---|---|
| bot | `bot` | `python -m app.bot.main` |
| worker | `worker` | `python -m app.worker.main` |

Set the start command under **Settings → Deploy → Custom Start Command**.

Only the API service needs a public domain. Turn networking off for the other
two — they make outbound connections and accept none.

## 5. Load the data

Once the API service is running, from **Settings → Deploy → Run Command**, or
via the CLI:

```bash
railway run python scripts/ingest.py
railway run python scripts/backfill.py --reference market_avg
railway run python scripts/reconstruct_highlights.py --days 365
```

Ingestion takes 20–40 minutes.

## 6. Verify

```bash
railway run python scripts/smoke_test.py
```

Every line should read `ok`. It checks Postgres, Redis, migrations, historical
data, the scheduled scan, bot and worker heartbeats, the live provider,
Telegram authentication, and that the value gate is still closed.

---

## Confirming it runs without your PC

**Close your laptop, wait an hour, message the bot.** If it replies, it is
running on Railway.

`Scheduled scan: last ran 1.4h ago` in the smoke test proves the worker ran on
its own.

---

## Things worth knowing

**Migrations run on every API deploy**, via the start command. Alembic is
idempotent, so redeploying is safe.

**Backups are Railway's job here.** Their Postgres plugin takes them; there is
no backup sidecar as in the compose stack. Check the retention on your plan and
take a manual dump before anything risky:

```bash
railway run pg_dump -Fc > quantsport-backup.dump
```

**Usage billing means the worker costs money while idle.** It sleeps between
scans but the container stays up. If cost matters more than latency, widen
`SCAN_INTERVAL_SECONDS`.

**The free API-Football tier allows 100 requests a day** wherever you host. A
busy Saturday can exhaust it; the budget guard degrades gracefully rather than
erroring, and you will see fixtures without prices.
