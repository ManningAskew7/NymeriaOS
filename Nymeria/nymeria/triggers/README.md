# triggers/

All entry points into Nymeria: API server, bots, CLI, and watchdog.

## Start here

`api.py` — FastAPI app factory, auth middleware, CORS setup.

## Contents

- `api.py` — FastAPI application factory
- `sse_consumer.py` — shared SSE event dispatcher for all surfaces
- `discord_bot.py`, `telegram_bot.py`, `twitch_bot.py` — bot entry points
- `api_client.py` — httpx client used by thin-client bots
- `bot_helpers.py` — shared bot utilities
- `watchdog_worker.py` — TODO watchdog daemon
- `slash_dispatcher.py` — slash command routing
- `cli/` — interactive CLI
- `sources/` — event trigger source implementations
- `discord_cogs/` — Discord slash command cogs
