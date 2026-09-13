# QUANTSPORT AI — brand in the bot

What the brand system means in practice for Telegram, and what has to be set by
hand because Telegram has no API for it.

## Palette

Applied to every generated chart in `app/bot/charts.py`.

| Role | Hex | Where |
|---|---|---|
| Primary emerald | `#08B85A` | Bars, accents, the signature |
| Deep emerald | `#006B42` | Reference lines |
| Bright green | `#35E56F` | Highlights |
| Lime | `#B6FF37` | Sparingly, never as a fill |
| Near-black | `#07171D` | Headings, labels |
| Muted grey | `#64757D` | Axis text, secondary copy |
| Borders | `#DFEAE5` | Gridlines |
| Surface | `#EFFBF5` | Light fills |

**Charts are white-first, not dark.** A white card in a dark conversation reads
as a published artefact rather than a screenshot, and can be reposted to X
without looking out of place.

**Negative uses `#C2410C`, a warm amber-red**, deliberately away from the red
every betting site uses. Losses must be legible without the product looking
like a casino.

## What you must set by hand in @BotFather

Telegram exposes none of this through the API.

**Profile photo** — `/setuserpic`. Use the **symbol-only Q mark**, not the full
wordmark: at 48px the lockup becomes unreadable mush.

**Name** — `/setname`

```
QUANTSPORT AI
```

**Short description** — `/setabouttext`, shown before a user starts the bot:

```
Football intelligence, quantified. Explore matches, markets, teams, history
and model-driven insights. Ask the Data.
```

**Full description** — `/setdescription`, shown on the empty chat screen:

```
QUANTSPORT AI combines verified football data, mathematical models, market
intelligence and historical evidence to help you understand the game more
deeply.

Explore Today's Analysis, Best of Today, Market Explorer, Team Intelligence,
Competitions, History and Track Record.

High probability is not certainty. No outcome is guaranteed. 18+.
```

**Commands** — `/setcommands`:

```
start - Open QUANTSPORT AI
team - A club's full record
find - Search fixtures in plain language
recent - Fixtures you opened recently
saved - Fixtures you saved
help - How to use QUANTSPORT
```

## Voice

Consistent with the website brief:

- **Say** "Football Intelligence, Quantified", "Ask the Data",
  "Model–market divergence", "No qualifying selection today"
- **Never say** guaranteed, fixed, sure win, unbeatable, 100%, banker

The wording rules are enforced in tests, not just documented: a service that
cannot be shown to beat the market must never be described as though it does.

## Channel assets

The full wordmark and the promotional header belong in channel posts, pinned
messages and launch graphics — never as the bot avatar.

Generated charts carry the signature automatically, so any screenshot a user
posts is already branded.
