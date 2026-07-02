# DoveLoButtoAI 🤖

Telegram bot for Italian waste sorting — just snap a photo of any trash and the bot tells you exactly which bin it goes in.

**Bot:** [@EcoGlassAaronBot](https://t.me/EcoGlassAaronBot)

## Features

- 📸 **Photo analysis** — Send a photo of any waste item; GPT-4.1 Vision identifies it and tells you the correct disposal method
- 🇮🇹 **Italian only** — All responses in Italian, tailored to your Comune's local rules
- 📍 **Location-aware** — Save your Comune and get rules specific to your municipality
- 🍝 **Food pre-classification** — When the photo is ambiguous (e.g., pizza box), the bot asks clarifying questions with inline buttons
- 🔍 **Text search** — Type any waste item name (e.g., "lampadina", "polistirolo") for instant lookup
- ⚡ **Hybrid AI** — Local SQLite database caches answers; OpenAI is only called on first-time queries
- 📊 **Built-in analytics** — `/stats` shows users, cache hits, OpenAI usage, and money saved

## Tech Stack

- Python 3.11 + `python-telegram-bot` 22.8
- OpenAI GPT-4.1 Vision for image recognition
- SQLite (local) for waste disposal rules + user analytics
- asyncio-safe OpenAI calls with 30s timeout
- pnpm workspace monorepo (API server + mockup sandbox)

## How it works

```
User sends photo
  ↓
Food detection (GPT-4.1) — is this ambiguous?
  ↓
YES → Show inline buttons (e.g., "Pizza intera" / "Solo scatola")
  ↓
NO  → Full vision analysis → Reply with disposal rules
  ↓
Auto-saved to local DB → Next time = instant reply
```

## Commands

| Command | Description |
|---|---|
| `/start` | Welcome message + category buttons |
| `/comune` | Set your municipality |
| `/stats` | Bot analytics (users, cache hits, OpenAI usage) |

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | From @BotFather |
| `OPENAI_API_KEY` | Yes | From OpenAI dashboard |

## Local Development

```bash
# Install dependencies
pip install python-telegram-bot openai httpx

# Run the bot
python bot/ecoglass_bot.py
```

## Deployment

This bot is deployed on Replit as a **VM (Always Running)** service, keeping the polling loop alive 24/7.

## Architecture Decisions

- **All OpenAI calls are async-safe** via `asyncio.to_thread()` + `asyncio.wait_for(timeout=30)` — direct sync calls block the event loop and silence the bot
- **Fire-and-forget DB tracking** — user analytics and auto-save run in background threads so they never slow down replies
- **Global error handler** — `app.add_error_handler()` catches any uncaught exception; the bot logs it and keeps polling
- **Per-handler try/except** — every command, message, and callback is wrapped for graceful degradation

## License

MIT
