# QUANTSPORT AI — website

Next.js frontend for the QUANTSPORT intelligence layer.

## The one architectural rule

**No model logic lives here.** Every probability, rate and count is fetched
from the FastAPI backend that the Telegram bot also reads. The website and the
bot therefore cannot disagree about what QUANTSPORT published, which is the
whole point of having one source of truth.

If you find yourself computing a percentage in a component, the number belongs
in the API instead.

## Running locally

```bash
cd web
npm install
QUANTSPORT_API=http://localhost:8000 npm run dev
```

The backend must be running. Without it, pages render with empty states rather
than errors — a page that cannot reach the API should say the data is
unavailable, not collapse.

## Deploying to Vercel

1. **New Project** → import `QUANTSPORTS-AI` → set **Root Directory** to `web`
2. Add an environment variable:

```
QUANTSPORT_API = https://<your-railway-api-domain>
```

Your Railway API service needs a public domain for this
(**Settings → Networking → Generate Domain**).

## Caching

Pages use Next's `revalidate` rather than fetching per request:

| Data | Revalidate |
|---|---|
| Platform summary | 15 minutes |
| Today, services, selections | 5 minutes |

Published selections are immutable, so caching them cannot produce a stale
claim. Only the settled result changes, and five minutes of lag on a result is
not worth hammering the API for.

## Design tokens

Defined once in `tailwind.config.ts`, matching `docs/BRAND.md` and the chart
palette in `app/bot/charts.py`. White-first, emerald-led.

The accent set is deliberately narrow. A product whose credibility rests on
restraint should not have twelve colours.

## Language

The footer disclosure and the homepage copy state plainly that the models are
calibrated but have not been shown to beat bookmaker prices.

That is not legal hedging. It is the finding, and it is the most distinctive
thing the product has.

Never write: guaranteed, sure, fixed, unbeatable, beat the bookmakers,
AI-powered winning predictions.
