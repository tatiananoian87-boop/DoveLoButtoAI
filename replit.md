# DoveLoButtoAI (EcoGlass Bot)

Telegram bot for Italian waste sorting — snap a photo of any trash and get instant disposal instructions in Italian.

## Run & Operate

- `python bot/ecoglass_bot.py` — run the Telegram bot (dev)
- Workflow: "EcoGlass Bot" — `python bot/ecoglass_bot.py`
- Required env: `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`

## Stack

- Python 3.11, `python-telegram-bot==22.8`, `openai`, `httpx`
- SQLite (stdlib) for waste rules DB + user analytics
- pnpm workspaces for monorepo (API server + mockup sandbox)
- GPT-4.1 Vision for photo analysis

## Where things live

- `bot/ecoglass_bot.py` — main bot (handlers, OpenAI integration, inline keyboards)
- `bot/waste_db.py` — SQLite DB (36 seeded waste objects, user stats, lookup, auto-save)
- `bot/github_client.py` — GitHub push via Node.js bridge
- `.local/skills/deployment/SKILL.md` — deployment skill reference

## Architecture decisions

- **All OpenAI calls are async-safe** via `asyncio.to_thread()` + `asyncio.wait_for(timeout=30)` — direct sync calls block the event loop and silence the bot
- **Fire-and-forget DB tracking** — user analytics and auto-save run in background threads; never block replies
- **Global + per-handler error handlers** — `app.add_error_handler()` catches uncaught exceptions; every command/message/callback has its own try/except for graceful degradation
- **Food pre-classification** — ambiguous items trigger inline clarifying questions before full vision analysis, saving tokens
- **Local SQLite caching** — answers cached after first OpenAI call; future queries return instantly without API cost

## Product

- Send a photo → bot identifies waste + tells you which bin (indifferenziata, plastica, carta, vetro, umido, etc.)
- Text search → type any waste item name for instant lookup
- `/comune` → set your municipality for local rules
- `/stats` → analytics dashboard (users, cache hits, OpenAI calls, money saved)
- Food ambiguity → inline buttons (e.g., "Pizza intera" vs "Solo scatola")

## User preferences

- Bot speaks **Italian only**
- Waste rules are **tailored per Comune**
- Inline buttons preferred over text menus for clarifying questions

## Deployment

1. Open the **Publish** panel in Replit
2. Set **Deployment type** to `Always Running (VM)` — required for Telegram long-polling bots
3. Set **Run command** to `python bot/ecoglass_bot.py`
4. Verify both secrets (`TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`) are available in production
5. Click **Publish**

## Gotchas

- `TELEGRAM_BOT_TOKEN` and `OPENAI_API_KEY` must be set as Secrets (not plain env vars)
- Never use sync OpenAI calls in async handlers — always wrap in `asyncio.to_thread()`
- SQLite DB lives in working directory (`waste_sorting.db`) — persists across restarts but not across fresh Replit containers unless committed
- Deployment target must be **VM (Always Running)** for Telegram long-polling bots — autoscale will kill the bot between requests
- If publishing fails, check that `.replit` has `deploymentTarget = "vm"` in the `[deployment]` section

## Pointers

- See the `pnpm-workspace` skill for workspace structure
- See the `deployment` skill for publish/deploy configuration
- GitHub repo: https://github.com/tatiananoian87-boop/DoveLoButtoAI
