# Deploying QUANTSPORT AI

Running 24/7 on a cloud server, independent of any desktop machine.

## What you need

A Linux VM with **2 vCPU and 4 GB RAM**. Hetzner CX22 (~€4/month), DigitalOcean
or Vultr all work. 2 GB is enough to run but tight during ingestion, and the
swap thrashing costs more in time than the extra euro.

Storage: 40 GB. The database reaches roughly 3 GB with all 38 leagues ingested
and backfilled; the rest is images and backups.

## Managed services: worth it or not

**Postgres is the one worth paying for.** It holds 113,000 matches and 93,000
settled predictions that took hours to build. A managed instance gives you
point-in-time recovery and someone else's problem at 3am. Around $15/month.

**Redis is not.** It holds cache, locks and heartbeats — all rebuildable in
seconds. The bundled container is fine.

To use managed Postgres, set `POSTGRES__HOST` and friends in `.env`, then
comment the `postgres` service out of `docker-compose.prod.yml`. Nothing else
changes; the application cannot tell the difference.

---

## First deployment

### 1. Prepare the server

```bash
ssh root@your-server-ip

apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh

# Run as a non-root user. A compromised container should not own the host.
adduser --disabled-password --gecos "" quantsport
usermod -aG docker quantsport
```

### 2. Copy the project across

From your machine:

```powershell
scp -r "$HOME\Projects\quantsport_ai" quantsport@your-server-ip:~/
```

### 3. Configure

```bash
ssh quantsport@your-server-ip
cd quantsport_ai
cp .env.example .env
nano .env
```

Required. The template ships with development defaults, and the application
refuses to start in production with any of them left in place — a debug build
or a placeholder password reaching a public server is worse than a failed
deploy:

```bash
ENVIRONMENT=production
DEBUG=false
OBSERVABILITY__LOG_FORMAT=json

POSTGRES__PASSWORD=<a long random string>
TELEGRAM__BOT_TOKEN=<from @BotFather>
API_FOOTBALL_KEY=<from dashboard.api-football.com>
```

Generate the password rather than inventing one:

```bash
openssl rand -base64 32
```

Then lock the file down — it holds every secret you have:

```bash
chmod 600 .env
```

### 4. Start

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Migrations run automatically in their own container before the services start,
so a deployment that would run new code against an old schema fails there
rather than at 3am.

### 5. Load the data

Once, on first deployment. Takes 20–40 minutes.

```bash
docker compose -f docker-compose.prod.yml exec api python scripts/ingest.py --data /data
docker compose -f docker-compose.prod.yml exec api python scripts/backfill.py --data /data --reference market_avg
```

### 6. Verify

```bash
docker compose -f docker-compose.prod.yml exec api python scripts/smoke_test.py
```

Every line should read `ok`. It checks Postgres, Redis, migrations, historical
data, the scheduled scan, bot and worker heartbeats, the live provider,
Telegram authentication, and that the value gate is still closed.

Exits non-zero on failure, so it can gate a deploy or run from cron.

---

## Confirming it runs without your PC

The real test: **close your laptop, wait an hour, then message the bot.** If it
replies, it is running on the server.

More precisely, from anywhere:

```bash
ssh quantsport@your-server-ip \
  'cd quantsport_ai && docker compose -f docker-compose.prod.yml exec -T api python scripts/smoke_test.py'
```

`Scheduled scan: last ran 1.4h ago` proves the worker is running on its own.

You can also check the API over an SSH tunnel:

```bash
ssh -L 8000:localhost:8000 quantsport@your-server-ip
# then open http://localhost:8000/health
```

The API is bound to loopback deliberately. Nothing outside the server needs it,
and an exposed service with no authentication is how servers get taken over.

---

## Troubleshooting

**`password authentication failed for user "quantsport"`** — Postgres only
applies `POSTGRES_PASSWORD` when it first initialises a data directory. If you
change the password afterwards, the existing volume keeps the old one. Either
start clean:

```bash
docker compose -f docker-compose.prod.yml down -v   # destroys the database
```

or change it inside Postgres instead:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U quantsport -c "ALTER USER quantsport WITH PASSWORD 'new-password';"
```

**`Cannot connect to host api.telegram.org`** — a DNS or egress problem on the
host, not a configuration error. Check the server can resolve it:

```bash
docker compose -f docker-compose.prod.yml exec api python -c \
  "import socket; print(socket.gethostbyname('api.telegram.org'))"
```

Telegram is blocked in some jurisdictions. If the server cannot reach it, the
bot will not run there regardless of configuration.

**`down -v` and your other stack** — the production stack runs under the
project name `quantsport-prod`, the development one under `quantsport`, so
their volumes are namespaced apart. Tearing one down cannot touch the other's
database. Verify which volumes belong to which:

```bash
docker volume ls | grep quantsport
```

**`migrate` exits non-zero** — read the reason before anything else:

```bash
docker compose -f docker-compose.prod.yml logs migrate --tail 40
```

Nothing downstream starts until migrations succeed, which is deliberate: a
deployment running new code against an old schema is worse than one that
refuses to start.

## Day-to-day

```bash
cd quantsport_ai

docker compose -f docker-compose.prod.yml ps           # what is running
docker compose -f docker-compose.prod.yml logs -f bot  # follow the bot
docker compose -f docker-compose.prod.yml restart worker
```

### Deploying a change

```bash
git pull   # or scp the new version
docker compose -f docker-compose.prod.yml up -d --build
```

Migrations run first. Containers restart one at a time.

### Backups

The `backup` service dumps the database daily to `./backups/` and keeps 14
days. Dumps are written to a `.partial` file first, so an interrupted backup
never replaces a good one with a truncated file.

Restore:

```bash
gunzip -c backups/quantsport-2026-09-09.sql.gz \
  | docker compose -f docker-compose.prod.yml exec -T postgres \
      psql -U quantsport -d quantsport
```

**Copy backups off the server.** A backup on the same disk as the database
protects against mistakes, not against losing the disk:

```bash
scp quantsport@your-server-ip:~/quantsport_ai/backups/*.sql.gz ./local-backups/
```

---

## Monitoring

The cheapest useful monitor: a daily cron running the smoke test and emailing
on failure.

```bash
crontab -e
```

```cron
0 7 * * * cd /home/quantsport/quantsport_ai && docker compose -f docker-compose.prod.yml exec -T api python scripts/smoke_test.py || echo "QUANTSPORT smoke test failed" | mail -s "QUANTSPORT alert" you@example.com
```

For uptime alerting, point [UptimeRobot](https://uptimerobot.com) or
[Healthchecks.io](https://healthchecks.io) at a small endpoint behind a reverse
proxy. Both have free tiers.

Restart policy is `always`, so containers return after a host reboot without
anyone logging in. Logs rotate at 10 MB with five files kept — without that, a
chatty worker fills the disk in a few weeks.

---

## Costs

| Item | Monthly |
|---|---|
| Hetzner CX22 (2 vCPU, 4 GB) | ~€4 |
| API-Football free tier | €0 |
| Domain, optional | ~€1 |
| Managed Postgres, optional | ~€15 |

Roughly **€5/month** self-hosted, **€20** with managed Postgres.

---

## Security notes

The `.env` file holds your bot token, API key and database password. `chmod
600`, never commit it, and rotate the bot token via @BotFather if it is ever
exposed.

Postgres and Redis publish no ports. The API binds to loopback. The containers
run as a non-root user. Keep it that way.

If you later expose the API publicly, put it behind Caddy or nginx with TLS and
authentication — the admin endpoints assume they are not reachable from the
open internet.
